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
