#!/usr/bin/env python3
"""Build a contact sheet for eyeballing fetched artwork before printing.

A search can return a plausible-looking wrong album, and the only reliable
check is a human looking at the picture next to the album's name. Anything
that did not come straight off disk is flagged, and anything whose matched
title does not resemble what was searched for is flagged harder.

    python3 make_review.py --dir ./cardart-out
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from artlib import MATCH_FLOOR  # noqa: E402


def classify(entry: dict, min_px: int) -> tuple[str, str]:
    """(css class, human reason) for one manifest entry.

    The fit score is the fetcher's, not recomputed here: the two drifting
    apart would mean the page disagreed with the thing that chose the image.
    """
    status = entry.get("status")
    if status == "skipped":
        return "skipped", entry.get("reason", "skipped")
    if status != "ok":
        return "bad", "nothing found"
    if entry["width"] < min_px:
        return "bad", f"only {entry['width']}px"
    if entry.get("override"):
        return "local", "pinned by hand"
    if entry.get("source", "").startswith(("file:", "embedded:")):
        return "local", "from the library"
    if entry.get("score", 1.0) < MATCH_FLOOR:
        return "suspect", "matched title looks wrong"
    return "fetched", "from a search"


CSS = """
:root { --bg:#f6f5f3; --fg:#1b1b1b; --muted:#6a6a6a; --card:#fff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header { padding:28px 32px 8px; }
h1 { margin:0 0 4px; font-size:22px; letter-spacing:-.01em; }
.sub { color:var(--muted); }
.key { padding:0 32px 18px; color:var(--muted); font-size:13px; }
.key b { display:inline-block; width:10px; height:10px; border-radius:2px;
         margin:0 5px 0 14px; vertical-align:baseline; }
.grid { display:grid; gap:18px; padding:0 32px 48px;
        grid-template-columns:repeat(auto-fill, minmax(215px, 1fr)); }
figure { margin:0; background:var(--card); border-radius:8px; overflow:hidden;
         box-shadow:0 1px 3px rgba(0,0,0,.09); border-top:4px solid transparent; }
figure.local   { border-top-color:#9fb89f; }
figure.fetched { border-top-color:#8fa8c8; }
figure.suspect { border-top-color:#e0a63e; }
figure.bad     { border-top-color:#c9584f; }
figure.skipped { border-top-color:#bdbdbd; opacity:.55; }
img { display:block; width:100%; aspect-ratio:1; object-fit:cover;
      background:#e8e6e3; }
.none { display:flex; aspect-ratio:1; align-items:center; justify-content:center;
        background:#efeceb; color:var(--muted); font-size:13px; }
figcaption { padding:10px 12px 12px; }
.album { font-weight:600; }
.artist, .meta { color:var(--muted); font-size:12.5px; }
.meta { margin-top:6px; }
.why { margin-top:6px; font-size:12px; font-weight:600; }
figure.suspect .why { color:#a9761c; }
figure.bad .why { color:#b2453c; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#17171a; --fg:#ececec; --muted:#9a9a9a; --card:#222226; }
  img, .none { background:#2c2c31; }
}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--min-px", type=int, default=1000)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    with open(os.path.join(args.dir, "manifest.json")) as handle:
        manifest = json.load(handle)

    order = {"bad": 0, "suspect": 1, "fetched": 2, "local": 3, "skipped": 4}
    rows = []
    for path, entry in manifest.items():
        css, why = classify(entry, args.min_px)
        rows.append((order[css], path, entry, css, why))
    rows.sort(key=lambda r: (r[0], r[1]))

    counts = {}
    cards = []
    for _, path, entry, css, why in rows:
        counts[css] = counts.get(css, 0) + 1
        artist, _, album = path.partition("/")
        if entry.get("status") == "ok":
            img = f'<img src="{html.escape(entry["file"])}" loading="lazy">'
            meta = (f'{entry["width"]}&times;{entry["height"]} &middot; '
                    f'{html.escape(entry["source"].split(":", 1)[0])}'
                    f'<br>{html.escape(entry["source"].split(":", 1)[-1])}')
        else:
            img = f'<div class="none">{html.escape(why)}</div>'
            meta = "&mdash;"
        cards.append(
            f'<figure class="{css}">{img}<figcaption>'
            f'<div class="album">{html.escape(album)}</div>'
            f'<div class="artist">{html.escape(artist)}</div>'
            f'<div class="meta">{meta}</div>'
            f'<div class="why">{html.escape(why) if css in ("bad", "suspect") else ""}</div>'
            f'</figcaption></figure>')

    summary = (f'{counts.get("local", 0)} from the library, '
               f'{counts.get("fetched", 0)} fetched, '
               f'{counts.get("suspect", 0)} to check, '
               f'{counts.get("bad", 0)} unusable, '
               f'{counts.get("skipped", 0)} skipped')
    out = args.out or os.path.join(args.dir, "review.html")
    with open(out, "w") as handle:
        handle.write(
            "<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Card artwork review</title>"
            f"<style>{CSS}</style>"
            "<header><h1>Card artwork review</h1>"
            f'<div class="sub">{summary}</div></header>'
            '<div class="key">'
            '<b style="background:#c9584f"></b>unusable'
            '<b style="background:#e0a63e"></b>check the match'
            '<b style="background:#8fa8c8"></b>fetched'
            '<b style="background:#9fb89f"></b>from the library</div>'
            f'<div class="grid">{"".join(cards)}</div>')
    print(f"{out}\n{summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
