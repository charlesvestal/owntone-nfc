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

## Hardware

| Part | Notes |
|---|---|
| **Raspberry Pi 4** | A 3B+ would likely do. Built and tested on a Pi 4. |
| **Waveshare PN532 NFC HAT** | Sits on the GPIO header. Any PN532 board with a UART mode works. |
| **NFC cards or tags** | See the note on card types below. |
| SD card | 16GB is plenty if the music lives on a NAS. |
| Speakers | Anything OwnTone can drive: the Pi's 3.5mm jack, a USB/I2S DAC, or AirPlay 2 speakers such as HomePods. |

### Configuring the HAT

**Set it to UART**, not I2C or SPI. This matters: `nfcpy` is the library with a
real card-*presence* API rather than read-a-UID-once, and it does not support
I2C at all. Continuous presence detection is the entire product - the card has
to keep saying "I am still here".

**DIP switches** — 1–6 OFF, 7 and 8 ON:

| # | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| Signal | SCK | MISO | MOSI | NSS | SCL | SDA | **RX** | **TX** |
| Set to | OFF | OFF | OFF | OFF | OFF | OFF | **ON** | **ON** |

**Jumpers** — the two labelled `I0`/`I1` (or `L0`/`L1`) set the chip's protocol,
separately from the DIP switches. For UART both go to **L**. Get the switches
right and the jumpers wrong and the reader sits there silent.

**Leave `RSTPDN` jumpered to `D20`.** It looks like a vestigial jumper and it
is not: killing the service mid-transaction leaves the PN532 out of frame sync,
after which *every* attempt to open it fails forever and reopening the port
never recovers it. Pulsing that reset line is the only fix, and the service
does it automatically after two consecutive failures.

`provision.sh` handles the Pi side: freeing the serial console and putting the
real PL011 UART on GPIO14/15 (the mini-UART's baud rate drifts with the VPU
clock and gives a reader that works only intermittently).

### A note on card types

**Card technology matters more than you would expect.** A 7-byte NTAG213
reports its presence continuously. A 4-byte Mifare-Classic-style card, sitting
motionless on the same reader, reported present for **8 milliseconds at a time,
725 times in 25 seconds** - because `nfcpy` re-selects the tag to check, and
that re-select fails for that type. Both work here, because presence is
debounced in software, but it is why `presence_debounce_s` exists and why you
should measure with the *worst* card you own rather than the first one to hand.
`spikes/presence_check.py` does that measurement.

### If it goes in an enclosure

Check Wi-Fi **in its final position before** building everything else. Ten
minutes of measuring saves an evening: see the runbook's Wi-Fi section, which
opens with the router setting that matters most.

## Getting started

Burn Raspberry Pi OS Lite 64-bit, then:

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

## Three things that will bite you

**Keep 5 GHz off the DFS channels.** Routers love to auto-select channels
100–140, which are radar-protected. On those a client may not probe actively -
it must passively wait for a beacon - and at marginal signal it associates,
reports "connected", and then never completes DHCP. The box looks perfectly
healthy from the inside and is invisible from the network. Moving to channel 36
took receive from 6 Mbit/s to 325 in the same enclosure. Check this first.

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
