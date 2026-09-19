"""Pure helpers for finding and sizing album artwork.

No network and no filesystem beyond reading a named file, so the fiddly parts
-- image header parsing and turning a library path into something a search
engine will match -- can be tested directly.
"""
from __future__ import annotations

import re
import struct

# JPEG start-of-frame markers, which are the ones carrying dimensions. The
# other 0xFFCn values are arithmetic-coding and DHT markers, which do not.
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a JPEG, or None if the header is not in `data`.

    Walks the segment chain rather than scanning for a marker byte: 0xFF is a
    common data byte, and a naive search finds false frames inside the entropy
    coded scan.
    """
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in _SOF:
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        # Standalone markers carry no length field.
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xFF:      # fill byte
            i += 1
            continue
        (seglen,) = struct.unpack(">H", data[i + 2:i + 4])
        if seglen < 2:
            return None
        i += 2 + seglen
    return None


def png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def image_size(data: bytes) -> tuple[int, int] | None:
    return jpeg_size(data) or png_size(data)


def flac_picture(path: str) -> tuple[int, int, bytes] | None:
    """Largest picture embedded in a FLAC, as (width, height, image bytes).

    The PICTURE block states its own width and height, but taggers are not
    obliged to fill them in and frequently write zeros -- every FLAC in this
    library does. Fall back to the embedded image's own header in that case,
    which is authoritative.
    """
    best = None
    with open(path, "rb") as fh:
        if fh.read(4) != b"fLaC":
            return None
        while True:
            header = fh.read(4)
            if len(header) < 4:
                return best
            is_last = header[0] & 0x80
            block_type = header[0] & 0x7F
            body = fh.read(int.from_bytes(header[1:4], "big"))
            if block_type == 6:
                found = _parse_picture(body)
                if found and (best is None or
                              found[0] * found[1] > best[0] * best[1]):
                    best = found
            if is_last:
                return best


def _parse_picture(body: bytes) -> tuple[int, int, bytes] | None:
    if len(body) < 32:
        return None
    off = 4                                                   # picture type
    off += 4 + int.from_bytes(body[off:off + 4], "big")        # MIME type
    off += 4 + int.from_bytes(body[off:off + 4], "big")        # description
    if off + 20 > len(body):
        return None
    width, height = struct.unpack(">II", body[off:off + 8])
    off += 20                            # width, height, depth, indexed colours
    datalen = int.from_bytes(body[off:off + 4], "big")
    off += 4
    image = body[off:off + datalen]
    if not width or not height:
        size = image_size(image)
        if not size:
            return None
        width, height = size
    return width, height, image


# Albums named after their artist, which the ripper wrote as "S/T" and the
# filesystem then mangled. The artwork search wants the artist's name, since
# that is what the release is actually called.
_SELF_TITLED = {"s/t", "s:t", "st", "s t", "self titled", "self-titled"}


def search_terms(path: str) -> tuple[str, str]:
    """(artist, album) suitable for a search engine, from a library path.

    Strips the parenthetical edition markers that make an exact-title match
    fail -- "(2023 Edition)", "(deluxe edition)" -- since the artwork is the
    same and the edition only narrows the search unhelpfully.
    """
    artist, _, album = path.partition("/")
    album = re.sub(r"\s*[\(\[][^)\]]*(edition|remaster|deluxe|version|"
                   r"anniversary|expanded)[^)\]]*[\)\]]", "",
                   album, flags=re.IGNORECASE)
    artist, album = _clean(artist), _clean(album)
    if album.lower() in _SELF_TITLED:
        album = artist
    return artist, album


def _clean(text: str) -> str:
    # An underscore is a character the filesystem could not hold, and which
    # one depends on where it sits. Before a space it stood for a colon
    # ("Patriot_ Season 1"); between two characters for a slash ("S_T"); at
    # the end for a character that adds nothing to a search anyway.
    text = re.sub(r"_$", "", text)
    text = re.sub(r"_(?=\s)", ":", text)
    text = re.sub(r"(?<=\S)_(?=\S)", "/", text)
    # Curly quotes and ellipses read fine to humans and badly to search APIs.
    text = (text.replace("’", "'").replace("‘", "'")
                .replace("“", '"').replace("”", '"')
                .replace("…", "..."))
    return text.strip()


def slug(path: str) -> str:
    """A flat, stable filename for an album's artwork."""
    text = path.replace("/", " - ")
    text = re.sub(r"[^\w\s\-.']", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()[:150]


def title_match(searched: tuple[str, str], matched: tuple[str, str]) -> float:
    """How well a search result fits what was asked for, 0.0 to 1.0.

    Artist and album are scored separately and the album weighted higher. A
    combined comparison looks reasonable until a self-titled record, where the
    artist name satisfies the entire query and any album by that artist scores
    highly -- which is exactly how a search for Liquid Mike's self-titled album
    returned "Paul Bunyan's Slingshot" wearing a 0.85.

    Deliberately forgiving about extra words, because catalogues append things
    the library does not carry ("(Deluxe)", "(Original 1965 TV Soundtrack)")
    and those are still the right record. Missing and different words are what
    indicate a wrong match.
    """
    return 0.35 * _similar(searched[0], matched[0]) + \
           0.65 * _similar(searched[1], matched[1])


def _similar(wanted_text: str, got_text: str) -> float:
    import difflib
    import re as _re

    def words(text: str) -> set[str]:
        return {w for w in _re.findall(r"[a-z0-9']+", text.lower())
                if w not in _NOISE}

    wanted, got = words(wanted_text), words(got_text)
    if not wanted:
        return 1.0 if not got else 0.0
    covered = len(wanted & got) / len(wanted)
    # Sequence similarity catches transpositions and near-spellings that a set
    # comparison misses; the two disagree often enough to be worth averaging.
    ratio = difflib.SequenceMatcher(None, wanted_text.lower(),
                                    got_text.lower()).ratio()
    return 0.7 * covered + 0.3 * ratio


_NOISE = {"the", "a", "an", "and", "of", "deluxe", "edition", "remaster",
          "remastered", "version", "expanded", "anniversary", "original",
          "soundtrack", "ep", "lp", "feat", "featuring", "bonus", "track",
          "tracks", "disc", "vol", "volume"}


# Below this a match is treated as a different album rather than a variant
# spelling. Tuned against this library: "Liquid Mike - Paul Bunyan's
# Slingshot" for a self-titled search scores 0.37, while "Vince Guaraldi Trio
# - A Charlie Brown Christmas (Original 1965 TV Soundtrack)" scores 0.67.
MATCH_FLOOR = 0.55

# Above this a match is good enough to stop searching. Clearing MATCH_FLOOR is
# not: a fit of 0.60 is "probably not a different album", which is a much
# weaker claim than "this is the album". Deezer offered "Heavenly Sweetheart -
# $300 (feat. Liquid Mike)" at 0.60 for a Liquid Mike search, and treating
# that as settled meant never asking the archive that had the real sleeve.
CONFIDENT = 0.85


def rank_candidates(candidates, min_px: int):
    """Order artwork candidates best-first.

    Correctness outranks resolution: a sharp scan of the wrong album is worse
    than a merely adequate picture of the right one, because only one of those
    is recoverable by looking at it. So candidates are grouped into "plausibly
    this album" and "probably not", and resolution only decides within a group.

    `candidates` are dicts with `width`, `score` and `source`.
    """
    def key(candidate):
        plausible = candidate["score"] >= MATCH_FLOOR
        big_enough = candidate["width"] >= min_px
        local = candidate["source"].startswith(("file:", "embedded:"))
        return (
            plausible,          # right album first
            big_enough,         # then usable for print
            local,              # then the library, which cannot be a mismatch
            # Fit to one decimal, so that a clearly better match beats a
            # merely sharper one, while near-ties fall through to resolution.
            # Without this a 0.6 fit at 4000px outranked a 1.0 at 3000.
            round(candidate["score"], 1),
            candidate["width"],  # then sharpness
        )
    return sorted(candidates, key=key, reverse=True)
