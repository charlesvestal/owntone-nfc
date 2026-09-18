# src/nfc_jukebox/config.py
"""Typed settings for the jukebox service.

The config file is hand-edited over SSH on a device with no screen, so every
way of getting it wrong has to end in a working box that explains itself in the
journal. A bad file falls back to defaults; a bad field costs only that field.
Crash-looping on a stray tab would leave the box silent with no explanation,
which is the one outcome the spec forbids.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("/etc/nfc-jukebox/config.yaml")


@dataclass(frozen=True)
class Config:
    owntone_url: str = "http://127.0.0.1:3689"
    library_root: Path = Path("/srv/music")
    cards_file: Path = Path("/etc/nfc-jukebox/cards.yaml")
    outputs_file: Path = Path("/var/lib/nfc-jukebox/outputs.json")
    reader_device: str = "tty:AMA0:pn532"
    # Measured, not guessed (Spike 2, 2026-09-18): a stationary NTAG213 on a
    # PN532 over UART showed a 35ms worst-case gap between presence reads over
    # ~11s. 0.25s is ~7x that margin, and safely below how fast a human can
    # lift and replace a card - so a dropped read resumes, a deliberate lift
    # restarts.
    #
    # That measurement was taken on a BARE board with the card resting on it.
    # An enclosure adds distance and will widen the gap, so re-measure once the
    # box is built by re-running spikes/presence_check.py on the Pi. This is a
    # config value, so tuning it is an edit to /etc/nfc-jukebox/config.yaml plus
    # a service restart - no code change needed.
    bump_window_s: float = 0.25
    grace_period_s: float = 90.0
    web_port: int = 8080

    _PATH_FIELDS = ("library_root", "cards_file", "outputs_file")
    _STR_FIELDS = ("owntone_url", "reader_device")
    _POSITIVE_FLOAT_FIELDS = ("bump_window_s", "grace_period_s")

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "Config":
        """Load config, falling back to defaults for anything absent or bad."""
        raw = cls._read_raw(path)

        known = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in raw.items() if k in known}

        for name in cls._PATH_FIELDS:
            if name in values:
                values[name] = cls._coerce(name, values[name], cls._as_path)
        for name in cls._STR_FIELDS:
            if name in values:
                values[name] = cls._coerce(name, values[name], cls._as_str)
        for name in cls._POSITIVE_FLOAT_FIELDS:
            if name in values:
                values[name] = cls._coerce(name, values[name], cls._as_positive_float)
        if "web_port" in values:
            values["web_port"] = cls._coerce("web_port", values["web_port"], cls._as_port)

        # Anything that failed coercion was dropped, so the field's declared
        # default applies.
        values = {k: v for k, v in values.items() if v is not None}
        return cls(**values)

    @staticmethod
    def _read_raw(path: Path) -> dict:
        try:
            text = path.read_text()
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError):
            log.exception("Could not read config %s; using defaults", path)
            return {}

        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError:
            log.exception("Config %s is not valid YAML; using defaults", path)
            return {}

        if raw is None:
            return {}
        if not isinstance(raw, dict):
            log.error("Config %s is not a mapping of settings; using defaults", path)
            return {}
        return raw

    @staticmethod
    def _coerce(name: str, value, converter):
        """Return the converted value, or None to mean 'fall back to default'."""
        try:
            return converter(value)
        except (TypeError, ValueError) as exc:
            log.warning("Ignoring invalid config value for %s (%r): %s; "
                        "using the default", name, value, exc)
            return None

    @staticmethod
    def _as_str(value) -> str:
        if not isinstance(value, str):
            raise TypeError("expected a string")
        return value

    @staticmethod
    def _as_path(value) -> Path:
        if not isinstance(value, (str, Path)):
            raise TypeError("expected a filesystem path")
        return Path(value)

    @staticmethod
    def _as_positive_float(value) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise TypeError("expected a number of seconds")
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
        if number < 0:
            raise ValueError("must not be negative")
        return number

    @staticmethod
    def _as_port(value) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError("expected a TCP port number")
        port = int(value)
        if not 1 <= port <= 65535:
            raise ValueError("must be between 1 and 65535")
        return port
