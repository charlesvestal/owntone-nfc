# src/nfc_jukebox/config.py
"""Typed settings for the jukebox service."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("/etc/nfc-jukebox/config.yaml")


@dataclass(frozen=True)
class Config:
    owntone_url: str = "http://127.0.0.1:3689"
    library_root: Path = Path("/srv/music")
    cards_file: Path = Path("/etc/nfc-jukebox/cards.yaml")
    outputs_file: Path = Path("/var/lib/nfc-jukebox/outputs.json")
    reader_device: str = "tty:AMA0:pn532"
    # Set from the Spike 2 measurement, not guessed.
    bump_window_s: float = 0.5
    grace_period_s: float = 90.0
    web_port: int = 8080

    _PATH_FIELDS = ("library_root", "cards_file", "outputs_file")

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "Config":
        """Load config, falling back to defaults for anything absent."""
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except FileNotFoundError:
            raw = {}

        known = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in raw.items() if k in known}
        for name in cls._PATH_FIELDS:
            if name in values:
                values[name] = Path(values[name])
        return cls(**values)
