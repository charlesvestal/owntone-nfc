"""How long does a motionless card go *unseen*?

This is the number `presence_debounce_s` is actually set against. The reader
calls a card lifted once it has been continuously unseen for that long, so what
matters is the tail of the absence distribution -- not the flicker rate, and
not the average.

Run with the service stopped, the card sitting still, and do not touch it:

    sudo systemctl stop nfc-jukebox
    sudo /opt/nfc-jukebox/venv/bin/python spikes/absence_check.py
    sudo systemctl start nfc-jukebox

Measured 2026-09-20 on `acd997ee`, a 4-byte card, in the built enclosure:
1742 absence runs in 60s, median 26.1ms, p99.9 26.5ms, worst 26.5ms. The
distribution is metronomic -- p99.9 and worst differ by 0.1ms -- so 0.5s has
~19x margin and raising it would only add lag to every lift. Measure before
changing the value; the intuition that a flickering card needs a longer
debounce is wrong on this hardware.
"""
import sys
import time

from _reader_probe import open_reader, sense

WINDOW_S = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

clf = open_reader()
print(f"measuring absence runs for {WINDOW_S:.0f}s -- keep the card still",
      flush=True)

deadline = time.monotonic() + WINDOW_S
absences: list[float] = []
uid = None
last_seen = None

try:
    while time.monotonic() < deadline:
        tag = sense(clf, deadline)
        now = time.monotonic()
        if tag is None:
            continue
        uid = tag.identifier.hex()
        if last_seen is not None:
            absences.append(now - last_seen)
        # Poll faster than the reader does: we are measuring the gap, so the
        # sampling interval has to be small compared to it.
        while tag.is_present:
            last_seen = time.monotonic()
            time.sleep(0.005)
        last_seen = time.monotonic()
finally:
    clf.close()

print()
if not absences:
    print("no card seen")
    raise SystemExit(0)

absences.sort()
n = len(absences)


def pct(p: float) -> float:
    return absences[min(n - 1, int(n * p / 100))] * 1000


worst_ms = absences[-1] * 1000
print(f"uid {uid}: {n} absence runs in {WINDOW_S:.0f}s")
print(f"  median : {pct(50):7.1f} ms")
print(f"  p99    : {pct(99):7.1f} ms")
print(f"  p99.9  : {pct(99.9):7.1f} ms")
print(f"  worst  : {worst_ms:7.1f} ms")
print()
for debounce in (0.3, 0.5, 0.7):
    over = sum(1 for a in absences if a >= debounce)
    print(f"  presence_debounce_s={debounce}: {debounce * 1000 / worst_ms:5.1f}x"
          f" the worst run, {over} false lifts in this sample")
print()
print("  A card is only worth a longer debounce if a run actually approaches")
print("  it. Flicker on its own is not a reason -- the debounce exists to")
print("  absorb exactly that.")
