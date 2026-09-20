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
| `web.py` | Card registration, and the admin page. Deliberately tiny. |
| `cardart/` | Finds cover art for albums still awaiting a card and lays it out as print-ready A4 sheets. Off the playback path entirely. |

Card mappings store **library-root-relative paths**, never absolute paths and
never OwnTone's numeric IDs — so a rebuild onto a new SD card doesn't cost you
a single re-registration.

Every album is meant to have exactly one card. Nothing in the format enforces
it, so the admin page flags any album holding more than one — with a hundred-odd
cards registered, a duplicate is otherwise invisible until two of them turn out
to play the same record.

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

**Leave `RSTPDN` jumpered to `D20`.** The service pulses that line to reset the
PN532 when it stops responding, which a restart can cause. Without the jumper a
wedged reader needs a power cycle.

`provision.sh` handles the Pi side: freeing the serial console and putting the
real PL011 UART on GPIO14/15 (the mini-UART's baud rate drifts with the VPU
clock and gives a reader that works only intermittently).

### Card types

**For new cards, buy NTAG213 or NTAG215.** Different tags report presence very
differently: motionless on the same reader, an NTAG213 held continuously for 11
seconds, while a 4-byte card was reported absent and re-found about 29 times a
second. That flicker is how the PN532 answers a re-select for that technology,
not the card moving — and the debounce absorbs it completely, with 19x margin
measured. Both kinds work; NTAG simply starts with more room.

The UID tells you which you have — 7 bytes starting `04` is an NTAG, 4 bytes is
Mifare-Classic-style. Insist on the chip name in the listing; "13.56MHz NFC
sticker" with no chip named is usually the wrong one. 213, 215 and 216 differ
only in memory, which does not matter here: the card carries no data, only its
UID.

Presence is debounced in software to cope, tuned by `presence_debounce_s`. If
cards stutter or fail to play, measure yours with `spikes/presence_check.py`
and raise that value. Measure with your least reliable card, not the first one
to hand.

### If it goes in an enclosure

Test networking in its final position before assembling everything, and **put
it on 2.4 GHz**. This board's 5 GHz receive path does not work: it associates,
completes the handshake, reports itself connected — and receives nothing. Not
"less", nothing. Transmit negotiates 390 Mbit/s while receive sits pinned at
6 Mbit/s, the floor, and zero bytes arrive. DHCP timing out is the first
symptom you notice, not the fault.

This was blamed on signal margin for a long time, and that was wrong: it fails
at -63 dBm just as completely as at -69, while a Mac in the same room uses the
same 5 GHz AP at -73 dBm without trouble. Don't go hunting for a few dB.

The failure is binary rather than gradual, and it does not announce itself.
[`docs/runbook.md`](docs/runbook.md) has the measurements and the four
plausible explanations that turned out to be wrong.

## Getting started

Burn Raspberry Pi OS Lite 64-bit, then:

```bash
git clone <this repo> /home/pi/owntone-nfc
sudo bash /home/pi/owntone-nfc/deploy/provision.sh
```

Copy albums to `/srv/music/<Artist>/<Album>/` (Samba share at
`smb://jukebox.local`), then open **http://jukebox.local:8080**, tap a card,
pick an album, save.

The same page collects cover art for albums that have no card yet, so you can
print a sheet of them. A search that picks the wrong cover can be corrected by
pinning a specific image URL; those corrections are hand-made judgements and
`backup.sh` keeps them alongside the card registry.

Pick your speakers in OwnTone at **http://jukebox.local:3689**. The jukebox
remembers them.

### Registering a stack of cards

Kneeling next to the box to register a shelf of albums gets old. Plug a USB
NFC reader into your laptop instead:

```bash
python3 -m pip install pyscard
python3 tools/desk_reader.py --host jukebox.local:8080
```

Tap a card, pick the album in the Register tab, save, tap the next. Each tap
prints `known card` or `NEW card`, so you can work through a pile without
watching the browser.

The scan is **identification only** — it cannot start, stop or switch a
record, so registering at a desk never interrupts what is playing in the
room. `--dry-run` prints UIDs without telling the jukebox, and `--silence`
turns off the reader's beep for good.

Tested with an ACS ACR122U, which reports UIDs byte-identical to the PN532 on
the box. Worth confirming that for any other reader before registering a
stack: scan a card you have already registered and check it matches
`cards.yaml`. Some readers report the UID byte-reversed, which would leave
every card you register pointing at nothing.

## Documentation

- **[`docs/runbook.md`](docs/runbook.md)** — hardware settings, the Wi-Fi and
  AirPlay gotchas, a symptom table, and how to rebuild. **Read this first when
  something is broken.**
- **[`docs/superpowers/specs/`](docs/superpowers/specs/)** — the design, and
  what hardware testing proved wrong about it.

## One thing worth knowing

**`user_agent = "AirPlay/999.0.0"` is required in `owntone.conf`.** Apple OS 27
gates `GET /info` on the User-Agent and returns 403 to OwnTone's default, so
AirPlay speakers stop working as they update. `provision.sh` sets it. Upstream:
[owntone#2042](https://github.com/owntone/owntone-server/issues/2042).

Environment-specific troubleshooting — networking, enclosures, recovery — is in
[`docs/runbook.md`](docs/runbook.md).

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
