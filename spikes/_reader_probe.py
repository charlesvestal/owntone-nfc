"""Shared plumbing for the reader probes.

Both probes have to be run with `nfc-jukebox` stopped, because the service
holds the PN532 exclusively. Stopping it mid-transaction is also exactly what
leaves the chip out of frame sync, so the first open then fails with
ETIMEDOUT and the probe looks broken when the hardware is fine. Pulse RSTPDN
and try again, which is what reader.py does in the running service.
"""
import subprocess
import time

import nfc

RESET_GPIO = 20
RESET_PULSE_S = 1.0
SETTLE_S = 2.0
DEVICE = "tty:AMA0:pn532"


def _pinctrl(level: str) -> None:
    subprocess.run(["sudo", "pinctrl", "set", str(RESET_GPIO), "op", level],
                   check=False)


def reset_pn532() -> None:
    """Pulse RSTPDN low-high, then let the chip come back up."""
    _pinctrl("dl")
    time.sleep(RESET_PULSE_S)
    _pinctrl("dh")
    time.sleep(SETTLE_S)


def open_reader(attempts: int = 4):
    """A ContactlessFrontend, resetting the chip if it will not open."""
    for _ in range(attempts):
        try:
            return nfc.ContactlessFrontend(DEVICE)
        except Exception as exc:                              # noqa: BLE001
            print(f"open failed ({exc}); pulsing RSTPDN", flush=True)
            reset_pn532()
    raise SystemExit(
        "Could not open the reader.\n"
        "  Is nfc-jukebox still running?  sudo systemctl stop nfc-jukebox\n"
        "  Is the RSTPDN-D20 jumper fitted?")


def sense(clf, deadline):
    """The next tag seen, or None once the deadline passes."""
    return clf.connect(rdwr={"on-connect": lambda tag: False},
                       terminate=lambda: time.monotonic() > deadline)
