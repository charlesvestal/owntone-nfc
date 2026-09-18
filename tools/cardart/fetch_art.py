#!/usr/bin/env python3
"""Collect print-resolution cover art for albums that have no card yet.

Asks every source and picks the best answer rather than taking the first one
that replies, because the first reply is regularly the wrong album: a search
for a self-titled record will happily return a different album by the same
artist, at full resolution, looking entirely convincing. Candidates are
therefore scored on how well the matched title fits before resolution is
considered at all.

Artwork already in the library always wins when it is big enough: it cannot
be a mismatch.

    python3 fetch_art.py --out ./cardart-out
    python3 fetch_art.py --out ./cardart-out --refetch    # ignore cached work
    python3 fetch_art.py --out ./cardart-out --all        # every album

Albums that need a human decision go in overrides.json next to the images:

    {"Liquid Mike/S_T": {"artist": "Liquid Mike", "album": "Liquid Mike"},
     "Jonas Alaska/Girl": {"url": "https://.../sleeve.jpg"},
     "ZZ Test/Stereo Test": {"skip": "test disc, no card wanted"}}
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
from artlib import (CONFIDENT, MATCH_FLOOR, flac_picture,  # noqa: E402
                    image_size,
                    rank_candidates, search_terms, slug, title_match)

UA = "owntone-nfc-cardart/0.1 (+https://github.com/charlesvestal/owntone-nfc)"

# 4.5 inch cards. The existing deck runs 1000-1400px (222-311 dpi at that
# size) with a median of 1200, so 1000 is the measured floor of "looked fine
# printed" rather than a textbook number.
DEFAULT_MIN_PX = 1000

# Apple and Deezer both serve whatever size you name in the URL, up to what
# they hold. Past 3000 is wasted bytes for a 4.5in card.
WANT_PX = 3000


def http_get(url: str, timeout: float = 25.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _candidate(data, source, artist, album, searched):
    size = image_size(data)
    if not size:
        return None
    return {"data": data, "width": size[0], "height": size[1],
            "source": f"{source}:{artist} - {album}",
            "score": title_match(searched, (artist, album))}


# --- sources ---------------------------------------------------------------
#
# Each returns a list of candidates. A source that is down, rate limited or
# simply has nothing returns an empty list: no single source is allowed to
# stop the run, because the whole point is that they cover each other.


def from_library(root, album_path, searched):
    """Cover files and embedded FLAC pictures. Scored 1.0 by definition -- it
    is the album's own artwork, whatever a catalogue would have called it."""
    directory = os.path.join(root, album_path)
    if not os.path.isdir(directory):
        return []
    out = []
    names = sorted(os.listdir(directory))
    for name in names:
        if name.lower().endswith((".jpg", ".jpeg", ".png")):
            with open(os.path.join(directory, name), "rb") as handle:
                data = handle.read()
            size = image_size(data)
            if size:
                out.append({"data": data, "width": size[0], "height": size[1],
                            "source": f"file:{name}", "score": 1.0})
    for name in names:
        if name.lower().endswith(".flac"):
            found = flac_picture(os.path.join(directory, name))
            if found:
                width, height, image = found
                out.append({"data": image, "width": width, "height": height,
                            "source": f"embedded:{name}", "score": 1.0})
            break
    return out


def from_itunes(artist, album, searched):
    query = urllib.parse.quote(f"{artist} {album}")
    url = ("https://itunes.apple.com/search?media=music&entity=album&limit=5"
           f"&term={query}")
    try:
        results = json.loads(http_get(url)).get("results", [])
    except Exception as exc:                        # noqa: BLE001
        print(f"      - itunes unavailable ({exc})")
        return []
    out = []
    for result in results[:3]:
        art = result["artworkUrl100"].replace("100x100bb", f"{WANT_PX}x{WANT_PX}bb")
        try:
            data = http_get(art)
        except Exception:                           # noqa: BLE001
            continue
        found = _candidate(data, "itunes", result.get("artistName", "?"),
                           result.get("collectionName", "?"), searched)
        if found:
            out.append(found)
    return out


def from_deezer(artist, album, searched):
    query = urllib.parse.quote(f'artist:"{artist}" album:"{album}"')
    try:
        results = json.loads(
            http_get(f"https://api.deezer.com/search/album?limit=5&q={query}")
        ).get("data", [])
    except Exception as exc:                        # noqa: BLE001
        print(f"      - deezer unavailable ({exc})")
        return []
    out = []
    for result in results[:3]:
        for key in ("cover_xl", "cover_big"):
            if not result.get(key):
                continue
            try:
                data = http_get(result[key])
            except Exception:                       # noqa: BLE001
                continue
            found = _candidate(data, "deezer",
                               result.get("artist", {}).get("name", "?"),
                               result.get("title", "?"), searched)
            if found:
                out.append(found)
            break
    return out


def from_coverart_archive(artist, album, searched):
    """MusicBrainz plus the Cover Art Archive.

    Searched at *release* level as well as release-group: the archive's scans
    hang off individual releases, and a release-group lookup misses them. This
    is where Liquid Mike's self-titled album turned up at 3000px after iTunes
    had confidently offered a different record.

    Rate limited to one request a second at MusicBrainz's asking, and prone to
    503s under load, so it runs last and only when it is needed.
    """
    out = []
    for kind, field in (("release", "release"), ("release-group", "releasegroup")):
        query = urllib.parse.quote(f'artist:"{artist}" AND {field}:"{album}"')
        try:
            payload = json.loads(http_get(
                f"https://musicbrainz.org/ws/2/{kind}/?query={query}"
                "&fmt=json&limit=3"))
        except Exception as exc:                    # noqa: BLE001
            print(f"      - musicbrainz {kind} unavailable ({exc})")
            continue
        items = payload.get("releases" if kind == "release" else "release-groups", [])
        for item in items[:2]:
            time.sleep(1.1)
            try:
                data = http_get(f"https://coverartarchive.org/{kind}/{item['id']}/front")
            except Exception:                       # noqa: BLE001
                continue
            credit = item.get("artist-credit") or [{}]
            found = _candidate(data, f"caa/{kind}",
                               credit[0].get("name", artist),
                               item.get("title", "?"), searched)
            if found:
                out.append(found)
        if out:
            break
    return out


def collect(args, album_path, overrides):
    """Every candidate for one album, best first."""
    override = overrides.get(album_path, {})
    if override.get("skip"):
        return [], override

    artist, album = search_terms(album_path)
    if override.get("artist") or override.get("album"):
        artist = override.get("artist", artist)
        album = override.get("album", album)
    searched = (artist, album)

    if override.get("url"):
        data = http_get(override["url"])
        size = image_size(data)
        if size:
            return [{"data": data, "width": size[0], "height": size[1],
                     "source": f"override:{override['url']}", "score": 1.0}], override
        print("      ! override URL is not an image")
        return [], override

    candidates = from_library(args.library_root, album_path, searched)
    best_local = max((c["width"] for c in candidates), default=0)
    if best_local >= args.min_px:
        return rank_candidates(candidates, args.min_px), override
    if best_local:
        print(f"      library art is only {best_local}px; searching")

    candidates += from_itunes(artist, album, searched)
    candidates += from_deezer(artist, album, searched)

    ranked = rank_candidates(candidates, args.min_px)
    settled = (ranked and ranked[0]["score"] >= CONFIDENT
               and ranked[0]["width"] >= args.min_px)
    if not settled:
        candidates += from_coverart_archive(artist, album, searched)
        ranked = rank_candidates(candidates, args.min_px)
    return ranked, override


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--library-root", default="/srv/music")
    parser.add_argument("--out", required=True)
    parser.add_argument("--min-px", type=int, default=DEFAULT_MIN_PX)
    parser.add_argument("--all", action="store_true",
                        help="every album, not only those without a card")
    parser.add_argument("--refetch", action="store_true",
                        help="ignore images already collected")
    args = parser.parse_args()

    albums = json.loads(http_get(f"{args.api}/api/albums"))["albums"]
    targets = [a["path"] for a in albums if args.all or not a["assigned"]]
    os.makedirs(args.out, exist_ok=True)

    overrides_path = os.path.join(args.out, "overrides.json")
    overrides = {}
    if os.path.exists(overrides_path):
        with open(overrides_path) as handle:
            overrides = json.load(handle)

    manifest_path = os.path.join(args.out, "manifest.json")
    manifest = {}
    if os.path.exists(manifest_path) and not args.refetch:
        with open(manifest_path) as handle:
            manifest = json.load(handle)

    print(f"{len(targets)} album(s); want >= {args.min_px}px, "
          f"{len(overrides)} override(s)\n")
    for album_path in targets:
        image_file = os.path.join(args.out, f"{slug(album_path)}.jpg")
        cached = manifest.get(album_path, {})
        if (not args.refetch and cached.get("status") == "ok"
                and os.path.exists(image_file)
                and cached.get("override") == bool(overrides.get(album_path))):
            continue

        print(f"  {album_path}")
        ranked, override = collect(args, album_path, overrides)

        if override.get("skip"):
            print(f"      skipped ({override['skip']})")
            manifest[album_path] = {"status": "skipped",
                                    "reason": override["skip"]}
            continue
        if not ranked:
            print("      ! nothing found anywhere")
            manifest[album_path] = {"status": "missing"}
            continue

        best = ranked[0]
        with open(image_file, "wb") as handle:
            handle.write(best["data"])
        notes = []
        if best["width"] < args.min_px:
            notes.append("BELOW TARGET")
        if best["score"] < MATCH_FLOOR:
            notes.append("MATCH LOOKS WRONG")
        suffix = ("  <-- " + ", ".join(notes)) if notes else ""
        print(f"      {best['width']}x{best['height']} from {best['source']}"
              f" (fit {best['score']:.2f}){suffix}")

        manifest[album_path] = {
            "status": "ok",
            "file": os.path.basename(image_file),
            "width": best["width"],
            "height": best["height"],
            "source": best["source"],
            "score": round(best["score"], 3),
            "searched": " - ".join(search_terms(album_path)),
            "override": bool(overrides.get(album_path)),
            # Kept so the review page can offer the runner-up without a
            # second pass over the network.
            "alternates": [{"source": c["source"], "width": c["width"],
                            "score": round(c["score"], 3)} for c in ranked[1:5]],
        }
        with open(manifest_path, "w") as handle:
            json.dump(manifest, handle, indent=1, ensure_ascii=False)

    with open(manifest_path, "w") as handle:
        json.dump(manifest, handle, indent=1, ensure_ascii=False)

    ok = [v for v in manifest.values() if v.get("status") == "ok"]
    small = [v for v in ok if v["width"] < args.min_px]
    wrong = [v for v in ok if v["score"] < MATCH_FLOOR]
    missing = [k for k, v in manifest.items() if v.get("status") == "missing"]
    print(f"\n{len(ok)} image(s) in {args.out}")
    for label, group in (("below target resolution", small),
                         ("questionable match", wrong)):
        if group:
            print(f"{len(group)} {label}")
    if missing:
        print(f"{len(missing)} not found:")
        for key in missing:
            print(f"   {key}")
    print("\nNext: make_review.py --dir <out>, then edit overrides.json "
          "and re-run for anything wrong.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
