#!/usr/bin/env python3
"""Collect print-resolution cover art for albums that have no card yet.

A command-line wrapper over nfc_jukebox.cardart, which the admin page drives
too. See tools/cardart/README.md.

    python3 fetch_art.py --out ./cardart-out
    python3 fetch_art.py --out ./cardart-out --refetch    # ignore cached work
    python3 fetch_art.py --out ./cardart-out --all        # every album
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

from nfc_jukebox.cardart import (DEFAULT_MIN_PX, MATCH_FLOOR,  # noqa: E402
                                 collect_album, load_manifest, save_manifest,
                                 slug)
from nfc_jukebox.cardart.collect import load_overrides  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--library-root", default="/srv/music")
    parser.add_argument("--out", required=True)
    parser.add_argument("--min-px", type=int, default=DEFAULT_MIN_PX)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--refetch", action="store_true")
    args = parser.parse_args()

    with urllib.request.urlopen(f"{args.api}/api/albums") as response:
        albums = json.load(response)["albums"]
    targets = [a["path"] for a in albums if args.all or not a["assigned"]]

    overrides = load_overrides(args.out)
    manifest = {} if args.refetch else load_manifest(args.out)

    print(f"{len(targets)} album(s); want >= {args.min_px}px, "
          f"{len(overrides)} override(s)\n")
    for album_path in targets:
        cached = manifest.get(album_path, {})
        if (not args.refetch and cached.get("status") == "ok"
                and os.path.exists(os.path.join(args.out, f"{slug(album_path)}.jpg"))
                and cached.get("override") == bool(overrides.get(album_path))):
            continue
        print(f"  {album_path}")
        entry = collect_album(album_path, args.out, args.library_root,
                              overrides, args.min_px)
        manifest[album_path] = entry
        if entry["status"] == "ok":
            notes = []
            if entry["width"] < args.min_px:
                notes.append("BELOW TARGET")
            if entry["score"] < MATCH_FLOOR:
                notes.append("MATCH LOOKS WRONG")
            print(f"      {entry['width']}x{entry['height']} from {entry['source']}"
                  f" (fit {entry['score']:.2f})"
                  + (("  <-- " + ", ".join(notes)) if notes else ""))
        else:
            print(f"      {entry['status']}")
        save_manifest(args.out, manifest)

    save_manifest(args.out, manifest)
    ok = [v for v in manifest.values() if v.get("status") == "ok"]
    print(f"\n{len(ok)} image(s) in {args.out}")
    print("Next: make_review.py --dir <out>, or use the admin page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
