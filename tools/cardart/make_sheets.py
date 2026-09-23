#!/usr/bin/env python3
"""Lay the collected artwork out as a print-ready PDF.

A command-line wrapper over nfc_jukebox.cardart.sheets, which the admin page
drives too.

    python3 make_sheets.py --dir ~/Documents/_Personal/music/cardart
    python3 make_sheets.py --dir ~/Documents/_Personal/music/cardart --paper a3
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

from nfc_jukebox.cardart import CARD_MM, LAYOUTS, build_sheets  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--min-px", type=int, default=0)
    parser.add_argument("--include-suspect", action="store_true")
    parser.add_argument("--paper", choices=sorted(LAYOUTS), default="a4",
                        help="a4: 6 a page with gaps, for home; "
                             "a3: 8 touching with bleed, for a print shop; "
                             "card: one a page, CMYK PDF/X-1a, for a print shop")
    args = parser.parse_args()

    out = args.out or os.path.join(args.dir, "covers-to-print.pdf")
    result = build_sheets(args.dir, out, args.min_px, args.include_suspect,
                          paper=args.paper)
    layout = LAYOUTS[args.paper]
    if not result["cards"]:
        print("nothing to print")
        return 1
    print(out)
    print(f"{result['cards']} card(s) on {result['pages']} "
          f"{args.paper.upper()} page(s), {CARD_MM:g}mm square, "
          f"{layout.cols}x{layout.rows} per page")
    if result["low_res"]:
        print(f"\n{len(result['low_res'])} below {layout.min_px}px "
              f"(printed anyway, not upscaled):")
        for item in result["low_res"]:
            print(f"   {item['px']:>5}px  {item['album']}")
    if result["skipped"]:
        print(f"\n{len(result['skipped'])} not included:")
        for item in result["skipped"]:
            print(f"   {item['why']:22} {item['album']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
