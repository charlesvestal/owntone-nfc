# owntone-nfc

An NFC "vinyl jukebox" for the Raspberry Pi. Put a card on the reader and an
album plays; take it off and it stops.

Audio goes to a local speaker, to AirPlay 2 speakers such as HomePods, or both.
When a card is removed and the grace period expires, the AirPlay session is
released so the speakers are free for anything else in the house — and your
speaker choice is remembered for the next card.

## The idea

Simulate a record player. Put the record on, it plays from the start. Take it
off, it stops. The card is a physical object that does exactly one thing, and
nothing about the interaction is hidden in an app.

That constraint drives the design: no resume position, no skip button on the
box, no state the card secretly carries. Anything player-shaped — transport,
volume, speaker selection, browsing — belongs to OwnTone's own web UI, which
already does it well. This project builds only what OwnTone cannot: turning a
card on a reader into playback.

## How it works

**OwnTone** owns the music library, playback, and the audio outputs. It is the
only mature AirPlay 2 *sender* on Linux, and since v29.0.139 it defaults to
AirPlay 2 with PTP timing, which is what keeps a HomePod stereo pair in sync.

A small Python service reads card presence from a **PN532 NFC HAT** over UART
and drives OwnTone through its JSON REST API.

| Module | Responsibility |
|---|---|
| `reader.py` | Card presence. Emits `card_present(uid)` / `card_removed()`, nothing else. |
| `owntone.py` | Thin REST client. Knows nothing about cards. |
| `cards.py` | UID → album mapping, persisted as YAML. |
| `outputs.py` | Speaker-selection snapshot, so the choice survives a release cycle and a reboot. |
| `controller.py` | The state machine. The only file with interesting logic, and where the tests live. |
| `web.py` | Card registration. Deliberately tiny. |

Card mappings store **library-root-relative paths**, never absolute paths and
never OwnTone's numeric IDs — so a rebuild onto a new SD card doesn't cost you
a single re-registration.

## Getting started

Burn Raspberry Pi OS **Bookworm** Lite 64-bit (not Trixie), then:

```bash
git clone <this repo> /home/pi/owntone-nfc
sudo bash /home/pi/owntone-nfc/deploy/provision.sh
```

Copy albums to `/srv/music/<Artist>/<Album>/` (Samba share at
`smb://jukebox.local`), then open **http://jukebox.local:8080**, tap a card,
pick an album, save.

Pick your speakers in OwnTone at **http://jukebox.local:3689**. The jukebox
remembers them.

## Documentation

- **[`docs/runbook.md`](docs/runbook.md)** — hardware settings, the Wi-Fi and
  AirPlay gotchas, a symptom table, and how to rebuild. **Read this first when
  something is broken.**
- **[`docs/superpowers/specs/`](docs/superpowers/specs/)** — the design, and
  what hardware testing proved wrong about it.

## Two things that will bite you

**Wi-Fi power save must be off.** With it on, the Pi associates, transmits at
260 Mbit/s, receives at 6 Mbit/s, and silently drops off the network. It looks
like a dead router. `provision.sh` handles it.

**`user_agent = "AirPlay/999.0.0"` is required in `owntone.conf`.** Apple OS 27
gates `GET /info` on the User-Agent and returns 403 to OwnTone's default, before
any authentication. Without it, every Apple speaker stops working as it updates.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The suite runs on macOS with no hardware: `nfcpy` is imported lazily and the
reader has a fake. Note what that means — the tests prove the state machine's
logic, not that OwnTone accepts the calls. Several bugs found on real hardware
were invisible to a green suite because the mocks encoded the same assumptions
as the code.
