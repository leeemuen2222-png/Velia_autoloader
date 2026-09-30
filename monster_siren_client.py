"""Small network client for Velia's optional Monster Siren catalogue.

Run ``python monster_siren_client.py --enrich TITLE`` to query wiki fallbacks.
No media is downloaded until a song is selected or explicitly saved.
"""

import argparse
import html
from html.parser import HTMLParser
from functools import lru_cache
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
                 artist=", ".join(a for a in (s.get("artists") or s.get("artistes") or [])
                                   if a not in ("塞壬唱片-MSR", "Monster Siren Records")) or "",
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


class _ArticleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if not self.skip and tag in ("p", "h2", "h3", "h4", "li", "tr", "td", "th", "br", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if not self.skip and tag in ("p", "h2", "h3", "tr", "td"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


class _MusicRows(HTMLParser):
    """Keep cells separate so a related event cannot be mistaken for an artist."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


@lru_cache(maxsize=1)
def _related_music_rows():
    query = urllib.parse.urlencode({'action': 'parse', 'page': '衍生作品/音乐',
                                    'prop': 'text', 'format': 'json'})
    parsed = json.loads(fetch_bytes('https://prts.wiki/api.php?' + query, 18 * 1024 * 1024))
    parser = _MusicRows()
    parser.feed(parsed.get('parse', {}).get('text', {}).get('*', ''))
    return parser.rows


def _related_music(title):
    normalize = lambda text: re.sub(r'[^\w]+', '', text, flags=re.UNICODE).casefold()
    wanted = normalize(title)
    for row in _related_music_rows():
        if len(row) < 4 or normalize(row[0]) != wanted:
            continue
        # Title | credited artist | MV character | related event | date | album.
        return {'performer': row[1] if row[1] != '-' else '',
                'character': row[2] if row[2] != '-' else '',
                'event': row[3] if row[3] != '-' else ''}
    return {}


def music_search_index():
    """Compact, exact-title PRTS character/event index for catalogue search."""
    index = {}
    normalize = lambda text: re.sub(r'[^\w]+', '', text, flags=re.UNICODE).casefold()
    for row in _related_music_rows():
        if len(row) < 4 or not row[0].strip():
            continue
        key = normalize(row[0])
        if not key:
            continue
        entry = index.setdefault(key, {'character': '', 'event': ''})
        for field, column in (('character', 2), ('event', 3)):
            value = row[column].strip()
            if value and value not in ('-', '—'):
                existing = entry[field].split('、') if entry[field] else []
                if value not in existing:
                    entry[field] = '、'.join(existing + [value])
    return index


def _wiki_gg_article(title):
    """Exact wiki.gg title only; do not attach a similar track's credits."""
    query = urllib.parse.urlencode({'action': 'query', 'titles': title,
                                    'prop': 'extracts|info', 'explaintext': 1,
                                    'inprop': 'url', 'format': 'json', 'redirects': 1})
    data = json.loads(fetch_bytes('https://arknights.wiki.gg/api.php?' + query))
    normalize = lambda text: re.sub(r'[^\w]+', '', text, flags=re.UNICODE).casefold()
    for page in (data.get('query') or {}).get('pages', {}).values():
        page_title = re.sub(r'\s*\((?:song|music)\)$', '', page.get('title', ''), flags=re.I)
        if 'missing' not in page and normalize(page_title) == normalize(title):
            return html.unescape(page.get('extract', '')), page.get('fullurl', '')
    return None


def _prts_article(title):
    """Fetch one exact PRTS article; use parse HTML when TextExtracts is absent."""
    base = "https://prts.wiki/api.php?"
    fallback_url = "https://prts.wiki/w/" + urllib.parse.quote(title.replace(" ", "_"))
    query = urllib.parse.urlencode({"action": "query", "prop": "extracts|info",
        "titles": title, "redirects": 1, "explaintext": 1,
        "inprop": "url", "format": "json"})
    normalize = lambda s: re.sub(r"[^\w]+", "", s).casefold()
    try:
        data = json.loads(fetch_bytes(base + query, 7 * 1024 * 1024))
        pages = (data.get("query") or {}).get("pages") or {}
        page = next((v for v in pages.values() if "missing" not in v
                     and normalize(v.get("title", "")) == normalize(title)), None)
        if page:
            body = html.unescape(page.get("extract") or "")
            if body and ("20" in body or "作曲" in body):
                return body, page.get("fullurl") or fallback_url
            query = urllib.parse.urlencode({"action": "parse", "page": page["title"],
                                             "prop": "text", "format": "json"})
            parsed = json.loads(fetch_bytes(base + query, 10 * 1024 * 1024))
            markup = parsed.get("parse", {}).get("text", {}).get("*", "")
        else:
            markup = ""
    except Exception:
        markup = ""
    if not markup:
        markup = fetch_bytes(fallback_url, 10 * 1024 * 1024).decode("utf-8", errors="replace")
        heading = re.search(r"<h1[^>]*>(.*?)</h1>", markup, re.I | re.S)
        if not heading:
            return None
        parser = _ArticleText()
        parser.feed(heading.group(1))
        if normalize(html.unescape("".join(parser.parts))) != normalize(title):
            return None
    parser = _ArticleText()
    parser.feed(markup)
    body = html.unescape("".join(parser.parts))
    return body, fallback_url


def _credit_value(text, names):
    """One line from a credits section, keeping roles separate."""
    pattern = r"(?:^|\n)\s*(?:" + '|'.join(names) + r")\s*[:：]\s*([^\n]{2,100})"
    match = re.search(pattern, text, re.I)
    return match.group(1).strip() if match else ''


def _real_person(value):
    """A record label/publisher is not automatically an individual creator."""
    value = re.sub(r'\s+', ' ', value).strip(' :-')
    return '' if value.casefold() in {
        '塞壬唱片', '塞壬唱片-msr', 'monster siren records', 'hypergryph',
        '鹰角网络', 'msr', 'unknown', '未知',
    } else value


def prts_credits(title, album=''):
    """Exact song/album PRTS credits, then wiki.gg and PRTS event index."""
    kind = 'ost' if re.search(r'(?:OST|原声带|Original Soundtrack)\s*$', album, re.I) else 'other'
    if re.search(r'(?:\bOP\b|片头曲)\s*$', title, re.I):
        kind = 'op'
    if re.search(r'(?:\bED\b|片尾曲)\s*$', title, re.I):
        kind = 'ed'
    result = {'year': '', 'composer': '', 'lyricist': '', 'arranger': '',
              'performer': '', 'credited_artist': '', 'character': '', 'event': '',
              'kind': kind, 'url': '', 'metadata_version': 3}
    for name in dict.fromkeys((title, album)):
        if not name:
            continue
        try:
            article = _prts_article(name)
        except Exception:
            continue
        if not article:
            continue
        body, url = article
        head = re.split(r'\n\s*(?:歌词|曲目|导航菜单)\s*\n', body, maxsplit=1)[0][:6500]
        result['url'] = result['url'] or url
        year = re.search(r'(?:于|发行日期|发布时间|发售日期)[^\n]{0,45}?(20\d{2})\s*年\s*\d{1,2}\s*月', head)
        if not year:
            year = re.search(r'(20\d{2})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日[^\n]{0,18}(?:发行|发布)', head)
        if not year:
            year = re.search(r'(?:发布时间|发行日期)\s*[:：\s]*(20\d{2})[-年/]', head)
        if year and not result['year']:
            result['year'] = year.group(1)
        if name == title:
            roles = {'composer': ('作曲', 'Composer'), 'lyricist': ('作词', 'Lyricist'),
                     'arranger': ('编曲', 'Arrangement', 'Arranger'),
                     'performer': ('演唱', '歌唱', '主唱', '演奏', 'Vocal', 'Vocals', 'Performer')}
            for key, labels in roles.items():
                result[key] = _real_person(_credit_value(head, labels))
            if re.search(r'(?:同名|单曲)\s*EP|\bEP\s*专辑', head, re.I) and kind == 'other':
                kind = 'ep'
            if re.search(r'(?:作为|是|用作)[^\n]{0,40}(?:片头曲|主题歌\s*\[?OP\]?)', head):
                kind = 'op'
            if re.search(r'(?:作为|是|用作)[^\n]{0,40}(?:片尾曲|主题歌\s*\[?ED\]?)', head):
                kind = 'ed'
        elif kind == 'other':
            if re.search(r'(?:OST|原声带|Original Soundtrack)\s*$', name, re.I):
                kind = 'ost'
            elif re.search(r'(?:同名|单曲)\s*EP|\bEP\s*专辑', head, re.I):
                kind = 'ep'

    # English wiki descriptions can identify an operator EP missed by the
    # Chinese article; retain PRTS as the primary source when it has credits.
    try:
        wiki = _wiki_gg_article(title)
    except Exception:
        wiki = None
    if wiki:
        body, wiki_url = wiki
        lead = body[:2200]
        if not result['composer']:
            match = re.search(r'compos(?:ed|er)\s+by\s+([^.,;\n]{2,65})', lead, re.I)
            if not match:
                match = re.search(r'\bComposer\s*:\s*([^\n]{2,65})', lead, re.I)
            if match:
                result['composer'] = _real_person(match.group(1))
        if not result['performer']:
            match = re.search(r'perform(?:ed)?\s+by\s+(?:the\s+)?([^.,;\n]{2,75})', lead, re.I)
            if not match:
                match = re.search(r'\b(?:Vocals|Singer|Performer)\s*:\s*([^\n]{2,75})', lead, re.I)
            if match:
                result['performer'] = _real_person(match.group(1))
        if kind == 'other' and re.search(r'(?:Operator|character)\s+EP', lead, re.I):
            kind = 'ep'
        if not result['character']:
            match = re.search(r'(?:Operator|character)\s+EP\s+of\s+([\w \-’]+)', lead, re.I)
            if match:
                result['character'] = match.group(1).strip()
        result['url'] = result['url'] or wiki_url

    try:
        related = _related_music(title)
    except Exception:
        related = {}
    if related:
        result['credited_artist'] = _real_person(related.get('performer', ''))
        result['character'] = result['character'] or related.get('character', '')
        result['event'] = related.get('event', '')
    result['kind'] = kind
    return result


def ffmpeg_executable():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        return shutil.which("ffmpeg")


def save_ep_zip(track, audio_path, lyric_path, output_dir, compress=True,
                bitrate=256, sample_rate=44100):
    """Create a classified NAME/{NAME.mp3,png,lrc} bundle atomically."""
    audio_path = Path(audio_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", track["title"]).strip(" ._")[:85] or track["cid"]
    kind = str(track.get("kind", "other")).lower()
    if kind not in ("ep", "ost", "op", "ed"):
        kind = "music"
    stem = kind + "_" + safe.replace(" ", "_")
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
                       "-metadata", "date=" + str(track.get("year", "")),
                       str(target_audio)]
            proc = subprocess.run(command, capture_output=True, timeout=300,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode or not target_audio.is_file():
                raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[-350:] or "MP3 encoding failed")
        else:
            shutil.copy2(audio_path, target_audio)
        if target_audio.suffix.lower() == ".mp3":
            from mutagen.id3 import APIC, ID3, TCON, TCOM, TALB, TIT2, TPE1, TPE2, TRCK, TXXX, TYER
            try:
                tags = ID3(str(target_audio))
            except Exception:
                tags = ID3()
            for frame in ("TIT2", "TPE1", "TPE2", "TALB", "TRCK", "TYER", "TDRC", "TCON", "TCOM", "APIC", "TXXX:SOURCE"):
                tags.delall(frame)
            tags.add(TIT2(encoding=3, text=track["title"]))
            tags.add(TPE1(encoding=3, text=track.get("artist") or ""))
            tags.add(TPE2(encoding=3, text=track.get("artist") or ""))
            tags.add(TALB(encoding=3, text=track.get("album") or ""))
            if str(track.get("track") or "").isdigit() and int(track["track"]) > 0:
                tags.add(TRCK(encoding=3, text=str(track["track"])))
            if str(track.get("year") or "").isdigit():
                tags.add(TYER(encoding=3, text=str(track["year"])))
            tags.add(TCON(encoding=3, text=kind.upper()))
            if track.get("composer"):
                tags.add(TCOM(encoding=3, text=track["composer"]))
            for field, desc in (('performer', 'PERFORMER'), ('lyricist', 'LYRICIST'),
                                ('character', 'OPERATOR'), ('event', 'RELATED_EVENT')):
                if track.get(field):
                    tags.add(TXXX(encoding=3, desc=desc, text=track[field]))
            tags.add(TXXX(encoding=3, desc="SOURCE", text=track.get("credits_url") or ROOT))
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
        if target_audio.suffix.lower() == ".mp3":
            tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover",
                          data=(stage / (stem + ".png")).read_bytes()))
            tags.save(str(target_audio), v2_version=3)
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
