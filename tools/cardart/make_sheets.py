#!/usr/bin/env python3
"""Lay the collected artwork out as a print-ready A4 PDF.

A command-line wrapper over nfc_jukebox.cardart.sheets, which the admin page
drives too.

    python3 make_sheets.py --dir ~/Documents/_Personal/music/cardart
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

from nfc_jukebox.cardart import CARD_MM, build_sheets  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--min-px", type=int, default=0)
    parser.add_argument("--include-suspect", action="store_true")
    args = parser.parse_args()

    out = args.out or os.path.join(args.dir, "covers-to-print.pdf")
    result = build_sheets(args.dir, out, args.min_px, args.include_suspect)
    if not result["cards"]:
        print("nothing to print")
        return 1
    print(out)
    print(f"{result['cards']} card(s) on {result['pages']} A4 page(s), "
          f"{CARD_MM:g}mm square, 2x3 per page")
    if result["skipped"]:
        print(f"\n{len(result['skipped'])} not included:")
        for item in result["skipped"]:
            print(f"   {item['why']:22} {item['album']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
