# src/nfc_jukebox/reader.py
"""Card presence detection.

Emits exactly two events and knows nothing about music. nfcpy is imported
lazily so tests (and a dev machine) need no reader hardware.
"""
from __future__ import annotations

import logging
import subprocess
import time
from typing import Callable

from .cards import normalise_uid

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.05
ERROR_BACKOFF = 1.0
# A failed open doubles the wait, up to a minute. Retrying once a second
# forever writes ~90k journal lines a day and buries whatever else went wrong;
# a wedged chip is not going to un-wedge itself in the second we saved.
MAX_BACKOFF = 60.0
# Reset on the SECOND consecutive failure, not the first. A single timeout is
# often a transient - a port still being released, a half-finished boot - and
# resetting the chip out from under a recovery already in progress helps
# nobody. Two in a row is the signature of the wedged-chip case, and at this
# backoff that is still only ~1s later.
RESET_AFTER_FAILURES = 2
# Hand-verified on the device: 1s low is comfortably longer than the PN532
# needs to see RSTPDN asserted.
RESET_PULSE_S = 1.0
PINCTRL_TIMEOUT_S = 5.0
DEFAULT_RESET_GPIO = 20


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
    """Real reader: PN532 over UART via nfcpy.

    Killing the service mid-transaction (a plain systemd restart will do it)
    leaves the PN532 out of frame sync. Every subsequent open then fails with
    ETIMEDOUT and stays failed - observed on hardware, 18 times in 20 seconds,
    with nothing holding the port. Only asserting the chip's active-low RSTPDN
    line clears it, so that is what this does before giving up on a retry.
    """

    def __init__(self, device: str,
                 reset_gpio: "int | None" = DEFAULT_RESET_GPIO) -> None:
        super().__init__()
        self._device = device
        self._reset_gpio = reset_gpio

    def run(self) -> None:
        import nfc  # imported here so the package works without hardware

        while True:
            clf = self._open_frontend(nfc)
            try:
                self._poll_forever(clf)
            except Exception:
                log.exception("Reader error; reopening")
                time.sleep(ERROR_BACKOFF)
            finally:
                clf.close()

    def _open_frontend(self, nfc):
        """Block until the reader opens, resetting the chip if it stays shut."""
        failures = 0
        while True:
            try:
                clf = nfc.ContactlessFrontend(self._device)
            except Exception:
                failures += 1
                if failures == 1:
                    # Full traceback once; after that the journal only needs to
                    # know we are still trying, and how hard.
                    log.exception("Cannot open reader %s; retrying",
                                  self._device)
                else:
                    log.warning("Cannot open reader %s (%d consecutive "
                                "failures)", self._device, failures)
                if failures == RESET_AFTER_FAILURES and self._reset_gpio is None:
                    log.warning("reset_gpio is disabled, so the PN532 cannot "
                                "be reset from software; retrying the open "
                                "alone, which will not clear a wedged chip")
                if failures >= RESET_AFTER_FAILURES:
                    self._pulse_reset()
                time.sleep(min(ERROR_BACKOFF * 2 ** (failures - 1), MAX_BACKOFF))
                continue

            if failures:
                log.info("Reader %s opened after %d failed attempt(s)",
                         self._device, failures)
            return clf

    def _pulse_reset(self) -> None:
        """Pulse RSTPDN low-high. Never raises: recovery must not kill us."""
        pin = self._reset_gpio
        if pin is None:  # no reset line wired; already reported by the caller
            return

        log.warning("Attempting hardware reset: pulsing PN532 RSTPDN on "
                    "GPIO%d low for %.1fs", pin, RESET_PULSE_S)
        if not self._pinctrl(pin, "dl"):
            return
        try:
            time.sleep(RESET_PULSE_S)
        finally:
            # Leaving the line low would hold the chip in reset forever, so the
            # release is attempted even if the wait is interrupted.
            if self._pinctrl(pin, "dh"):
                log.warning("Hardware reset pulse sent on GPIO%d", pin)

    @staticmethod
    def _pinctrl(pin: int, level: str) -> bool:
        """Drive a pin via raspi-utils' pinctrl. True if it worked."""
        cmd = ["pinctrl", "set", str(pin), "op", level]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=PINCTRL_TIMEOUT_S)
        except FileNotFoundError:
            log.error("pinctrl not found (it ships in raspi-utils); cannot "
                      "reset the PN532 - will keep retrying the open")
            return False
        except Exception:
            log.exception("Running %s failed; cannot reset the PN532",
                          " ".join(cmd))
            return False
        if result.returncode != 0:
            log.error("%s exited %d: %s", " ".join(cmd), result.returncode,
                      (result.stderr or "").strip())
            return False
        return True

    def _poll_forever(self, clf) -> None:
        while True:
            tag = clf.connect(rdwr={"on-connect": lambda tag: False})
            if tag is None:
                continue
            self.on_present(normalise_uid(tag.identifier.hex()))
            while tag.is_present:
                time.sleep(POLL_INTERVAL)
            self.on_removed()
