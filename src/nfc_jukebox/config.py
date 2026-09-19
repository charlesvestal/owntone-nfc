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

from . import reader

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("/etc/nfc-jukebox/config.yaml")


@dataclass(frozen=True)
class Config:
    owntone_url: str = "http://127.0.0.1:3689"
    library_root: Path = Path("/srv/music")
    cards_file: Path = Path("/etc/nfc-jukebox/cards.yaml")
    outputs_file: Path = Path("/var/lib/nfc-jukebox/outputs.json")
    # Where collected cover art and the print-ready sheets live. Under the
    # state directory rather than /etc: it is derived data, rebuildable at any
    # time from the library and the internet, and some of it is megabytes.
    artwork_dir: Path = Path("/var/lib/nfc-jukebox/artwork")
    reader_device: str = "tty:AMA0:pn532"
    # How long a card must be continuously unseen before the reader calls it
    # lifted. See reader.DEFAULT_PRESENCE_DEBOUNCE_S for the measurements: a
    # 4-byte Mifare-Classic-style card reports itself absent hundreds of times
    # a second while lying motionless on the reader, and without this the box
    # plays and pauses so fast that nothing is ever audible. Raise it if a card
    # technology we have not tested still stutters; lower it if lifting a
    # record feels laggy.
    presence_debounce_s: float = reader.DEFAULT_PRESENCE_DEBOUNCE_S
    grace_period_s: float = 90.0
    # Hour of the local day at which an unfinished album stops being resumable,
    # so a record abandoned at midnight starts from track 1 the next morning
    # rather than picking up mid-side. None turns the reset off entirely and
    # the place is kept until a different card is played.
    #
    # An hour, not a duration, because the thing being modelled is "a new day",
    # and 3am is when nobody is listening. Note 0 is a real setting (midnight),
    # which is why turning it off needs the same explicit vocabulary reset_gpio
    # uses rather than a falsy value.
    resume_reset_hour: int | None = 3
    web_port: int = 8080
    # BCM pin wired to the PN532's RSTPDN (active-low reset). On the Waveshare
    # HAT this repo is built around, RSTPDN is jumpered to D20. Killing the
    # service mid-transaction can leave the chip out of frame sync, after which
    # every open times out forever; pulsing this line is the only recovery.
    # Set it to null on a board with no reset jumper - the reader then just
    # retries, which is what it did before this existed.
    reset_gpio: int | None = 20

    _PATH_FIELDS = ("library_root", "cards_file", "outputs_file", "artwork_dir")
    _STR_FIELDS = ("owntone_url", "reader_device")
    _POSITIVE_FLOAT_FIELDS = ("presence_debounce_s", "grace_period_s")

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

        # These two are the fields where None is a real setting ("no reset line
        # is wired", "never reset the resume position") rather than "fall back
        # to the default", so they cannot ride the drop-None pass above.
        # Coerce them afterwards instead.
        for name, converter in (("reset_gpio", cls._as_gpio),
                                ("resume_reset_hour", cls._as_hour)):
            if name not in raw:
                continue
            try:
                values[name] = converter(raw[name])
            except (TypeError, ValueError) as exc:
                log.warning("Ignoring invalid config value for %s (%r): %s; "
                            "using the default", name, raw[name], exc)
                values.pop(name, None)

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

    # Words an operator might reasonably type to mean "switched off". Shared by
    # reset_gpio ("there is no reset line") and resume_reset_hour ("never reset
    # the resume position"), because an operator editing YAML over SSH should
    # not have to remember two spellings of off.
    _DISABLED_WORDS = ("", "none", "null", "off", "no", "disabled")

    @classmethod
    def _as_gpio(cls, value) -> "int | None":
        """A BCM pin number, or None meaning 'no reset line is wired'."""
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in cls._DISABLED_WORDS:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError("expected a BCM GPIO number, or null to disable")
        pin = int(value)
        if not 0 <= pin <= 53:
            raise ValueError("must be a BCM GPIO number between 0 and 53")
        return pin

    @classmethod
    def _as_hour(cls, value) -> "int | None":
        """An hour of the local day, or None meaning 'never reset'."""
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in cls._DISABLED_WORDS:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError("expected an hour 0-23, or null to disable")
        hour = int(value)
        if not 0 <= hour <= 23:
            raise ValueError("must be an hour of the day between 0 and 23")
        return hour

    @staticmethod
    def _as_port(value) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError("expected a TCP port number")
        port = int(value)
        if not 1 <= port <= 65535:
            raise ValueError("must be between 1 and 65535")
        return port
