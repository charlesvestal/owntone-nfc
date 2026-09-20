"""Register cards from a desk instead of kneeling next to the jukebox.

Reads UIDs from a USB NFC reader on this machine and posts each one to the
box, which records it exactly as though the card had been tapped on the
jukebox itself. The admin page's Register tab then works unchanged: tap here,
pick the album there, save.

Identification only. The endpoint cannot start, stop or switch a record, so
scanning at a desk never interrupts what is playing in the room.

    python3 -m pip install pyscard requests
    python3 tools/desk_reader.py --host jukebox.local:8080

Tested with an ACS ACR122U on macOS, which the system binds with its own CCID
driver -- so PC/SC is the way in and libusb gets EACCES. Nothing to unload and
no permissions to grant, but it does mean pyscard rather than nfcpy here, even
though the box itself uses nfcpy.

The reader's beep is its own firmware, not this script. Silence it once with
--silence; the setting lives in the reader's non-volatile memory and survives
unplugging.
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request

try:
    from smartcard.System import readers
    from smartcard.util import toHexString
except ImportError:                                     # pragma: no cover
    sys.exit("pyscard is not installed:  python3 -m pip install pyscard")

GET_UID = [0xFF, 0xCA, 0x00, 0x00, 0x00]
# "Set buzzer output during card detection", 0x00 = off, 0xFF = on.
BUZZER = {False: [0xFF, 0x00, 0x52, 0x00, 0x00],
          True: [0xFF, 0x00, 0x52, 0xFF, 0x00]}

# A card sits on the reader for as long as it takes to pick an album, and
# re-posting the same UID twenty times a second would be pointless noise.
SETTLE_S = 0.2


def post_scan(host: str, uid: str) -> str:
    """Tell the jukebox about a card. Returns a line to print."""
    import json
    request = urllib.request.Request(
        f"http://{host}/api/scan",
        data=json.dumps({"uid": uid}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.load(response)
    except urllib.error.URLError as exc:
        return f"{uid}  -- could not reach {host}: {exc.reason}"
    return f"{uid}  {'known card' if body.get('known') else 'NEW card'}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="jukebox.local:8080",
                        help="jukebox admin address (default: %(default)s)")
    parser.add_argument("--silence", action="store_true",
                        help="turn the reader's beep off permanently and exit")
    parser.add_argument("--unsilence", action="store_true",
                        help="turn the reader's beep back on and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print UIDs without telling the jukebox")
    args = parser.parse_args()

    found = readers()
    if not found:
        return print_err("No PC/SC reader found. Is it plugged in?")
    reader = found[0]
    print(f"reader: {reader}", flush=True)

    if args.silence or args.unsilence:
        print("Hold a card on the reader...", flush=True)
        conn = wait_for_card(reader)
        if conn is None:
            return print_err("No card presented.")
        _, sw1, sw2 = conn.transmit(BUZZER[args.unsilence])
        conn.disconnect()
        ok = (sw1, sw2) == (0x90, 0x00)
        print("beep " + ("on" if args.unsilence else "off")
              + (" -- saved to the reader" if ok else f" -- refused ({sw1:02X}{sw2:02X})"))
        return 0 if ok else 1

    print(f"posting to {args.host}" if not args.dry_run else "dry run",
          flush=True)
    print("tap cards -- ctrl-c to stop", flush=True)
    last = None
    try:
        while True:
            conn = wait_for_card(reader, timeout=None)
            if conn is None:                            # only on a timeout
                continue
            data, sw1, sw2 = conn.transmit(GET_UID)
            uid = toHexString(data).replace(" ", "").lower() \
                if (sw1, sw2) == (0x90, 0x00) else None
            conn.disconnect()
            if uid and uid != last:
                last = uid
                print("  " + (uid if args.dry_run else post_scan(args.host, uid)),
                      flush=True)
            # Wait for the card to be lifted, so one tap is one scan.
            while card_present(reader):
                time.sleep(SETTLE_S)
            last = None
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def card_present(reader) -> bool:
    conn = reader.createConnection()
    try:
        conn.connect()
        conn.disconnect()
        return True
    except Exception:                                   # noqa: BLE001
        return False


def wait_for_card(reader, timeout: float | None = 15.0):
    """Block until a card is on the reader. None if `timeout` passes first."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while deadline is None or time.monotonic() < deadline:
        conn = reader.createConnection()
        try:
            conn.connect()
            return conn
        except Exception:                               # noqa: BLE001
            time.sleep(SETTLE_S)
    return None


def print_err(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
