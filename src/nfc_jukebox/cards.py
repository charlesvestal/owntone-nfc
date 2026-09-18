"""Card UID to album mapping, persisted as YAML.

Paths are stored relative to the library root so a mapping survives a rebuild
onto a new SD card, or a switch between local storage and a NAS mount. This
module knows nothing about playback -- it only maps UIDs to relative paths.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_SEPARATORS = re.compile(r"[^0-9a-fA-F]")


def normalise_uid(raw: str) -> str:
    """Lowercase hex, separators removed, so formatting differences still match."""
    return _SEPARATORS.sub("", raw).lower()


@dataclass
class Card:
    uid: str
    name: str
    path: str  # relative to the library root


class CardStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, Card]:
        try:
            raw = yaml.safe_load(self._path.read_text()) or {}
        except FileNotFoundError:
            return {}
        cards: dict[str, Card] = {}
        for uid, entry in raw.items():
            # cards.yaml is hand-edited and restored from backup; one bad
            # entry must cost that card, not the whole record collection.
            if not isinstance(entry, dict) or "name" not in entry or "path" not in entry:
                log.warning(
                    "Skipping malformed card entry for UID %s in %s: "
                    "expected a mapping with 'name' and 'path'", uid, self._path,
                )
                continue
            key = normalise_uid(str(uid))
            cards[key] = Card(uid=key, name=entry["name"], path=entry["path"])
        return cards

    def save(self, cards: dict[str, Card]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            card.uid: {"name": card.name, "path": card.path}
            for card in cards.values()
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=True))
        tmp.replace(self._path)

    def get(self, uid: str) -> Card | None:
        return self.load().get(normalise_uid(uid))
