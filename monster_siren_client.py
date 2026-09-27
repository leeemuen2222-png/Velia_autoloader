"""Small network client for Velia's optional Monster Siren catalogue.

Run ``python monster_siren_client.py --enrich TITLE`` to query wiki fallbacks.
No media is downloaded until a song is selected or explicitly saved.
"""

import argparse
import html
import json
import io
import re
import shutil
import subprocess
import tempfile
import uuid
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = "https://monster-siren.hypergryph.com"
HEADERS = {"User-Agent": "Velia/1.0 (personal desktop music player)", "Referer": ROOT + "/"}
MAX_AUDIO = 200 * 1024 * 1024


def fetch_bytes(url, limit=6 * 1024 * 1024):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or not (
        parsed.hostname == "monster-siren.hypergryph.com"
        or parsed.hostname.endswith(".hycdn.cn")
        or parsed.hostname in {"arknights.wiki.gg", "prts.wiki"}
    ):
        raise ValueError("Unexpected remote host")
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=18) as response:
        if int(response.headers.get("Content-Length", "0")) > limit:
            raise ValueError("Response too large")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Response too large")
        return data


def api(path):
    raw = json.loads(fetch_bytes(ROOT + path, 8 * 1024 * 1024))
    if raw.get("code") != 0:
        raise ValueError(raw.get("msg") or "Catalogue is temporarily unavailable")
    return raw["data"]


def catalogue():
    albums = {str(a["cid"]): a for a in api("/api/albums")}
    songs = api("/api/songs")["list"]
    return [dict(cid=str(s["cid"]), title=s.get("name") or "—",
                 artist=", ".join(s.get("artists") or s.get("artistes") or []) or "塞壬唱片-MSR",
                 album=albums.get(str(s.get("albumCid")), {}).get("name", ""),
                 album_cid=str(s.get("albumCid") or ""),
                 cover_url=albums.get(str(s.get("albumCid")), {}).get("coverUrl", ""))
            for s in songs if s.get("cid")]


def song_detail(cid):
    if not str(cid).isdigit():
        raise ValueError("Invalid song id")
    return api("/api/song/" + str(cid))


def album_detail(cid):
    if not str(cid).isdigit():
        raise ValueError("Invalid album id")
    return api("/api/album/" + str(cid) + "/detail")


def download(url, destination, limit=MAX_AUDIO):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".hycdn.cn"):
        raise ValueError("Unexpected media host")
    temp = destination.with_name(destination.name + ".part")
    try:
        request = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(request, timeout=25) as response, open(temp, "wb") as out:
            if int(response.headers.get("Content-Length", "0")) > limit:
                raise ValueError("Audio exceeds allowed size")
            total = 0
            while True:
                part = response.read(256 * 1024)
                if not part:
                    break
                total += len(part)
                if total > limit:
                    raise ValueError("Audio exceeds allowed size")
                out.write(part)
        if not total:
            raise ValueError("Empty audio response")
        temp.replace(destination)
        return destination
    finally:
        temp.unlink(missing_ok=True)


def wiki_fallback(title):
    """Only accept exact MediaWiki search titles; avoid attaching a different song."""
    results = []
    for site in ("https://arknights.wiki.gg", "https://prts.wiki"):
        query = urllib.parse.urlencode({"action": "query", "generator": "search",
            "gsrsearch": 'intitle:"' + title + '"', "gsrlimit": 5,
            "prop": "extracts|info", "exintro": 1, "explaintext": 1,
            "exchars": 650, "inprop": "url", "format": "json"})
        try:
            data = json.loads(fetch_bytes(site + "/api.php?" + query))
            pages = (data.get("query") or {}).get("pages") or {}
            normalize = lambda s: re.sub(r"[^\w]+", "", s, flags=re.UNICODE).casefold()
            for page in pages.values():
                name = page.get("title", "").split("/")[-1]
                name = re.sub(r"\s*[（(](?:song|music|歌曲|音乐)[）)]\s*$", "", name, flags=re.I)
                if normalize(name) == normalize(title) or normalize(name) == normalize("歌曲" + title):
                    extract = html.unescape(page.get("extract", "")).strip()
                    if extract:
                        results.append({"source": site, "url": page.get("fullurl", ""),
                                        "summary": extract[:650]})
                        break
        except Exception:
            continue
    return results


def prts_credits(title):
    """Read a song's exact PRTS article linked from 音乐鉴赏.

    The appreciation index itself lists titles, while individual pages list
    release dates and production credits. Never infer a release year from
    the game's timeline or a different song's article.
    """
    query = urllib.parse.urlencode({"action": "query", "prop": "extracts|info",
        "titles": title, "redirects": 1, "explaintext": 1, "exchars": 7000,
        "inprop": "url", "format": "json"})
    try:
        data = json.loads(fetch_bytes("https://prts.wiki/api.php?" + query))
        pages = (data.get("query") or {}).get("pages") or {}
        normalize = lambda s: re.sub(r"[^\w]+", "", s).casefold()
        page = next((v for v in pages.values()
                     if "missing" not in v and normalize(v.get("title", "")) == normalize(title)), None)
        if not page:
            return {}
        body = html.unescape(page.get("extract") or "")
        head = re.split(r"\n\s*(?:歌词|曲目|相关内容|导航)\s*\n", body, maxsplit=1)[0][:4500]
        year = re.search(r"(?:于|发行日期|发布时间|发售日期)[^\n]{0,35}?(20\d{2})年\d{1,2}月", head)
        if not year:
            year = re.search(r"(20\d{2})年\d{1,2}月\d{1,2}日[^\n]{0,12}(?:发行|发布)", head)
        composer = re.search(r"(?:作曲|音乐制作|编曲)\s*[:：]\s*([^\n]{2,95})", head)
        if composer:
            credit = composer.group(1).strip().split("  ")[0].strip()
        else:
            credit = ""
        return {"year": year.group(1) if year else "", "composer": credit,
                "url": page.get("fullurl") or "https://prts.wiki/w/" + urllib.parse.quote(title.replace(" ", "_"))}
    except Exception:
        return {}


def ffmpeg_executable():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        return shutil.which("ffmpeg")


def save_ep_zip(track, audio_path, lyric_path, output_dir, compress=True,
                bitrate=256, sample_rate=44100):
    """Create the user's ep_NAME/{ep_NAME.mp3,png,lrc} bundle atomically."""
    audio_path = Path(audio_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", track["title"]).strip(" ._")[:85] or track["cid"]
    stem = "ep_" + safe.replace(" ", "_")
    destination = output_dir / (stem + ".zip")
    stage = Path(tempfile.mkdtemp(prefix="velia_ep_"))
    temporary_zip = output_dir / ("." + stem + "_" + uuid.uuid4().hex[:8] + ".part")
    try:
        target_audio = stage / (stem + (".mp3" if compress else audio_path.suffix.lower()))
        if compress:
            exe = ffmpeg_executable()
            if not exe:
                raise RuntimeError("FFmpeg not found. Install imageio-ffmpeg to enable MP3 conversion.")
            command = [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       "-i", str(audio_path), "-vn", "-codec:a", "libmp3lame",
                       "-b:a", str(int(bitrate)) + "k", "-ar", str(int(sample_rate)),
                       "-metadata", "title=" + track["title"],
                       "-metadata", "artist=" + track.get("artist", ""),
                       "-metadata", "album=" + track.get("album", ""),
                       "-metadata", "date=" + track.get("year", ""),
                       str(target_audio)]
            proc = subprocess.run(command, capture_output=True, timeout=300,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode or not target_audio.is_file():
                raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[-350:] or "MP3 encoding failed")
        else:
            shutil.copy2(audio_path, target_audio)
        if lyric_path and Path(lyric_path).is_file():
            shutil.copy2(lyric_path, stage / (stem + ".lrc"))
        else:
            (stage / (stem + ".lrc")).write_text("", encoding="utf-8")
        cover = track.get("cover_bytes")
        if not cover and track.get("cover_url"):
            cover = fetch_bytes(track["cover_url"], 5 * 1024 * 1024)
        from PySide6.QtGui import QImage
        image = QImage.fromData(cover) if cover else QImage()
        if image.isNull():
            from PySide6.QtGui import QColor
            image = QImage(512, 512, QImage.Format_ARGB32)
            image.fill(QColor("#222630"))
        image.save(str(stage / (stem + ".png")), "PNG")
        with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            for file in sorted(stage.iterdir()):
                if file.is_file() and file != temporary_zip:
                    archive.write(file, stem + "/" + file.name)
        temporary_zip.replace(destination)
        return destination
    finally:
        temporary_zip.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--enrich", metavar="TITLE", help="Look up a missing music description")
    args = parser.parse_args()
    if args.enrich:
        print(json.dumps(wiki_fallback(args.enrich), ensure_ascii=False, indent=2))
