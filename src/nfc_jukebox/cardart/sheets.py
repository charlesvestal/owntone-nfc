"""Laying the collected artwork out as a print-ready A4 PDF.

Geometry measured from the Affinity Publisher export that produced the
existing deck, so new cards come out the same size as the ones already in the
box: 95mm squares, six to an A4 page in two columns of three, evenly spaced.

JPEGs are embedded as-is rather than re-encoded -- a PDF can carry JPEG data
directly -- so nothing is resampled on the way through. Artwork that is not
square is centre-cropped with a clipping path, which costs no image processing
at all: the picture is scaled to cover the square and the square is painted.
"""
from __future__ import annotations

import io
import json
import os
import struct

from .artlib import MATCH_FLOOR

MM = 72.0 / 25.4                      # PDF points per millimetre
PAGE_W_MM, PAGE_H_MM = 210.0, 297.0         # A4, mm
CARD_MM = 95.0                           # mm, measured from the existing export
COLS, ROWS = 2, 3

# The existing layout distributes the leftover space evenly: every horizontal
# gap (left margin, gutter, right margin) is the same, and likewise vertically.
GAP_X = (PAGE_W_MM - COLS * CARD_MM) / (COLS + 1)
GAP_Y = (PAGE_H_MM - ROWS * CARD_MM) / (ROWS + 1)

# How far the artwork runs past the cut line. A guillotine drifts a fraction of
# a millimetre, and without bleed that drift shows as a white hairline down one
# edge of the card. The art is centre-cropped to the square anyway, so the
# extra is free.
#
# Capped by the layout rather than chosen: the vertical gap is 3mm, so 1.5mm is
# the most either neighbour can take before one album's artwork prints over the
# next one's. Printers usually ask for 3mm; this sheet cannot give it without
# dropping to four cards a page, and 1.5mm is enough for a guillotine.
BLEED_MM = min(GAP_X, GAP_Y) / 2

# Ticks at the sheet edges rather than corner crop marks: at 1.5mm of clear
# margin there is no room for the usual offset-and-length pair, and a guillotine
# operator lines up on the sheet edge regardless.
MARK_LEN_MM = 1.4
MARK_WEIGHT_PT = 0.25

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

    JPEGs are returned byte-for-byte -- the point of this module is that
    nothing is resampled on the way into the PDF -- so this only does work for
    the handful of sources that serve PNG.

    That conversion was a call to `sips` until 2026-09-19. sips ships with
    macOS and exists nowhere else, so on the Pi it raised FileNotFoundError and
    the sheets endpoint returned a 500. Pillow costs a dependency and works on
    both.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:2] == b"\xff\xd8":
        return data

    from PIL import Image

    buffer = io.BytesIO()
    with Image.open(io.BytesIO(data)) as source:
        image = source
        if source.mode in ("RGBA", "LA", "P"):
            # JPEG has no alpha channel. Compose onto white, or every
            # transparent region prints black.
            rgba = source.convert("RGBA")
            image = Image.new("RGB", rgba.size, (255, 255, 255))
            image.paste(rgba, mask=rgba.split()[-1])
        elif source.mode != "RGB":
            image = source.convert("RGB")
        image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


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




def _trim_marks(count: int) -> list[str]:
    """Ticks in the sheet margins, one pair per cut line.

    Only for the rows actually used: a half-full last page should not invite a
    cut through empty paper.
    """
    rows_used = (count + COLS - 1) // COLS
    xs, ys = [], []
    for col in range(COLS):
        left = GAP_X + col * (CARD_MM + GAP_X)
        xs += [left, left + CARD_MM]
    for row in range(rows_used):
        top = PAGE_H_MM - (GAP_Y + row * (CARD_MM + GAP_Y))
        ys += [top, top - CARD_MM]

    out = [f"q 0 G {MARK_WEIGHT_PT} w"]
    for x in xs:                       # vertical cuts: tick top and bottom
        for y0, y1 in ((0, MARK_LEN_MM),
                       (PAGE_H_MM - MARK_LEN_MM, PAGE_H_MM)):
            out.append(f"{x * MM:.3f} {y0 * MM:.3f} m "
                       f"{x * MM:.3f} {y1 * MM:.3f} l S")
    for y in ys:                       # horizontal cuts: tick left and right
        for x0, x1 in ((0, MARK_LEN_MM),
                       (PAGE_W_MM - MARK_LEN_MM, PAGE_W_MM)):
            out.append(f"{x0 * MM:.3f} {y * MM:.3f} m "
                       f"{x1 * MM:.3f} {y * MM:.3f} l S")
    out.append("Q")
    return out


def _render(directory: str, chosen: list, out_path: str) -> int:
    pdf = Pdf()
    image_ids, names = [], []
    for index, (album_path, entry) in enumerate(chosen):
        data = as_jpeg(os.path.join(directory, entry['file']))
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
            x = GAP_X + col * (CARD_MM + GAP_X)
            # PDF's origin is bottom-left; lay out from the top of the page.
            y = PAGE_H_MM - (GAP_Y + row * (CARD_MM + GAP_Y) + CARD_MM)
            # Paint the bleed box, clip to the bleed box: the card is cut out
            # of the middle of it at CARD_MM.
            box = (CARD_MM + 2 * BLEED_MM) * MM
            bx, by = (x - BLEED_MM) * MM, (y - BLEED_MM) * MM
            draw_w, draw_h, dx, dy = cover_placement(img_w, img_h, box)
            content.append(
                f"q {bx:.3f} {by:.3f} {box:.3f} {box:.3f} re W n "
                f"{draw_w:.3f} 0 0 {draw_h:.3f} {bx + dx:.3f} {by + dy:.3f} cm "
                f"/{name} Do Q")
        content.extend(_trim_marks(len(batch)))
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
            f"/MediaBox [0 0 {PAGE_W_MM * MM:.4f} {PAGE_H_MM * MM:.4f}] "
            f"/Resources {resource_id} 0 R /Contents {content_id} 0 R >>".encode()))
    pages_id = pdf.add(
        (f"<< /Type /Pages /Count {len(page_ids)} /Kids ["
         + " ".join(f"{i} 0 R" for i in page_ids) + "] >>").encode())
    root_id = pdf.add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    pdf.write(out_path, page_ids, pages_id, root_id)
    return len(page_ids)


def build_sheets(directory: str, out_path: str, min_px: int = 0,
                 include_suspect: bool = False,
                 only: set[str] | None = None) -> dict:
    """Write the print PDF. Returns what went on it and what did not.

    `only` restricts the sheet to a set of album paths, so what prints matches
    what the page is showing. Without it the manifest decides, and an album
    collected weeks ago for a different purpose turns up on the sheet.
    """
    with open(os.path.join(directory, "manifest.json")) as handle:
        manifest = json.load(handle)

    chosen, skipped = [], []
    for album_path, entry in sorted(manifest.items()):
        if only is not None and album_path not in only:
            continue
        if entry.get("status") != "ok":
            skipped.append((album_path, entry.get("status", "?")))
        elif min_px and entry["width"] < min_px:
            skipped.append((album_path, f"{entry['width']}px"))
        elif not include_suspect and entry.get("score", 1.0) < MATCH_FLOOR:
            skipped.append((album_path, "questionable match"))
        else:
            chosen.append((album_path, entry))

    if not chosen:
        return {"cards": 0, "pages": 0, "skipped": skipped}

    pages = _render(directory, chosen, out_path)
    return {"cards": len(chosen), "pages": pages,
            "skipped": [{"album": a, "why": w} for a, w in skipped]}
