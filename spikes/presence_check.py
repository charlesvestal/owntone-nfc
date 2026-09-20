"""Does a card hold presence, or does the reader keep losing it?

Prints an episode per present/removed pair, so flicker is visible as it
happens. For choosing `presence_debounce_s` use `absence_check.py` instead:
what matters there is the length of the unseen runs, which this does not
measure.

Run with the service stopped -- it holds the PN532 exclusively:

    sudo systemctl stop nfc-jukebox
    sudo /opt/nfc-jukebox/venv/bin/python spikes/presence_check.py
    sudo systemctl start nfc-jukebox

Measured 2026-09-20 in the built enclosure: a 4-byte card (`acd997ee`)
produced 4325 present/removed episodes in 150s, every hold 0.01s. An NTAG213
held continuously for 11s+. Both work in practice -- see absence_check.py for
why the flicker does not matter.
"""
import sys
import time

from _reader_probe import open_reader, sense

WINDOW_S = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

clf = open_reader()
print(f"reader open -- place a card (window {WINDOW_S:.0f}s)", flush=True)

deadline = time.monotonic() + WINDOW_S
episodes: dict[str, list[tuple[float, float]]] = {}

try:
    while time.monotonic() < deadline:
        tag = sense(clf, deadline)
        if tag is None:
            continue
        uid = tag.identifier.hex()
        start = time.monotonic()
        last = start
        gap = 0.0
        print(f"PRESENT uid={uid} type={type(tag).__name__}", flush=True)
        while tag.is_present:
            now = time.monotonic()
            gap = max(gap, now - last)
            last = now
            time.sleep(0.02)
        held = time.monotonic() - start
        episodes.setdefault(uid, []).append((held, gap))
        print(f"REMOVED after {held:.2f}s (worst read gap {gap * 1000:.0f} ms)",
              flush=True)
finally:
    clf.close()

print()
if not episodes:
    print("no card seen")
    raise SystemExit(0)

for uid, runs in episodes.items():
    holds = [h for h, _ in runs]
    print(f"uid {uid}: {len(runs)} present/removed episode(s)")
    print(f"  longest continuous hold : {max(holds):.2f}s")
    print(f"  shortest hold           : {min(holds):.2f}s")
    if len(runs) > 3:
        rate = len(runs) / WINDOW_S
        print(f"  -> FLICKER at ~{rate:.0f}/s. Not in itself a problem:")
        print("     run absence_check.py to see whether any unseen run gets")
        print("     near presence_debounce_s. On tested hardware none did.")
    else:
        print("  -> steady")
