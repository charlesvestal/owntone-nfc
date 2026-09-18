"""Persisted snapshot of which OwnTone outputs were selected.

Written to disk rather than held in memory so the speaker choice survives a
reboot, not merely an idle cycle.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class OutputSnapshot:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> list[str]:
        try:
            value = json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
            return []
        return value if isinstance(value, list) else []

    def save(self, output_ids: list[str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(output_ids))
        os.replace(tmp, self._path)  # atomic on POSIX
