# src/nfc_jukebox/reader.py
"""Card presence detection.

Emits exactly two events and knows nothing about music. nfcpy is imported
lazily so tests (and a dev machine) need no reader hardware.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from .cards import normalise_uid

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.05
ERROR_BACKOFF = 1.0


class BaseReader:
    def __init__(self) -> None:
        self.on_present: Callable[[str], None] = lambda uid: None
        self.on_removed: Callable[[], None] = lambda: None


class FakeReader(BaseReader):
    """Test double -- presence is driven manually."""

    def __init__(self) -> None:
        super().__init__()
        self._present = False

    def place(self, uid: str) -> None:
        self._present = True
        self.on_present(normalise_uid(uid))

    def lift(self) -> None:
        if not self._present:
            return
        self._present = False
        self.on_removed()


class Pn532Reader(BaseReader):
    """Real reader: PN532 over UART via nfcpy."""

    def __init__(self, device: str) -> None:
        super().__init__()
        self._device = device

    def run(self) -> None:
        import nfc  # imported here so the package works without hardware

        while True:
            try:
                clf = nfc.ContactlessFrontend(self._device)
            except Exception:
                log.exception("Cannot open reader %s; retrying", self._device)
                time.sleep(ERROR_BACKOFF)
                continue

            try:
                self._poll_forever(clf)
            except Exception:
                log.exception("Reader error; reopening")
                time.sleep(ERROR_BACKOFF)
            finally:
                clf.close()

    def _poll_forever(self, clf) -> None:
        while True:
            tag = clf.connect(rdwr={"on-connect": lambda tag: False})
            if tag is None:
                continue
            self.on_present(normalise_uid(tag.identifier.hex()))
            while tag.is_present:
                time.sleep(POLL_INTERVAL)
            self.on_removed()
