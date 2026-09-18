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
    # BCM pin wired to the PN532's RSTPDN (active-low reset). On the Waveshare
    # HAT this repo is built around, RSTPDN is jumpered to D20. Killing the
    # service mid-transaction can leave the chip out of frame sync, after which
    # every open times out forever; pulsing this line is the only recovery.
    # Set it to null on a board with no reset jumper - the reader then just
    # retries, which is what it did before this existed.
    reset_gpio: int | None = 20

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

        # reset_gpio is the one field where None is a real setting ("no reset
        # line is wired") rather than "fall back to the default", so it cannot
        # ride the drop-None pass above. Coerce it afterwards instead.
        if "reset_gpio" in raw:
            try:
                values["reset_gpio"] = cls._as_gpio(raw["reset_gpio"])
            except (TypeError, ValueError) as exc:
                log.warning("Ignoring invalid config value for reset_gpio "
                            "(%r): %s; using the default",
                            raw["reset_gpio"], exc)
                values.pop("reset_gpio", None)

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

    # Words an operator might reasonably type to mean "there is no reset line".
    _GPIO_DISABLED_WORDS = ("", "none", "null", "off", "no", "disabled")

    @classmethod
    def _as_gpio(cls, value) -> "int | None":
        """A BCM pin number, or None meaning 'no reset line is wired'."""
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in cls._GPIO_DISABLED_WORDS:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError("expected a BCM GPIO number, or null to disable")
        pin = int(value)
        if not 0 <= pin <= 53:
            raise ValueError("must be a BCM GPIO number between 0 and 53")
        return pin

    @staticmethod
    def _as_port(value) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError("expected a TCP port number")
        port = int(value)
        if not 1 <= port <= 65535:
            raise ValueError("must be between 1 and 65535")
        return port
