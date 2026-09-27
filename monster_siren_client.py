"""Small network client for Velia's optional Monster Siren catalogue.

Run ``python monster_siren_client.py --enrich TITLE`` to query wiki fallbacks.
No media is downloaded until a song is selected or explicitly saved.
"""

import argparse
import html
import json
import re
import urllib.parse
import urllib.request
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--enrich", metavar="TITLE", help="Look up a missing music description")
    args = parser.parse_args()
    if args.enrich:
        print(json.dumps(wiki_fallback(args.enrich), ensure_ascii=False, indent=2))
