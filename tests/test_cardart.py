"""Tests for the cover-art tooling.

Only the pure parts: header parsing, turning a library path into search terms,
and the scoring that decides which candidate gets printed. The network sources
are deliberately untested here -- they are thin wrappers whose real failure
mode is a remote service changing, which a mock would not catch.

The scoring cases are all real results this library produced, including the
ones that were wrong.
"""
import os
import struct
import sys
import time

import pytest

from nfc_jukebox.cardart import (CONFIDENT, MATCH_FLOOR, image_size,
                                 jpeg_size, png_size, rank_candidates,
                                 search_terms, slug, title_match)


# --- image headers ---------------------------------------------------------


def _jpeg(width: int, height: int, *, preamble: bytes = b"") -> bytes:
    """A JPEG header with `preamble` bytes of other segments before the frame."""
    # Length, sample precision, height, width -- the precision byte sits
    # between the length and the dimensions and is easy to forget.
    sof = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, height, width) + b"\x03" * 9
    return b"\xff\xd8" + preamble + sof


def test_jpeg_dimensions_are_read_from_the_frame_header():
    assert jpeg_size(_jpeg(1400, 1400)) == (1400, 1400)


def test_jpeg_dimensions_are_found_past_earlier_segments():
    """Real files put EXIF and quantisation tables before the frame, so the
    parser has to walk the segment chain rather than read a fixed offset."""
    exif = b"\xff\xe1" + struct.pack(">H", 100) + b"\x00" * 98
    assert jpeg_size(_jpeg(3000, 3000, preamble=exif)) == (3000, 3000)


def test_a_stray_ff_byte_does_not_look_like_a_frame():
    """0xFF is an ordinary data byte, and scanning for it rather than
    following segment lengths finds imaginary frames inside the scan data."""
    decoy = b"\xff\xdb" + struct.pack(">H", 8) + b"\xff\xc0\xff\xc0\xff\xc0"
    assert jpeg_size(_jpeg(1200, 1200, preamble=decoy)) == (1200, 1200)


def test_non_jpeg_data_is_not_guessed_at():
    assert jpeg_size(b"not an image at all") is None
    assert image_size(b"") is None


def test_png_dimensions():
    header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 900, 900)
    assert png_size(header) == (900, 900)
    assert image_size(header) == (900, 900)


def test_a_truncated_jpeg_returns_nothing_rather_than_raising():
    """Only the first chunk of a large file is read when auditing, so the
    frame header is often simply not there yet."""
    assert jpeg_size(b"\xff\xd8\xff\xe0\x00") is None


# --- library paths to search terms ----------------------------------------


@pytest.mark.parametrize("path,expected", [
    # An underscore before a space stood for a colon.
    ("Various Artists/Patriot_ Season 1 EP",
     ("Various Artists", "Patriot: Season 1 EP")),
    # Between two characters it stood for a slash...
    ("Liquid Mike/S_T", ("Liquid Mike", "Liquid Mike")),
    # ...and at the end, for something a search does not want anyway.
    ("Carlton Jumel Smith/1634 Lexington Ave_",
     ("Carlton Jumel Smith", "1634 Lexington Ave")),
    # Edition markers narrow a search unhelpfully; the artwork is the same.
    ("Alan Braxe/The Upper Cuts (2023 Edition)",
     ("Alan Braxe", "The Upper Cuts")),
    ("Joey Valence and Brae/NO HANDS (deluxe edition)",
     ("Joey Valence and Brae", "NO HANDS")),
    # Typographic quotes read fine to humans and badly to search APIs.
    ("Fatboy Slim/You’ve Come a Long Way, Baby",
     ("Fatboy Slim", "You've Come a Long Way, Baby")),
])
def test_search_terms(path, expected):
    assert search_terms(path) == expected


def test_a_self_titled_album_searches_for_the_artists_name():
    """"S/T" is not a title anyone catalogues; the release is named after the
    artist, and searching for the literal string finds nothing."""
    assert search_terms("Liquid Mike/S_T") == ("Liquid Mike", "Liquid Mike")


def test_slug_is_a_safe_flat_filename():
    """Punctuation the filesystem dislikes is dropped rather than translated.
    The result only has to be stable and unique -- it names a file, and
    changing the rule would orphan every image already downloaded."""
    assert slug("Harry Styles/Harry’s House") == "Harry Styles - Harrys House"
    assert "/" not in slug("Various Artists/Patriot_ Season 1 (An Amazon Original)")


# --- match scoring ---------------------------------------------------------
#
# Every pair below is a real search result from this library.


@pytest.mark.parametrize("searched,matched", [
    # Exact.
    (("Paul Simon", "Graceland"), ("Paul Simon", "Graceland")),
    # A deluxe reissue carries the same cover.
    (("Vampire Weekend", "Father of the Bride"),
     ("Vampire Weekend", "Father of the Bride (Deluxe)")),
    # Catalogue titles carry qualifiers the library does not.
    (("Vince Guaraldi", '"A Charlie Brown Christmas" Featuring the Famous '
                        'Peanuts Characters: Original Soundtrack'),
     ("Vince Guaraldi Trio", "A Charlie Brown Christmas "
                             "(Original 1965 TV Soundtrack) [Expanded Edition]")),
    (("CAKE", "Motorcade of Generosity"), ("CAKE", "Motorcade Of Generosity")),
])
def test_a_real_match_clears_the_floor(searched, matched):
    assert title_match(searched, matched) >= MATCH_FLOOR


@pytest.mark.parametrize("searched,matched", [
    # The one that started this: a different album by the right artist,
    # returned for a self-titled search.
    (("Liquid Mike", "Liquid Mike"), ("Liquid Mike", "Paul Bunyan's Slingshot")),
    (("Non-Album", "The State of Samuel"),
     ("The Ohio State University Buckeye Marching Band",
      "The Ohio State University Marching Band-New Era")),
    (("Various Artists", "Patriot: Season 2 (Amazon Original Soundtrack)"),
     ("Various Artists", "Patriotism In Melody - A Tribute To Netaji "
                         "Subhas Chandra Bose")),
])
def test_a_wrong_album_falls_below_the_floor(searched, matched):
    assert title_match(searched, matched) < MATCH_FLOOR


def test_the_artist_alone_cannot_carry_a_self_titled_match():
    """Scoring artist and album as one string let any album by the right
    artist score highly for a self-titled search, because the artist name
    satisfied the entire query."""
    right = title_match(("Liquid Mike", "Liquid Mike"),
                        ("Liquid Mike", "Liquid Mike"))
    wrong = title_match(("Liquid Mike", "Liquid Mike"),
                        ("Liquid Mike", "Paul Bunyan's Slingshot"))
    assert right > wrong + 0.4


def test_a_merely_plausible_match_is_not_confident():
    """Clearing the floor means "probably not a different album", which is a
    weaker claim than "this is the album" -- and only the latter is grounds to
    stop asking other sources."""
    score = title_match(("Liquid Mike", "Liquid Mike"),
                        ("Heavenly Sweetheart", "$300 (feat. Liquid Mike)"))
    assert MATCH_FLOOR <= score < CONFIDENT


# --- ranking ---------------------------------------------------------------


def _candidate(source, width, score):
    return {"source": source, "width": width, "score": score}


def test_a_confident_match_beats_a_sharper_doubtful_one():
    """The regression that printed the wrong sleeve: ranking on resolution
    once both candidates cleared the floor."""
    ranked = rank_candidates([_candidate("deezer:wrong", 4000, 0.60),
                              _candidate("caa/release:right", 3000, 1.0)], 1000)
    assert ranked[0]["source"] == "caa/release:right"


def test_resolution_still_decides_between_equally_good_matches():
    ranked = rank_candidates([_candidate("itunes:a", 1200, 1.0),
                              _candidate("caa/release:b", 3000, 1.0)], 1000)
    assert ranked[0]["width"] == 3000


def test_the_library_wins_a_tie_because_it_cannot_be_a_mismatch():
    ranked = rank_candidates([_candidate("itunes:a", 1400, 1.0),
                              _candidate("file:cover.jpg", 1400, 1.0)], 1000)
    assert ranked[0]["source"] == "file:cover.jpg"


def test_an_unprintable_image_of_the_right_album_still_beats_a_wrong_one():
    """Both are unusable, but only one is fixable by finding a bigger copy of
    the same record, so it is the one worth showing on the review sheet."""
    ranked = rank_candidates([_candidate("itunes:wrong", 3000, 0.2),
                              _candidate("caa/release:right", 500, 1.0)], 1000)
    assert ranked[0]["source"] == "caa/release:right"


def test_print_resolution_outranks_sharpness_of_a_doubtful_match():
    ranked = rank_candidates([_candidate("deezer:right", 1000, 0.9),
                              _candidate("itunes:wrong", 900, 0.9)], 1000)
    assert ranked[0]["width"] == 1000


# --- converting sources that are not JPEG ----------------------------------
#
# The print sheets embed JPEG data directly, so anything arriving as PNG has to
# be converted first. This used to shell out to `sips`, which exists only on
# macOS -- on the Pi that raised FileNotFoundError and the sheets endpoint
# returned a 500.


def _png(path, mode="RGB", size=(8, 8), colour=(10, 20, 30)):
    from PIL import Image
    Image.new(mode, size, colour).save(path, format="PNG")
    return str(path)


def test_a_jpeg_source_is_returned_untouched(tmp_path):
    """Nothing is re-encoded on the way into the PDF."""
    from nfc_jukebox.cardart.sheets import as_jpeg
    data = _jpeg(600, 600)
    path = tmp_path / "cover.jpg"
    path.write_bytes(data)
    assert as_jpeg(str(path)) == data


def test_a_png_source_is_converted_to_jpeg(tmp_path):
    from nfc_jukebox.cardart.sheets import as_jpeg
    out = as_jpeg(_png(tmp_path / "cover.png"))
    assert out[:2] == b"\xff\xd8", "expected JPEG data"
    assert image_size(out) == (8, 8)


def test_a_transparent_png_flattens_onto_white_not_black(tmp_path):
    """JPEG has no alpha. Transparent corners must not come out black."""
    from PIL import Image
    from nfc_jukebox.cardart.sheets import as_jpeg
    path = tmp_path / "cover.png"
    Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(path, format="PNG")
    with Image.open(__import__("io").BytesIO(as_jpeg(str(path)))) as img:
        assert img.convert("RGB").getpixel((0, 0)) == (255, 255, 255)


# --- the manifest is written atomically -------------------------------------


def test_a_manifest_is_never_readable_half_written(tmp_path):
    """Two writers and a reader share this file: the collector saves after
    every album, and a manifest GET prunes albums whose folder has gone. A
    plain open(..., 'w') truncates in place, so a reader can catch a partial
    file and blow up on json.load."""
    import json
    import threading
    from nfc_jukebox.cardart.collect import load_manifest, save_manifest

    big = {f"Artist {i}/Album {i}": {"status": "ok", "file": f"{i}.jpg",
                                     "width": 3000, "height": 3000}
           for i in range(400)}
    save_manifest(str(tmp_path), big)

    errors = []
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            save_manifest(str(tmp_path), big)

    def reader():
        while not stop.is_set():
            try:
                load_manifest(str(tmp_path))
            except (json.JSONDecodeError, ValueError) as exc:
                errors.append(exc)
                return

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    time.sleep(0.4)
    stop.set()
    for t in threads:
        t.join()

    assert not errors, f"read a half-written manifest: {errors[0]}"


# --- card size and trim marks -----------------------------------------------
#
# Every card is exactly 95mm, so nothing of the cover is lost to the cut. On A4
# the cards have white gaps and the marks continue each cut line through them;
# on A3 the cards touch, the outside of the block carries mirrored bleed, and
# the marks sit in the margins.


def _cards(layout):
    from nfc_jukebox.cardart import sheets
    return [(x, y, x + sheets.CARD_MM, y + sheets.CARD_MM)
            for x, y in map(layout.cell, range(layout.per_page))]


def _mark_points(layout, count):
    import re
    from nfc_jukebox.cardart import sheets
    for line in sheets._trim_marks(count, layout)[1:-1]:
        x0, y0, x1, y1 = (float(v) / sheets.MM for v in re.findall(r"-?[\d.]+", line))
        for t in (0, 0.5, 1):
            yield line, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t


@pytest.mark.parametrize("paper", ["a4", "a3"])
def test_trim_marks_stay_out_of_the_artwork(paper):
    """A mark drawn over the artwork -- or its bleed -- would be cut into a
    card."""
    from nfc_jukebox.cardart import sheets
    layout = sheets.LAYOUTS[paper]
    b = layout.bleed_mm
    for line, px, py in _mark_points(layout, layout.per_page):
        for cx0, cy0, cx1, cy1 in _cards(layout):
            assert not (cx0 - b < px < cx1 + b and cy0 - b < py < cy1 + b), line


def test_a3_matches_the_print_spec():
    from nfc_jukebox.cardart import sheets
    a3 = sheets.LAYOUTS["a3"]
    assert (a3.page_w, a3.page_h, a3.per_page) == (297, 420, 8)
    xs = sorted({round(c[0], 3) for c in _cards(a3)})
    ys = sorted({round(c[1], 3) for c in _cards(a3)})
    assert xs == [53.5, 148.5]
    assert ys == [20, 115, 210, 305]
    cut_x, cut_y = sheets._cut_lines(a3, a3.rows)
    assert cut_x == [53.5, 148.5, 243.5]
    assert cut_y == [20, 115, 210, 305, 400]


def test_a3_marks_start_five_mm_clear_of_the_bleed():
    from nfc_jukebox.cardart import sheets
    a3 = sheets.LAYOUTS["a3"]
    clear = a3.bleed_mm + sheets.MARGIN_MARK_OFFSET_MM
    for line, px, py in _mark_points(a3, 3):
        outside_x = min(abs(px - 53.5), abs(px - 243.5)) if not 53.5 < px < 243.5 else 0
        outside_y = min(abs(py - 20), abs(py - 400)) if not 20 < py < 400 else 0
        assert max(outside_x, outside_y) >= clear - 1e-2, line


def test_a3_is_marked_in_full_even_on_a_short_page():
    """The block is cut as a whole, so the empty padding cells get marks too."""
    from nfc_jukebox.cardart import sheets
    a3 = sheets.LAYOUTS["a3"]
    assert sheets._trim_marks(1, a3) == sheets._trim_marks(8, a3)


def _one_card_pdf(tmp_path, paper="a4", px=3000):
    import json
    from PIL import Image
    from nfc_jukebox.cardart import build_sheets
    art = tmp_path / "art"
    art.mkdir()
    Image.new("RGB", (px, px), (30, 60, 90)).save(art / "a.jpg", "JPEG")
    (art / "manifest.json").write_text(json.dumps(
        {"A/B": {"status": "ok", "file": "a.jpg", "width": px,
                 "height": px, "score": 1.0}}))
    out = str(tmp_path / "sheet.pdf")
    result = build_sheets(str(art), out, 0, True, paper=paper)
    return result, pathlib.Path(out).read_bytes()


def test_a_sheet_carries_trim_marks(tmp_path):
    import pathlib
    globals()["pathlib"] = pathlib
    result, pdf = _one_card_pdf(tmp_path)
    assert result["cards"] == 1
    # Stroked paths exist only for the marks; the artwork is painted, not drawn.
    assert b" S\n" in pdf or b" S " in pdf or pdf.count(b"\nS\n") > 0


def test_the_artwork_is_drawn_at_exactly_the_card_size(tmp_path):
    """No bleed: scaling the art past the cut line trims text off its edges."""
    import pathlib
    globals()["pathlib"] = pathlib
    from nfc_jukebox.cardart import sheets
    _, pdf = _one_card_pdf(tmp_path)
    card_pt = sheets.CARD_MM * sheets.MM
    assert f"{card_pt:.3f} {card_pt:.3f} re W n".encode() in pdf


def test_a3_mirrors_the_outside_edges_and_keeps_the_card_whole(tmp_path):
    """The card is still exactly 95mm; the bleed is extra, reflected outward.
    A lone card in the top-left cell borders the outside on its left and top,
    so it gets a left strip, a top strip and the corner between them."""
    import pathlib
    globals()["pathlib"] = pathlib
    from nfc_jukebox.cardart import sheets
    _, pdf = _one_card_pdf(tmp_path, paper="a3")
    card_pt = sheets.CARD_MM * sheets.MM
    assert f"{card_pt:.3f} {card_pt:.3f} re W n".encode() in pdf
    assert pdf.count(b"-1 0 0 1 ") == 1      # left
    assert pdf.count(b"1 0 0 -1 ") == 2      # top, and the corner ("-1 0 0 -1")
    assert pdf.count(b"-1 0 0 -1 ") == 1     # the corner


def test_a3_flags_low_resolution_art_without_dropping_it(tmp_path):
    result, _ = _one_card_pdf(tmp_path, paper="a3", px=1000)
    assert result["cards"] == 1
    assert result["low_res"] == [{"album": "A/B", "px": 1000}]


def test_a3_pads_to_whole_pages(tmp_path):
    """220 cards is 27.5 pages of eight, so 28 pages."""
    import json
    from PIL import Image
    from nfc_jukebox.cardart import build_sheets
    art = tmp_path / "art"
    art.mkdir()
    Image.new("RGB", (64, 64)).save(art / "a.jpg", "JPEG")
    (art / "manifest.json").write_text(json.dumps(
        {f"A/{i:03}": {"status": "ok", "file": "a.jpg", "width": 1200,
                       "height": 1200, "score": 1.0} for i in range(220)}))
    result = build_sheets(str(art), str(tmp_path / "s.pdf"), 0, True, paper="a3")
    assert (result["cards"], result["pages"]) == (220, 28)


# --- one card a page, CMYK PDF/X-1a ------------------------------------------


@pytest.fixture
def pso_profile(tmp_path, monkeypatch):
    """A stand-in CMYK profile, so the tests never go to eci.org."""
    from PIL import ImageCms
    from nfc_jukebox.cardart import cmyk
    icc = "/System/Library/ColorSync/Profiles/Generic CMYK Profile.icc"
    if not os.path.exists(icc):
        pytest.skip("no CMYK profile on this machine to stand in for ECI's")
    ImageCms.getOpenProfile(icc)

    def fake_ensure(directory):
        target = os.path.join(directory, cmyk.PROFILE_NAME)
        if not os.path.exists(target):
            with open(icc, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
        return target
    monkeypatch.setattr(cmyk, "ensure_profile", fake_ensure)


def test_card_pages_are_pdfx_with_the_trim_centred(tmp_path, pso_profile):
    import pathlib
    import re
    globals()["pathlib"] = pathlib
    from nfc_jukebox.cardart import sheets
    result, pdf = _one_card_pdf(tmp_path, paper="card")
    assert result["pages"] == 1
    assert pdf.startswith(b"%PDF-1.3")
    mm = sheets.MM
    media = f"[0 0 {101 * mm:.4f} {101 * mm:.4f}]"
    trim = f"[{3 * mm:.4f} {3 * mm:.4f} {98 * mm:.4f} {98 * mm:.4f}]"
    assert f"/MediaBox {media} /TrimBox {trim} /BleedBox {media}".encode() in pdf
    for key in (b"/GTS_PDFXVersion (PDF/X-1:2001)",
                b"/GTS_PDFXConformance (PDF/X-1a:2001)",
                b"/OutputConditionIdentifier (FOGRA51)",
                b"/DestOutputProfile", b"/Trapped /False", b"/ID ["):
        assert key in pdf, key
    assert b"/DeviceRGB" not in pdf
    assert b"/DeviceCMYK" in pdf
    # No marks: nothing is stroked.
    assert not re.search(rb" l S\b", pdf)


def test_card_pages_mirror_all_four_edges(tmp_path, pso_profile):
    import pathlib
    globals()["pathlib"] = pathlib
    from nfc_jukebox.cardart import sheets
    _, pdf = _one_card_pdf(tmp_path, paper="card")
    card_pt = sheets.CARD_MM * sheets.MM
    assert f"{card_pt:.3f} {card_pt:.3f} re W n".encode() in pdf
    assert pdf.count(b"-1 0 0 1 ") == 2       # left, right
    assert pdf.count(b"-1 0 0 -1 ") == 4      # the corners
    assert pdf.count(b" 1 0 0 -1 ") == 2      # top, bottom


def test_cmyk_conversion_keeps_colours_the_right_way_round(tmp_path, pso_profile):
    """A CMYK JPEG read without its inversion undone prints as a negative."""
    import pathlib
    import shutil
    import subprocess
    globals()["pathlib"] = pathlib
    if not shutil.which("pdftoppm"):
        pytest.skip("needs poppler's pdftoppm to render the page")
    from PIL import Image
    result, _ = _one_card_pdf(tmp_path, paper="card", px=1200)
    subprocess.run(["pdftoppm", "-r", "30", "-png", str(tmp_path / "sheet.pdf"),
                    str(tmp_path / "r")], check=True)
    rendered = Image.open(next(tmp_path.glob("r*.png"))).convert("RGB")
    r, g, b = rendered.getpixel((rendered.width // 2, rendered.height // 2))
    # The source is (30, 60, 90): dark, and bluer than it is red.
    assert r + g + b < 3 * 140 and b > r


def test_cmyk_brings_huge_covers_down_and_never_scales_up(tmp_path, pso_profile):
    from PIL import Image
    from nfc_jukebox.cardart import cmyk
    from nfc_jukebox.cardart.sheets import jpeg_info
    for px in (3000, 800):
        Image.new("RGB", (px, px), (200, 20, 20)).save(tmp_path / f"{px}.jpg")
    converter = cmyk.Converter(str(tmp_path))
    assert jpeg_info(converter.jpeg(str(tmp_path / "3000.jpg")))[:3] == (
        cmyk.MAX_PX, cmyk.MAX_PX, 4)
    assert jpeg_info(converter.jpeg(str(tmp_path / "800.jpg")))[:2] == (800, 800)


def test_spares_pad_the_card_file_at_the_end(tmp_path, pso_profile):
    import pathlib
    import re
    globals()["pathlib"] = pathlib
    from nfc_jukebox.cardart import build_sheets, sheets
    _one_card_pdf(tmp_path, paper="card")          # sets up the artwork
    out = tmp_path / "padded.pdf"
    result = build_sheets(str(tmp_path / "art"), str(out), 0, True,
                          paper="card", pad_to=5)
    assert (result["cards"], result["spares"], result["pages"]) == (1, 4, 5)
    pdf = out.read_bytes()
    assert b"/Count 5" in pdf
    # The album is page 1, the spares after it: the first page's content is
    # the cover, and the spare field is drawn in CMYK, never white.
    kids = re.search(rb"/Kids \[([^\]]*)\]", pdf).group(1).split(b" 0 R")
    first = int(kids[0])
    page = re.search(rb"\n%d 0 obj\n(.*?)endobj" % first, pdf, re.S).group(1)
    content = int(re.search(rb"/Contents (\d+) 0 R", page).group(1))
    body = re.search(rb"\n%d 0 obj\n(.*?)endobj" % content, pdf, re.S).group(1)
    assert b" Do" in body
    c, m, y, k = sheets.SPARE_CMYK
    assert f"{c:g} {m:g} {y:g} {k:g} k".encode() in pdf
    assert sum(sheets.SPARE_CMYK) >= 0.1, "a spare must not read as white"


def test_more_cards_than_the_pad_are_all_printed(tmp_path, pso_profile):
    import pathlib
    globals()["pathlib"] = pathlib
    from nfc_jukebox.cardart import build_sheets
    _one_card_pdf(tmp_path, paper="card")
    result = build_sheets(str(tmp_path / "art"), str(tmp_path / "p.pdf"), 0,
                          True, paper="card", pad_to=0)
    assert (result["cards"], result["spares"], result["pages"]) == (1, 0, 1)


def test_spares_are_refused_on_a_sheet_of_several_cards(tmp_path):
    import pathlib
    globals()["pathlib"] = pathlib
    from nfc_jukebox.cardart import build_sheets
    _one_card_pdf(tmp_path)
    with pytest.raises(ValueError):
        build_sheets(str(tmp_path / "art"), str(tmp_path / "p.pdf"), 0, True,
                     paper="a3", pad_to=8)
