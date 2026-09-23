"""Converting cover art to the printer's CMYK, for the PDF/X-1a card file.

The printer asked for PSO Coated v3 (FOGRA51). ECI publishes the profile for
free, and its terms allow using and embedding it -- PDF/X-1a embeds it in the
file -- but not redistributing it. So it is not in this repository: the first
build fetches it from ECI into the artwork directory and keeps it there.

This is the one place art is re-encoded rather than embedded byte-for-byte. A
CMYK file cannot carry the RGB JPEG, so the pixels are converted anyway, and
while they are being re-encoded anything far beyond print resolution is
brought down to MAX_PX. Nothing is ever scaled up.
"""
from __future__ import annotations

import hashlib
import io
import os
import urllib.request
import zipfile
from typing import Any

PROFILE_NAME = "PSOcoated_v3.icc"
PROFILE_URL = "https://www.eci.org/lib/exe/pso-coated_v3.zip"
# What the PDF/X OutputIntent calls it: the ICC registry's characterization
# name, so a preflight recognises the condition without reading the profile.
OUTPUT_CONDITION = "FOGRA51"
OUTPUT_CONDITION_INFO = "PSO Coated v3"

# 1500px across 95mm is 400dpi: comfortably past the 300dpi print wants, and
# a quarter of the pixels of the 3000px covers most sources return. Without a
# cap 220 of those make a file of half a gigabyte, past what print shops take.
MAX_PX = 1500
JPEG_QUALITY = 92

CACHE_DIR = ".cmyk"


def ensure_profile(directory: str) -> str:
    """Path to the CMYK profile, fetching it from ECI the first time."""
    path = os.path.join(directory, PROFILE_NAME)
    if os.path.exists(path):
        return path
    with urllib.request.urlopen(PROFILE_URL, timeout=60) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    member = next(n for n in archive.namelist()
                  if n.endswith(PROFILE_NAME) and "__MACOSX" not in n)
    partial = path + ".part"
    with open(partial, "wb") as handle:
        handle.write(archive.read(member))
    os.replace(partial, path)
    return path


class Converter:
    """RGB artwork to CMYK JPEG bytes, cached next to the artwork.

    Relative colorimetric with black point compensation: what Photoshop's
    "Convert to Profile" does by default, and what keeps a cover's blacks
    black rather than a muddy grey. The conversion is slow on the Pi, so each
    result is kept, keyed on the source's contents and these settings.
    """

    def __init__(self, directory: str):
        from PIL import ImageCms

        self._directory = directory
        self._profile_path = ensure_profile(directory)
        self._cmyk = ImageCms.getOpenProfile(self._profile_path)
        self._srgb = ImageCms.createProfile("sRGB")
        self._transforms: dict[bytes, Any] = {}
        self._cache = os.path.join(directory, CACHE_DIR)
        os.makedirs(self._cache, exist_ok=True)

    @property
    def profile_bytes(self) -> bytes:
        with open(self._profile_path, "rb") as handle:
            return handle.read()

    def _transform(self, icc: bytes | None):
        from PIL import ImageCms

        key = icc or b""
        if key not in self._transforms:
            source = self._srgb
            if icc:
                try:
                    source = ImageCms.getOpenProfile(io.BytesIO(icc))
                except Exception:                       # noqa: BLE001
                    # A broken embedded profile: sRGB is what a browser would
                    # have shown, so it is the honest fallback.
                    source = self._srgb
            self._transforms[key] = ImageCms.buildTransform(
                source, self._cmyk, "RGB", "CMYK",
                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                flags=ImageCms.Flags.BLACKPOINTCOMPENSATION)
        return self._transforms[key]

    def jpeg(self, path: str) -> bytes:
        """CMYK JPEG bytes for one piece of artwork."""
        with open(path, "rb") as handle:
            source = handle.read()
        digest = hashlib.sha256(
            source + f"{MAX_PX}:{JPEG_QUALITY}:{OUTPUT_CONDITION}".encode()
        ).hexdigest()[:32]
        cached = os.path.join(self._cache, digest + ".jpg")
        if os.path.exists(cached):
            with open(cached, "rb") as handle:
                return handle.read()

        from PIL import Image, ImageCms

        with Image.open(io.BytesIO(source)) as image:
            icc = image.info.get("icc_profile")
            if image.mode == "CMYK":
                # Already separated, for some press we know nothing about.
                # Converting CMYK to CMYK without its profile is guesswork,
                # so it goes through as it is.
                cmyk = image.copy()
            else:
                if image.mode in ("RGBA", "LA", "P"):
                    # No alpha in print. Compose onto white, or every
                    # transparent region prints black.
                    rgba = image.convert("RGBA")
                    rgb = Image.new("RGB", rgba.size, (255, 255, 255))
                    rgb.paste(rgba, mask=rgba.split()[-1])
                else:
                    rgb = image.convert("RGB")
                if max(rgb.size) > MAX_PX:
                    rgb.thumbnail((MAX_PX, MAX_PX), Image.Resampling.LANCZOS)
                cmyk = ImageCms.applyTransform(rgb, self._transform(icc))
                assert cmyk is not None          # None only when in place
            if max(cmyk.size) > MAX_PX:
                cmyk.thumbnail((MAX_PX, MAX_PX), Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        cmyk.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        data = buffer.getvalue()
        partial = cached + ".part"
        with open(partial, "wb") as handle:
            handle.write(data)
        os.replace(partial, cached)
        return data
