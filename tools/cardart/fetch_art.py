#!/usr/bin/env python3
"""Collect print-resolution cover art for albums that have no card yet.

Prefers the artwork already in the library, because it is guaranteed to be
the right album. Only when that is missing or too small for print does it go
looking online, where a search can always return the wrong record -- so every
fetched image is recorded in a manifest with the title that was actually
matched, for review before anything is printed.

    python3 fetch_art.py --out ./cardart-out
    python3 fetch_art.py --out ./cardart-out --all       # not just unassigned
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from artlib import flac_picture, image_size, search_terms, slug  # noqa: E402

UA = "owntone-nfc-cardart/0.1 (+https://github.com/charlesvestal/owntone-nfc)"

# 4.5 inch cards. The existing deck runs 1000-1400px (222-311 dpi at that
# size) with a median of 1200, so 1000 is the measured floor of "looked fine
# printed" rather than a textbook number.
DEFAULT_MIN_PX = 1000

# Apple serves whatever size you ask for in the filename. 3000 is past the
# point of diminishing returns for a 4.5in card and still a small download.
ITUNES_SIZE = 3000


def http_get(url: str, timeout: float = 25.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def local_art(root: str, album_path: str) -> tuple[bytes, tuple[int, int], str] | None:
    """Best artwork already on disk for this album: a cover file, or one
    embedded in the first FLAC."""
    directory = os.path.join(root, album_path)
    if not os.path.isdir(directory):
        return None
    names = sorted(os.listdir(directory))

    best = None
    for name in names:
        if name.lower().endswith((".jpg", ".jpeg", ".png")):
            with open(os.path.join(directory, name), "rb") as handle:
                data = handle.read()
            size = image_size(data)
            if size and (best is None or size[0] > best[1][0]):
                best = (data, size, f"file:{name}")
    if best:
        return best

    for name in names:
        if name.lower().endswith(".flac"):
            found = flac_picture(os.path.join(directory, name))
            if found:
                width, height, image = found
                return image, (width, height), f"embedded:{name}"
    return None


def itunes_art(artist: str, album: str) -> tuple[bytes, tuple[int, int], str] | None:
    """Search the iTunes catalogue and take the front cover of the best match.

    Returns the matched collection name alongside the image so a wrong album
    is visible in the manifest instead of turning up on a printed card.
    """
    query = urllib.parse.quote(f"{artist} {album}")
    url = ("https://itunes.apple.com/search?media=music&entity=album&limit=5"
           f"&term={query}")
    try:
        results = json.loads(http_get(url)).get("results", [])
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        print(f"      ! iTunes search failed: {exc}")
        return None

    for result in results:
        art_url = result["artworkUrl100"].replace(
            "100x100bb", f"{ITUNES_SIZE}x{ITUNES_SIZE}bb")
        try:
            data = http_get(art_url)
        except Exception:                         # noqa: BLE001
            continue
        size = image_size(data)
        if not size:
            continue
        matched = f"{result.get('artistName', '?')} - {result.get('collectionName', '?')}"
        return data, size, f"itunes:{matched}"
    return None


def coverart_archive(artist: str, album: str) -> tuple[bytes, tuple[int, int], str] | None:
    """MusicBrainz release-group lookup, then the Cover Art Archive front
    image. Slower and rate limited, so only used when iTunes has nothing."""
    query = urllib.parse.quote(f'artist:"{artist}" AND releasegroup:"{album}"')
    url = f"https://musicbrainz.org/ws/2/release-group/?query={query}&fmt=json&limit=3"
    try:
        groups = json.loads(http_get(url)).get("release-groups", [])
    except Exception as exc:                      # noqa: BLE001
        print(f"      ! MusicBrainz lookup failed: {exc}")
        return None
    for group in groups:
        time.sleep(1.1)                           # MusicBrainz asks for 1 req/sec
        try:
            data = http_get(
                f"https://coverartarchive.org/release-group/{group['id']}/front")
        except Exception:                         # noqa: BLE001
            continue
        size = image_size(data)
        if size:
            return data, size, f"caa:{group.get('title', '?')}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8080",
                        help="jukebox admin API base URL")
    parser.add_argument("--library-root", default="/srv/music")
    parser.add_argument("--out", required=True, help="directory to write into")
    parser.add_argument("--min-px", type=int, default=DEFAULT_MIN_PX)
    parser.add_argument("--all", action="store_true",
                        help="every album, not only those without a card")
    parser.add_argument("--refetch", action="store_true",
                        help="ignore images already in --out")
    args = parser.parse_args()

    albums = json.loads(http_get(f"{args.api}/api/albums"))["albums"]
    targets = [a["path"] for a in albums if args.all or not a["assigned"]]
    os.makedirs(args.out, exist_ok=True)

    manifest_path = os.path.join(args.out, "manifest.json")
    manifest = {}
    if os.path.exists(manifest_path) and not args.refetch:
        with open(manifest_path) as handle:
            manifest = json.load(handle)

    print(f"{len(targets)} album(s); want >= {args.min_px}px\n")
    for album_path in targets:
        name = slug(album_path)
        image_file = os.path.join(args.out, f"{name}.jpg")
        if not args.refetch and album_path in manifest and os.path.exists(image_file):
            continue

        print(f"  {album_path}")
        artist, album = search_terms(album_path)

        found = local_art(args.library_root, album_path)
        if found and found[1][0] < args.min_px:
            print(f"      local art is only {found[1][0]}px; looking online")
            found = None

        if not found:
            found = itunes_art(artist, album)
            if not found:
                found = coverart_archive(artist, album)
            time.sleep(0.3)

        if not found:
            print("      ! nothing found")
            manifest[album_path] = {"status": "missing"}
            continue

        data, (width, height), source = found
        with open(image_file, "wb") as handle:
            handle.write(data)
        flag = "" if width >= args.min_px else "  <-- BELOW TARGET"
        print(f"      {width}x{height} from {source}{flag}")
        manifest[album_path] = {
            "status": "ok",
            "file": os.path.basename(image_file),
            "width": width,
            "height": height,
            "source": source,
            "searched": f"{artist} - {album}",
        }

    with open(manifest_path, "w") as handle:
        json.dump(manifest, handle, indent=1, ensure_ascii=False)

    ok = [v for v in manifest.values() if v.get("status") == "ok"]
    small = [v for v in ok if v["width"] < args.min_px]
    missing = [k for k, v in manifest.items() if v.get("status") != "ok"]
    print(f"\n{len(ok)} image(s) in {args.out}")
    if small:
        print(f"{len(small)} below {args.min_px}px")
    if missing:
        print(f"{len(missing)} not found:")
        for key in missing:
            print(f"   {key}")
    online = [v for v in ok if not v["source"].startswith(("file:", "embedded:"))]
    if online:
        print(f"\n{len(online)} came from a search and should be eyeballed "
              f"(see review.html)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
