import time, nfc
clf = nfc.ContactlessFrontend("tty:AMA0:pn532")
print("waiting for a card (60s)...", flush=True)
deadline = time.monotonic() + 60
max_gap = 0.0
seen = False
try:
    while time.monotonic() < deadline:
        tag = clf.connect(rdwr={"on-connect": lambda tag: False},
                          terminate=lambda: time.monotonic() > deadline)
        if tag is None:
            continue
        seen = True
        uid = tag.identifier.hex()
        t0 = time.monotonic()
        print(f"PRESENT uid={uid} type={type(tag).__name__}", flush=True)
        last = time.monotonic()
        while tag.is_present:
            now = time.monotonic()
            max_gap = max(max_gap, now - last)
            last = now
            time.sleep(0.02)
        print(f"REMOVED after {time.monotonic()-t0:.2f}s", flush=True)
finally:
    clf.close()
if seen:
    print(f"max gap between presence reads: {max_gap*1000:.0f} ms")
    print(f"suggested bump_window_s: {max(0.2, max_gap*3):.2f}")
else:
    print("no card seen")
