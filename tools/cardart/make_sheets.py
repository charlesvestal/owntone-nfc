#!/usr/bin/env python3
"""Lay the collected artwork out as a print-ready A4 PDF.

Geometry measured from the Affinity Publisher export that produced the
existing deck, so new cards come out the same size as the ones already in the
box: 95mm squares, six to an A4 page in two columns of three, evenly spaced.

JPEGs are embedded as-is rather than re-encoded -- a PDF can carry JPEG data
directly -- so nothing is resampled or degraded on the way through. Artwork
that is not square is centre-cropped with a clipping path, which costs no
image processing at all: the picture is scaled to cover the square and the
square is what gets painted.

    python3 make_sheets.py --dir ~/Documents/_Personal/music/cardart
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from artlib import MATCH_FLOOR  # noqa: E402

MM = 72.0 / 25.4                      # PDF points per millimetre
PAGE_W, PAGE_H = 210.0, 297.0         # A4, mm
CARD = 95.0                           # mm, measured from the existing export
COLS, ROWS = 2, 3

# The existing layout distributes the leftover space evenly: every horizontal
# gap (left margin, gutter, right margin) is the same, and likewise vertically.
GAP_X = (PAGE_W - COLS * CARD) / (COLS + 1)
GAP_Y = (PAGE_H - ROWS * CARD) / (ROWS + 1)

_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_info(data: bytes):
    """(width, height, components) from a JPEG's frame header."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in _SOF:
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height, data[i + 9]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (seglen,) = struct.unpack(">H", data[i + 2:i + 4])
        i += 2 + seglen
    raise ValueError("no JPEG frame header")


def as_jpeg(path: str) -> bytes:
    """JPEG bytes for an image file, converting only if it is not already one.

    A handful of sources serve PNG. Rather than carry an image library for the
    sake of one or two files, hand those to sips, which ships with macOS.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:2] == b"\xff\xd8":
        return data
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        out = tmp.name
    try:
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions",
                        "95", path, "--out", out],
                       check=True, capture_output=True)
        with open(out, "rb") as handle:
            return handle.read()
    finally:
        os.path.exists(out) and os.unlink(out)


class Pdf:
    """A very small PDF writer: enough for pages of embedded JPEGs."""

    def __init__(self):
        self.objects: list[bytes] = []

    def add(self, body: bytes) -> int:
        self.objects.append(body)
        return len(self.objects)          # 1-based object numbers

    def write(self, path: str, page_ids: list[int], pages_id: int, root_id: int):
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * (len(self.objects) + 1)
        for number, body in enumerate(self.objects, start=1):
            offsets[number] = len(out)
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(self.objects) + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for number in range(1, len(self.objects) + 1):
            out += f"{offsets[number]:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(self.objects) + 1} /Root {root_id} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        with open(path, "wb") as handle:
            handle.write(out)


def cover_placement(img_w: int, img_h: int, box: float):
    """Scale and offset that make the image cover a square box, centred.

    The overflow is cropped by the clipping path, so a 4:3 sleeve loses its
    left and right edges rather than being squashed -- which for album art is
    always the right call, since the label and title sit in the middle.
    """
    scale = max(box / img_w, box / img_h)
    draw_w, draw_h = img_w * scale, img_h * scale
    return draw_w, draw_h, -(draw_w - box) / 2, -(draw_h - box) / 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--min-px", type=int, default=0,
                        help="skip artwork below this width (0 = print everything)")
    parser.add_argument("--include-suspect", action="store_true",
                        help="include cards whose matched title looked wrong")
    args = parser.parse_args()

    with open(os.path.join(args.dir, "manifest.json")) as handle:
        manifest = json.load(handle)

    chosen, skipped = [], []
    for album_path, entry in sorted(manifest.items()):
        if entry.get("status") != "ok":
            skipped.append((album_path, entry.get("status", "?")))
            continue
        if args.min_px and entry["width"] < args.min_px:
            skipped.append((album_path, f"{entry['width']}px"))
            continue
        if not args.include_suspect and entry.get("score", 1.0) < MATCH_FLOOR:
            skipped.append((album_path, "questionable match"))
            continue
        chosen.append((album_path, entry))

    if not chosen:
        print("nothing to print")
        return 1

    pdf = Pdf()
    image_ids, names = [], []
    for index, (album_path, entry) in enumerate(chosen):
        data = as_jpeg(os.path.join(args.dir, entry["file"]))
        width, height, components = jpeg_info(data)
        colour = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(components)
        if colour is None:
            print(f"  ! unsupported colour ({components} components): {album_path}")
            continue
        header = (f"<< /Type /XObject /Subtype /Image /Width {width} "
                  f"/Height {height} /ColorSpace {colour} /BitsPerComponent 8 "
                  f"/Filter /DCTDecode /Length {len(data)} >>\nstream\n").encode()
        image_ids.append(pdf.add(header + data + b"\nendstream"))
        names.append((f"Im{index}", width, height))

    per_page = COLS * ROWS
    page_ids, content_ids, resource_ids = [], [], []
    for start in range(0, len(image_ids), per_page):
        batch = list(zip(image_ids[start:start + per_page],
                         names[start:start + per_page]))
        content = []
        for slot, (image_id, (name, img_w, img_h)) in enumerate(batch):
            col, row = slot % COLS, slot // COLS
            x = GAP_X + col * (CARD + GAP_X)
            # PDF's origin is bottom-left; lay out from the top of the page.
            y = PAGE_H - (GAP_Y + row * (CARD + GAP_Y) + CARD)
            box = CARD * MM
            draw_w, draw_h, dx, dy = cover_placement(img_w, img_h, box)
            content.append(
                f"q {x * MM:.3f} {y * MM:.3f} {box:.3f} {box:.3f} re W n "
                f"{draw_w:.3f} 0 0 {draw_h:.3f} {x * MM + dx:.3f} {y * MM + dy:.3f} cm "
                f"/{name} Do Q")
        stream = "\n".join(content).encode()
        content_ids.append(pdf.add(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"))
        resources = " ".join(f"/{name} {image_id} 0 R"
                             for image_id, (name, _, _) in batch)
        resource_ids.append(pdf.add(f"<< /XObject << {resources} >> >>".encode()))

    pages_id = len(pdf.objects) + len(content_ids) + 1
    for content_id, resource_id in zip(content_ids, resource_ids):
        page_ids.append(pdf.add(
            f"<< /Type /Page /Parent {pages_id} 0 R "
            f"/MediaBox [0 0 {PAGE_W * MM:.4f} {PAGE_H * MM:.4f}] "
            f"/Resources {resource_id} 0 R /Contents {content_id} 0 R >>".encode()))
    pages_id = pdf.add(
        (f"<< /Type /Pages /Count {len(page_ids)} /Kids ["
         + " ".join(f"{i} 0 R" for i in page_ids) + "] >>").encode())
    root_id = pdf.add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())

    out = args.out or os.path.join(args.dir, "covers-to-print.pdf")
    pdf.write(out, page_ids, pages_id, root_id)

    print(f"{out}")
    print(f"{len(chosen)} card(s) on {len(page_ids)} A4 page(s), "
          f"{CARD:g}mm square, {COLS}x{ROWS} per page")
    if skipped:
        print(f"\n{len(skipped)} not included:")
        for album_path, why in skipped:
            print(f"   {why:22} {album_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
