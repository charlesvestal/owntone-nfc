"""Laying the collected artwork out as a print-ready PDF.

Every card is a 95mm square, the size of the cards already in the box. Two
sheets are on offer:

  a4  Six to a page in two columns of three, evenly spaced, as measured from
      the Affinity Publisher export that produced the existing deck. White
      gaps between the cards and no bleed: for printing and cutting at home.
  a3  Eight to a page in two columns of four, the cards touching, with 3mm of
      bleed around the outside of the block. For a print shop.
  card
      One card to a 101mm page: the 95mm cover on the TrimBox with 3mm of
      bleed all round, in CMYK, as PDF/X-1a. For a print shop that cuts each
      card from its own page.

Bleed is always the cover mirrored outward across the cut line, so each card
is the whole cover at exactly 95mm and nothing is lost to the bleed.

JPEGs are embedded as-is rather than re-encoded -- a PDF can carry JPEG data
directly -- so nothing is resampled on the way through. Artwork that is not
square is centre-cropped with a clipping path, which costs no image processing
at all: the picture is scaled to cover the square and the square is painted.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import struct
import zlib
from dataclasses import dataclass

from .artlib import MATCH_FLOOR

MM = 72.0 / 25.4                      # PDF points per millimetre
CARD_MM = 95.0                        # mm, measured from the existing export
MARK_WEIGHT_PT = 0.25


@dataclass(frozen=True)
class Layout:
    """Where the cards go on a sheet, and how it is to be cut.

    `margin_*` is paper edge to the first card, `gutter_*` the space between
    neighbouring cards; all in mm. Cells are numbered across then down, from
    the top left.
    """
    name: str
    page_w: float
    page_h: float
    cols: int
    rows: int
    margin_x: float
    margin_y: float
    gutter_x: float
    gutter_y: float
    # Artwork mirrored outward past the edge of the block, perimeter only.
    bleed_mm: float = 0.0
    # Source images narrower than this are listed as low resolution. Flagged,
    # not dropped, and never upscaled.
    min_px: int = 0
    # PDF/X-1a: CMYK only, TrimBox and BleedBox on every page, an output
    # intent, and no marks -- the printer's imposition adds its own.
    pdfx: bool = False

    @property
    def per_page(self) -> int:
        return self.cols * self.rows

    def cell(self, slot: int) -> tuple[float, float]:
        """Bottom-left corner of a cell, in mm from the page's bottom left."""
        col, row = slot % self.cols, slot // self.cols
        x = self.margin_x + col * (CARD_MM + self.gutter_x)
        # PDF's origin is bottom-left; lay out from the top of the page.
        y = self.page_h - (self.margin_y + row * (CARD_MM + self.gutter_y)
                           + CARD_MM)
        return x, y


def _evenly(page_w: float, page_h: float, cols: int, rows: int):
    """Margins and gutters all the same size, as the existing deck has them."""
    gap_x = (page_w - cols * CARD_MM) / (cols + 1)
    gap_y = (page_h - rows * CARD_MM) / (rows + 1)
    return dict(margin_x=gap_x, margin_y=gap_y, gutter_x=gap_x, gutter_y=gap_y)


LAYOUTS = {
    # No bleed at home. Bleed means scaling the art past the cut line, and at
    # 95mm that trims text set tight to the edge while closing up the gaps. A
    # white gap is the margin; the cut goes along the edge of the art.
    "a4": Layout("a4", 210.0, 297.0, 2, 3, **_evenly(210.0, 297.0, 2, 3)),
    # A shop cuts precisely enough for the cards to touch. The outer edges
    # carry 3mm of bleed, which puts 98mm of art on a perimeter card: 1160px
    # holds 300dpi across that.
    "a3": Layout("a3", 297.0, 420.0, 2, 4,
                 margin_x=(297.0 - 2 * CARD_MM) / 2,
                 margin_y=(420.0 - 4 * CARD_MM) / 2,
                 gutter_x=0.0, gutter_y=0.0,
                 bleed_mm=3.0, min_px=1160),
    # The page is the bleed box. 95mm at 300dpi is 1122px; the bleed is
    # mirrored from the card, so it asks nothing more of the source.
    "card": Layout("card", 101.0, 101.0, 1, 1,
                   margin_x=3.0, margin_y=3.0, gutter_x=0.0, gutter_y=0.0,
                   bleed_mm=3.0, min_px=1122, pdfx=True),
}

# The A4 sheet's numbers, kept at module level for the command-line wrapper.
PAGE_W_MM, PAGE_H_MM = LAYOUTS["a4"].page_w, LAYOUTS["a4"].page_h

# Where there are gaps between cards, the marks are the cut lines continued
# through the white space, stopping this short of any artwork so a mark is
# never cut into a card. They sit between cards rather than at the paper edge,
# where most printers cannot print.
MARK_CLEAR_MM = 0.5

# Where the cards touch there is no white space inside the block, so the marks
# go in the margins: this far clear of the bled artwork, and this long.
MARGIN_MARK_OFFSET_MM = 5.0
MARGIN_MARK_LEN_MM = 5.0

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

    def write(self, path: str, page_ids: list[int], pages_id: int, root_id: int,
              version: str = "1.4", info_id: int | None = None):
        out = bytearray(f"%PDF-{version}\n".encode() + b"%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * (len(self.objects) + 1)
        for number, body in enumerate(self.objects, start=1):
            offsets[number] = len(out)
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(self.objects) + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for number in range(1, len(self.objects) + 1):
            out += f"{offsets[number]:010d} 00000 n \n".encode()
        trailer = f"/Size {len(self.objects) + 1} /Root {root_id} 0 R"
        if info_id is not None:
            # PDF/X wants a file identifier; any stable-enough bytes will do.
            ident = hashlib.md5(bytes(out[:4096]) + str(xref).encode()).hexdigest()
            trailer += f" /Info {info_id} 0 R /ID [<{ident}> <{ident}>]"
        out += (f"trailer\n<< {trailer} >>\n"
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




def _cut_lines(layout: Layout, rows_used: int):
    """The x and y of every cut, in mm, for the rows actually used."""
    xs, ys = [], []
    for col in range(layout.cols):
        x, _ = layout.cell(col)
        xs += [x, x + CARD_MM]
    for row in range(rows_used):
        _, y = layout.cell(row * layout.cols)
        ys += [y + CARD_MM, y]
    # Touching cards share a cut; the rounding only decides what counts as
    # the same line, the value kept is the unrounded one.
    def shared(values):
        return sorted({round(v, 6): v for v in values}.values())
    return shared(xs), shared(ys)


def _line(x0: float, y0: float, x1: float, y1: float) -> str:
    return f"{x0 * MM:.3f} {y0 * MM:.3f} m {x1 * MM:.3f} {y1 * MM:.3f} l S"


def _trim_marks(count: int, layout: Layout = LAYOUTS["a4"]) -> list[str]:
    """Cut marks for a page holding `count` cards.

    Only for the rows actually used, where the layout leaves them empty: a
    half-full last page should not invite a cut through empty paper. A sheet
    whose cards touch is cut as a whole block, so it is marked in full.
    """
    touching = layout.gutter_x == 0 and layout.gutter_y == 0
    rows_used = (layout.rows if touching
                 else (count + layout.cols - 1) // layout.cols)
    xs, ys = _cut_lines(layout, rows_used)
    out = [f"q 0 G {MARK_WEIGHT_PT} w"]

    if touching:
        # Margin ticks, starting clear of the bled edge and heading outward.
        left, top = layout.cell(0)[0], layout.cell(0)[1] + CARD_MM
        right = left + layout.cols * CARD_MM
        bottom = top - layout.rows * CARD_MM
        start = layout.bleed_mm + MARGIN_MARK_OFFSET_MM
        end = start + MARGIN_MARK_LEN_MM
        for x in xs:
            out.append(_line(x, top + start, x, top + end))
            out.append(_line(x, bottom - start, x, bottom - end))
        for y in ys:
            out.append(_line(left - start, y, left - end, y))
            out.append(_line(right + start, y, right + end, y))
        out.append("Q")
        return out

    # The white bands the marks run through, as (start, end) in page mm.
    # Across: left margin, gutters, right margin. Down: above each used row
    # and below the last one.
    lefts = [layout.cell(col)[0] for col in range(layout.cols)]
    bottoms = [layout.cell(row * layout.cols)[1] for row in range(rows_used)]
    across = [(0.0, lefts[0])] + [
        (left + CARD_MM, nxt) for left, nxt in zip(lefts, lefts[1:])
    ] + [(lefts[-1] + CARD_MM, layout.page_w)]
    down = [(b + CARD_MM, b + CARD_MM + layout.gutter_y) for b in bottoms] + [
        (bottoms[-1] - layout.gutter_y, bottoms[-1])]

    c = MARK_CLEAR_MM
    for x in xs:                       # vertical cuts, through the gaps down
        for lo, hi in down:
            out.append(_line(x, lo + c, x, hi - c))
    for y in ys:                       # horizontal cuts, through the gaps across
        for lo, hi in across:
            out.append(_line(lo + c, y, hi - c, y))
    out.append("Q")
    return out


def _paint(name: str, img_w: int, img_h: int, x: float, y: float,
           layout: Layout, slot: int) -> list[str]:
    """Draw one card, and mirror its edges outward where it borders the
    outside of the block.

    The card itself is exactly CARD_MM: nothing of the cover is lost to bleed.
    The bleed strip is the cover reflected across the cut line, so a guillotine
    that drifts outward shows the cover's own edge colours rather than white.
    Reflection is a transformation matrix, so this costs no image processing.
    """
    box = CARD_MM * MM
    bx, by = x * MM, y * MM
    draw_w, draw_h, dx, dy = cover_placement(img_w, img_h, box)
    place = (f"{draw_w:.3f} 0 0 {draw_h:.3f} {bx + dx:.3f} {by + dy:.3f} cm "
             f"/{name} Do")
    out = [f"q {bx:.3f} {by:.3f} {box:.3f} {box:.3f} re W n {place} Q"]

    b = layout.bleed_mm * MM
    if not b:
        return out
    col, row = slot % layout.cols, slot // layout.cols
    # (clip x, clip width, reflection matrix a, e) for each side that needs it,
    # and the same for y. None means "this axis is not mirrored".
    xs = [(bx, box, 1, 0.0)]
    if col == 0:
        xs.append((bx - b, b, -1, 2 * bx))
    if col == layout.cols - 1:
        xs.append((bx + box, b, -1, 2 * (bx + box)))
    ys = [(by, box, 1, 0.0)]
    if row == 0:
        ys.append((by + box, b, -1, 2 * (by + box)))
    if row == layout.rows - 1:
        ys.append((by - b, b, -1, 2 * by))
    for cx, cw, ax, ex in xs:
        for cy, ch, ay, ey in ys:
            if ax == 1 and ay == 1:
                continue                   # the card itself, drawn above
            out.append(f"q {cx:.3f} {cy:.3f} {cw:.3f} {ch:.3f} re W n "
                       f"{ax} 0 0 {ay} {ex:.3f} {ey:.3f} cm {place} Q")
    return out


def _render(directory: str, chosen: list, out_path: str,
            layout: Layout) -> int:
    pdf = Pdf()
    converter = None
    if layout.pdfx:
        from .cmyk import Converter
        converter = Converter(directory)
    image_ids, names = [], []
    for index, (album_path, entry) in enumerate(chosen):
        source = os.path.join(directory, entry['file'])
        data = converter.jpeg(source) if converter else as_jpeg(source)
        width, height, components = jpeg_info(data)
        colour = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(components)
        if converter and components != 4:
            colour = None
        if colour is None:
            print(f"  ! unsupported colour ({components} components): {album_path}")
            continue
        # Pillow writes CMYK JPEGs the way Photoshop does, with the Adobe
        # marker and the channels inverted; the Decode array undoes that.
        decode = (" /Decode [1 0 1 0 1 0 1 0]"
                  if components == 4 and _adobe_inverted(data) else "")
        header = (f"<< /Type /XObject /Subtype /Image /Width {width} "
                  f"/Height {height} /ColorSpace {colour}{decode} /BitsPerComponent 8 "
                  f"/Filter /DCTDecode /Length {len(data)} >>\nstream\n").encode()
        image_ids.append(pdf.add(header + data + b"\nendstream"))
        names.append((f"Im{index}", width, height))

    per_page = layout.per_page
    page_ids, content_ids, resource_ids = [], [], []
    for start in range(0, len(image_ids), per_page):
        batch = list(zip(image_ids[start:start + per_page],
                         names[start:start + per_page]))
        content = []
        # A short last page is simply left blank in the empty cells: that is
        # the padding, and there is nothing to draw for it.
        for slot, (image_id, (name, img_w, img_h)) in enumerate(batch):
            x, y = layout.cell(slot)
            content.extend(_paint(name, img_w, img_h, x, y, layout, slot))
        if not layout.pdfx:
            content.extend(_trim_marks(len(batch), layout))
        stream = "\n".join(content).encode()
        content_ids.append(pdf.add(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"))
        resources = " ".join(f"/{name} {image_id} 0 R"
                             for image_id, (name, _, _) in batch)
        resource_ids.append(pdf.add(f"<< /XObject << {resources} >> >>".encode()))

    media = f"[0 0 {layout.page_w * MM:.4f} {layout.page_h * MM:.4f}]"
    boxes = ""
    if layout.pdfx:
        # The block of cards, exactly: for one card, its 95mm square.
        left, bottom = layout.cell(layout.per_page - layout.cols)
        right = layout.cell(layout.cols - 1)[0] + CARD_MM
        top = layout.cell(0)[1] + CARD_MM
        boxes = (f" /TrimBox [{left * MM:.4f} {bottom * MM:.4f} "
                 f"{right * MM:.4f} {top * MM:.4f}] /BleedBox {media}")

    pages_id = len(pdf.objects) + len(content_ids) + 1
    for content_id, resource_id in zip(content_ids, resource_ids):
        page_ids.append(pdf.add(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox {media}{boxes} "
            f"/Resources {resource_id} 0 R /Contents {content_id} 0 R >>".encode()))
    pages_id = pdf.add(
        (f"<< /Type /Pages /Count {len(page_ids)} /Kids ["
         + " ".join(f"{i} 0 R" for i in page_ids) + "] >>").encode())

    if not layout.pdfx:
        root_id = pdf.add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
        pdf.write(out_path, page_ids, pages_id, root_id)
        return len(page_ids)

    from . import cmyk
    profile = zlib.compress(converter.profile_bytes)
    profile_id = pdf.add(
        f"<< /N 4 /Filter /FlateDecode /Length {len(profile)} >>\nstream\n".encode()
        + profile + b"\nendstream")
    intent_id = pdf.add(
        (f"<< /Type /OutputIntent /S /GTS_PDFX "
         f"/OutputConditionIdentifier ({cmyk.OUTPUT_CONDITION}) "
         f"/OutputCondition ({cmyk.OUTPUT_CONDITION_INFO}) "
         f"/Info ({cmyk.OUTPUT_CONDITION_INFO}) "
         f"/RegistryName (http://www.color.org) "
         f"/DestOutputProfile {profile_id} 0 R >>").encode())
    root_id = pdf.add(
        (f"<< /Type /Catalog /Pages {pages_id} 0 R "
         f"/OutputIntents [{intent_id} 0 R] >>").encode())
    now = datetime.datetime.now(datetime.timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
    info_id = pdf.add(
        (f"<< /Title (Record cards) /Creator (nfc-jukebox) "
         f"/Producer (nfc-jukebox) /CreationDate ({now}) /ModDate ({now}) "
         f"/Trapped /False /GTS_PDFXVersion (PDF/X-1:2001) "
         f"/GTS_PDFXConformance (PDF/X-1a:2001) >>").encode())
    # PDF/X-1a:2001 is defined on PDF 1.3, and nothing here needs later.
    pdf.write(out_path, page_ids, pages_id, root_id, version="1.3",
              info_id=info_id)
    return len(page_ids)


def _adobe_inverted(data: bytes) -> bool:
    """Does this JPEG carry an Adobe APP14 marker (so CMYK is inverted)?"""
    i = 2
    while i + 4 < len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xEE and data[i + 4:i + 9] == b"Adobe":
            return True
        if marker == 0xDA:                 # start of scan: headers are over
            return False
        (seglen,) = struct.unpack(">H", data[i + 2:i + 4])
        i += 2 + seglen
    return False


def build_sheets(directory: str, out_path: str, min_px: int = 0,
                 include_suspect: bool = False,
                 only: set[str] | None = None, paper: str = "a4") -> dict:
    """Write the print PDF. Returns what went on it and what did not.

    `only` restricts the sheet to a set of album paths, so what prints matches
    what the page is showing. Without it the manifest decides, and an album
    collected weeks ago for a different purpose turns up on the sheet.

    `paper` picks the layout from LAYOUTS. Art below that layout's resolution
    floor still prints, and is listed under "low_res" for someone to look at.
    """
    layout = LAYOUTS[paper]
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
        return {"cards": 0, "pages": 0, "skipped": skipped, "low_res": [],
                "paper": paper}

    low_res = [{"album": a, "px": min(e["width"], e.get("height", e["width"]))}
               for a, e in chosen
               if min(e["width"], e.get("height", e["width"])) < layout.min_px]
    pages = _render(directory, chosen, out_path, layout)
    return {"cards": len(chosen), "pages": pages, "paper": paper,
            "skipped": [{"album": a, "why": w} for a, w in skipped],
            "low_res": low_res}
