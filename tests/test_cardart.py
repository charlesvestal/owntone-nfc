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
