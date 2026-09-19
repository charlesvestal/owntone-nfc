"""Card UID to album mapping, persisted as YAML.

Paths are stored relative to the library root so a mapping survives a rebuild
onto a new SD card, or a switch between local storage and a NAS mount. This
module knows nothing about playback -- it only maps UIDs to relative paths.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import PurePosixPath
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_SEPARATORS = re.compile(r"[^0-9a-fA-F]")


def normalise_uid(raw: str) -> str:
    """Lowercase hex, separators removed, so formatting differences still match."""
    return _SEPARATORS.sub("", raw).lower()


def name_for_path(path: str) -> str:
    """A display label derived from the album folder.

    The path already names the album, so asking for a separate name is
    redundant typing. A name is still allowed - for a friendly label like
    "Bedtime Songs" - but it is never required.
    """
    return PurePosixPath(path.strip("/")).name or path


@dataclass
class Card:
    uid: str
    name: str  # display only; defaults to the album folder name
    path: str  # relative to the library root


def duplicate_paths(cards: dict[str, Card]) -> dict[str, list[str]]:
    """Album paths claimed by more than one card, path -> sorted UIDs.

    Every album is meant to have exactly one card, so anything in here is a
    registration mistake. Nothing in the store prevents one -- the registry is
    keyed by UID, and two UIDs pointing at one album are perfectly valid to it
    -- and with a hundred-odd cards the error is invisible until two of them
    turn out to play the same record.

    The album path is the identity, matching the rest of the system. `name` is
    a label a human typed and is deliberately ignored: two cards labelled
    differently for the same album are still two cards for one album.
    """
    by_path: dict[str, list[str]] = {}
    for card in cards.values():
        by_path.setdefault(card.path, []).append(card.uid)
    return {path: sorted(uids)
            for path, uids in by_path.items() if len(uids) > 1}


class CardStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text()

    def _quarantine(self) -> None:
        """Move an unreadable registry aside instead of letting save() eat it.

        The registry is hand-built card by card, so it is worth far more than
        an outputs snapshot. Renaming lets the box carry on (silently empty is
        still better than crash-looping) while keeping the file for a human to
        repair, and stops every subsequent tap re-hitting the same parse error.
        """
        aside = self._path.with_suffix(
            self._path.suffix + f".corrupt-{time.strftime('%Y%m%d%H%M%S')}"
        )
        try:
            self._path.replace(aside)
        except OSError:
            log.exception("Could not move unreadable card file %s aside", self._path)
        else:
            log.error(
                "Card file %s was unreadable; preserved as %s and continuing with "
                "no cards registered. Repair it and restart.", self._path, aside,
            )

    def load(self) -> dict[str, Card]:
        try:
            text = self._read(self._path)
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError):
            # A read failure is not evidence the content is bad, so leave the
            # file alone -- it may just be a permissions or hardware blip.
            log.exception("Could not read card file %s; continuing with no cards",
                          self._path)
            return {}

        try:
            raw = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            log.exception("Card file %s is not valid YAML", self._path)
            self._quarantine()
            return {}

        if not isinstance(raw, dict):
            log.error("Card file %s is not a mapping of UIDs to entries", self._path)
            self._quarantine()
            return {}

        cards: dict[str, Card] = {}
        for uid, entry in raw.items():
            # cards.yaml is hand-edited and restored from backup; one bad
            # entry must cost that card, not the whole record collection.
            if not isinstance(entry, dict) or "path" not in entry:
                log.warning(
                    "Skipping malformed card entry for UID %s in %s: "
                    "expected a mapping with a 'path'", uid, self._path,
                )
                continue
            key = normalise_uid(str(uid))
            cards[key] = Card(uid=key,
                              name=entry.get("name") or name_for_path(entry["path"]),
                              path=entry["path"])
        return cards

    def save(self, cards: dict[str, Card]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            card.uid: {"name": card.name, "path": card.path}
            for card in cards.values()
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            # fsync before the rename: without it a power cut can leave the
            # rename durable but the contents not, i.e. a zero-length or
            # half-written registry and a permanently silent box.
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(yaml.safe_dump(payload, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)  # atomic on POSIX
            self._fsync_dir(self._path.parent)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Make the rename itself durable. Best effort; not all FSes allow it."""
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def get(self, uid: str) -> Card | None:
        return self.load().get(normalise_uid(uid))
