# NFC Vinyl Jukebox — Design

**Date:** 2026-09-18
**Status:** Approved, pending Spike 0

## Goal

Rebuild a contactless RFID/NFC music player on a Raspberry Pi. Placing an NFC card
on the reader plays an album; removing it stops playback. Audio must be able to go
to a local speaker *or* to AirPlay 2 HomePods, and the HomePods must be released
when playback ends so they are free for other senders.

**North star: simulate vinyl.** Put the record on, it plays from the start. Take it
off, it stops. The physical object is honest — no skip button, no resume, no state
hidden in the card.

## Why not Phoniebox

The previous build used Phoniebox. It remains the dominant project and v3 (Python
rewrite) is near-default, but it is the wrong fit here for one reason: **MPD, its
playback engine, has no AirPlay output at all.**

The only documented Phoniebox+HomePod recipe requires Phoniebox v2 + MPD on a
non-default port + OwnTone + PulseAudio + `snd-aloop` + patched shell scripts. Its
author describes it as "very convoluted" and reports that **v3's PulseAudio layer
would not pair with HomePods**. Adopting it means carrying local patches against a
moving upstream, and local-vs-AirPlay switching stays awkward because MPD has no
concept of AirPlay.

OwnTone (formerly forked-daapd) is the only mature AirPlay 2 *sender* on Linux.
Since v29.0.139 its default AirPlay mode is AirPlay 2 with PTP timing. It treats
local ALSA and AirPlay devices as peer outputs on one synced timeline, and exposes
output selection over a documented REST API. Building on it directly removes the
entire hack stack.

Note: the repo directory is named `phoniebox-nfc` for historical reasons. Phoniebox
is not used.

## Hardware & OS

| Item | Choice | Rationale |
|---|---|---|
| Board | **Pi 4** (3B kept as spare) | Better Wi-Fi radio — AirPlay 2 PTP is sensitive to flaky Wi-Fi. Headroom, USB 3 for a future DAC. |
| OS | **Raspberry Pi OS Bookworm Lite, 64-bit** | Driver/library maturity for the PN532. Nothing here requires Trixie; cheap to revisit. |
| Reader | **PN532 HAT over UART** | `nfcpy` is the library with a real *card-presence* API rather than read-UID-once. It supports PN532 over serial and USB but **not I2C**. The vinyl model depends on reliable presence detection, so UART it is. |
| Audio | Onboard 3.5mm initially | AirPlay bypasses the Pi DAC entirely, so onboard only caps the local path. |

### UART consequence

Set the HAT's DIP switches to UART. Free the serial console and add
`dtoverlay=disable-bt` so the good PL011 UART lands on GPIO14/15 instead of the
mini-UART. Wi-Fi is unaffected; we give up onboard Bluetooth, which costs nothing
in this design.

### Audio upgrade path (deliberately deferred)

An I2S DAC HAT needs GPIO18/19/20/21 plus power, which does **not** collide with the
PN532 on UART — so it can stack. A USB DAC avoids the GPIO question entirely and is
the zero-risk option. Neither is needed to ship.

## Architecture

Four pieces, each independently testable:

- **OwnTone** (from its apt repo) — owns the library at `/srv/music`, exposes local
  ALSA + HomePods as selectable outputs, provides the REST API and the player web UI
  on `:3689`. We configure it; we do not modify it.
- **`reader.py`** — wraps `nfcpy`. Emits exactly two events: `card_present(uid)` and
  `card_removed()`. Knows nothing about music.
- **`owntone.py`** — thin REST client. Knows nothing about cards.
- **`controller.py`** — the state machine joining them. The only place vinyl behavior
  lives, and the only part with interesting logic. Unit tested with both `reader` and
  `owntone` mocked.
- **`web.py`** — small admin app on `:8080`.

Runs as a single systemd service, `nfc-jukebox`.

## The vinyl state machine

**Card placed** → resolve UID → clear queue, add album, restore output selection,
play **from track 1**.

**Card removed** → **pause immediately**, start grace timer (default 90s).

- Same card returns within a **bump window** → resume in place. Contactless readers
  drop a read occasionally; restarting the album because someone nudged the box would
  be maddening. **Window length to be set by measurement in Spike 2, not guessed.**
  Prior art suggests sub-second: Phoniebox's equivalent setting has a minimum of 0.2s,
  chosen specifically to ride out brief missed reads. A window of seconds would be too
  long — a deliberate lift-and-replace would wrongly resume instead of restarting.
- Same card returns after the bump window → restart from track 1, per vinyl.
- Different card at any point → switch immediately.
- **Grace expires** → stop, clear queue, deselect AirPlay outputs so the HomePods go
  idle and are free for other senders.

The grace timer is why this does not feel laggy: AirPlay 2's PTP handshake costs a
couple of seconds, so the session is held through short gaps and only truly released
when the user is actually done.

## Output handling

**OwnTone owns output selection. We do not store an output mode.**

Its web UI already ships per-output volume and speaker selection, so duplicating that
would mean building a worse version of an existing UI. Instead the controller:

1. On release: **snapshots the currently selected outputs to disk**, then deselects
   the AirPlay ones.
2. On next card: **restores that snapshot** via `PUT /api/outputs/set`.

The user picks speakers in OwnTone's UI like any other AirPlay app and it sticks.
"It lives in one mode" falls out for free; switching modes is a checkbox in a UI that
already exists. Playing to local and AirPlay simultaneously is just a two-element
list — a mode, not an architecture.

**Guard rail:** if outputs are already selected while the box is idle (the user chose
them by hand), respect that rather than clobbering it with the snapshot.

**The snapshot is persisted to disk, not held in memory**, so a reboot does not lose
the speaker choice. *"Output actually sticks" is an explicit acceptance criterion,
verified across both a release cycle and a reboot.*

## Card mapping

`/etc/nfc-jukebox/cards.yaml`, keyed by UID hex:

```yaml
04a2b3c4d5e680:
  name: "Kind of Blue"
  path: "Miles Davis/Kind of Blue"   # relative to the library root
```

Entries store a **library-root-relative path, not an OwnTone numeric ID**, and are
queued via OwnTone's `expression` query against the path.

This is load-bearing for portability. Numeric IDs are assigned at scan time, so they
differ on every rebuild — every mapping would break when redeployed to a new SD card.
A relative path survives a new card, a local→NAS switch, and a change of mount point.
Re-registering cards after a rebuild is the one thing this system must never require.

Consequence: the `library:album:<id>` approach used by the Instructables build is
**ruled out**, not merely riskier. Spike 1 verifies the expression syntax works, not
whether to use it.

## Music library

Local FLAC (and other local files). One folder per album under
`/srv/music/<Artist>/<Album>`, since the card mapping keys off that path prefix.

**Storage: music lives locally on the Pi — USB SSD or a large SD card. A NAS, if
used, is the master copy that we sync *from*, not a mount we play *through*.**

Two documented problems rule out mounting the NAS directly:

1. **No real-time updates.** OwnTone's docs: "if you have your library on a network
   mount then real time updating may not work" — most network filesharing protocols
   send no change notifications. Requires cron-driven `.init-rescan` trigger files.
2. **Boot-order fragility.** OwnTone issue #690: if the share is not mounted when
   OwnTone starts, the music stays unavailable *even after the share remounts*, and
   only a full rescan recovers it. A Pi that boots faster than the NAS wakes gives a
   silent box, and the failure is invisible until someone taps a card.

For an appliance that should behave like a record player, "works only if the NAS
happened to be awake first" is disqualifying. Local storage gives real-time inotify
updates, makes boot order irrelevant, and keeps the box working when the NAS or
network is down. A 256GB card holds a few hundred FLAC albums.

Populated by rsync from the NAS, or drag-and-drop onto the Pi's Samba share.

**Decided: the library root is a configurable mount point.** Both are supported:

- **Local storage** — simplest, real-time inotify updates, boot order irrelevant.
  Constrained by card size: the library is ~56GB and a 64GB card cannot hold it
  alongside the OS, so local means a *subset* until a larger card arrives.
- **NAS mount** — the answer to the capacity squeeze. The two problems above are
  mitigable, not fatal:
  - Boot ordering (#690): `x-systemd.automount` in fstab plus a
    `RequiresMountsFor=/srv/music` drop-in on `owntone.service`, so OwnTone cannot
    start before the mount exists.
  - No inotify: a cron job dropping an `.init-rescan` trigger file — OwnTone's own
    documented workaround.

Either can be chosen at deploy time without touching the card mappings.

Out of scope: Spotify, web radio, podcasts. (Apple Music is not possible — there is
no usable Linux client.)

### Audio quality ceiling — AirPlay vs local

AirPlay 2 to a HomePod is **ALAC 16-bit/44.1kHz**; OwnTone transcodes to it. Any
24/96 FLAC will never reach the HomePods at full resolution. This is an Apple
protocol ceiling, not an OwnTone limitation.

Consequence: **hi-res only ever pays off on the local output.** That is the real
argument for eventually adding an I2S or USB DAC. Over AirPlay, CD quality is the
ceiling regardless of spend — which is also why onboard audio is an acceptable
starting point.

## Web admin (`:8080`)

Scope is deliberately minimal — only what OwnTone cannot already do:

- **Learn mode** — live-displays the UID of whatever card is tapped; pick a folder,
  save. This is the card re-registration story: existing physical cards work
  unchanged because only their UID is ever read.
- **Mapping table** — view, edit, delete.
- **Status line** — current state and last error.

Everything player-related (transport, volume, queue, library browsing, speaker
selection) links out to OwnTone on `:3689`, which already provides all of it.

Explicitly out of scope: transport controls, resume-position, per-card output
settings, GPIO buttons, rotary encoder, status LED.

## Failure handling

**AirPlay takeover is expected behavior, not an error.** AirPlay receivers tear down
the current session when a new sender connects. Tapping a card while a podcast plays
in the kitchen will stop the podcast, exactly as hitting AirPlay from a phone would.
Nothing to build; it is the behavior inherent to the protocol.

The genuine failure mode is narrower: the device is unreachable, or mDNS has not
discovered it yet (notably in the first ~30s after boot). Then: **play locally
anyway and surface it on the status line. Never leave the box silent.**

Known Apple-side gotcha, documented by OwnTone: if a speaker deselects itself the
instant playback starts (log shows `ANNOUNCE request failed in session startup: 400
Bad Request`), the fix is **Apple Home → Allow Speakers & TV Access → "Anyone On the
Same Network"**. Apple's default of *"Only People Sharing This Home"* cannot be
satisfied by a Linux sender. This presents as a compatibility failure but is a
permissions one.

## Spikes — before building

### Spike 0: HomePod pair reachability (highest risk, do first)

Can OwnTone drive the HomePod stereo pair given it is associated with an Apple TV?

Standalone HomePods and stereo pairs are well-proven. HomePods **bound to an Apple TV
as home-theater speakers** are not covered in OwnTone's docs, and the Phoniebox
builder reported exactly this case as failing. In that configuration HomePods
generally stop advertising as independent AirPlay receivers.

Steps: install OwnTone, set Apple Home → Allow Speakers & TV Access → "Anyone On the
Same Network", check whether the pair appears and plays.

Fallbacks in order:
1. PIN-pair the Apple TV itself as the AirPlay target (*Settings → Remotes &
   Outputs*; required for Apple TV4 / tvOS 10.2+).
2. Un-bind the HomePods from the Apple TV so they are standalone targets — the
   known-good configuration.

**This gates the rest of the build. Everything else assumes AirPlay output works.**

### Spike 1: path-expression queueing

Verify `POST /api/queue/items/add?expression=...` reliably queues a whole album by
path prefix, in correct track order. ~10 minutes. Falls back to numeric IDs.

### Spike 2: PN532 presence polling over UART

Verify `nfcpy` gives stable continuous presence detection, and **measure the real
dropped-read rate to set the bump window**. Expect sub-second. Phoniebox has an open
issue, #1991, titled "Placing card starts the player, removal stops it doesn't work
anymore" — presence detection is the fragile part of the incumbent, which is a good
reason to own this state machine and to characterise it with real numbers.

## Testing

`controller.py` is the testable core, with `reader` and `owntone` mocked:

- card placed → queue cleared, album added, playback starts at track 1
- card removed → pause is immediate
- same card within bump window → resumes in place, no restart
- same card after bump window → restarts from track 1
- different card mid-playback → switches immediately
- grace expiry → stop, queue cleared, AirPlay outputs deselected
- output snapshot restored on next card
- outputs already selected while idle → snapshot does not clobber
- AirPlay output unreachable → falls back to local, playback continues, error surfaced

Integration, on-device and manual: output selection survives a full release cycle
**and a reboot**.

## Prior art

Surveyed before building. Nobody has built this exact combination (OwnTone + PN532
presence detection + vinyl semantics + local/AirPlay peer outputs).

| Project | Status | Why not this |
|---|---|---|
| [lennyomg "Record Player"](https://www.instructables.com/Record-Player-a-Real-Player-That-Plays-Fake-Record/) | Published Apr 2025 | **Strongest prior art — independently arrived at this same architecture.** |
| [cleverdevil/musicbox](https://github.com/cleverdevil/musicbox) | Archived, 1★, Jan 2023 | Closest in intent — "NFC-powered AirPlay music box". Two Python files, tap-to-play only, AirPlay-only, one hardcoded HomePod, maps tags by filename (`[tagid].mp3`). Uses **pyatv**, not OwnTone. |
| [TagTuner](https://github.com/luka6000/TagTuner) + Music Assistant | 99★, active | The real competing ecosystem, and its pitch matches the vinyl goal. Rejected on evidence — see below. |
| [Gudsfile/jukebox](https://github.com/Gudsfile/jukebox) | 4★, active | Closest philosophically ("vinyl or CD tagged with NFC tags") but targets Spotify/Sonos. |
| [zacharycohn/jukebox](https://github.com/zacharycohn/jukebox) | 20★, stale 2023 | Sonos. |
| [layereight/nfc-music-box](https://github.com/layereight/nfc-music-box) | 51★, active | A build guide, not software. |

### Why not Music Assistant

Music Assistant is mature, actively developed, and runs standalone (Home Assistant
optional), so it was a serious candidate. Rejected on a documented technical point:
its own docs state the **AirPlay 2 implementation "is new and has not yet been
extensively tested"**, and specifically that **PTP timing is not yet supported**.

OwnTone has defaulted to AirPlay 2 *with* PTP since v29.0.139. For a HomePod stereo
pair, PTP is what keeps the two speakers in sync. This is the deciding factor.

### Borrowed

- **pyatv** as a Spike 0 diagnostic: `atvremote scan` enumerates what the HomePod pair
  actually advertises, far faster than diagnosing through OwnTone's UI.
- **Phoniebox's 0.2s grace-period minimum** as calibration for the bump window.

### The "Record Player" build (closest match)

An Instructables build from April 2025 independently reached four-for-four on our
load-bearing decisions: **OwnTone** as the media server, **PN532** as the reader,
output to "local speakers or Apple HomePods", and a **Python app driving OwnTone via
its API**. Someone has shipped this architecture and lived with it.

Differences, and what we take from them:

- **It maps tags to numeric `library:album:123456789` IDs** — the thing Spike 1 was
  designed to avoid. Someone running this in production on numeric IDs is evidence
  they may be stable enough in practice, which makes Spike 1 cheaper: if path
  expressions give trouble, numeric IDs are a proven fallback rather than a guess.
- **Output switching is a physical rotary switch** (local vs remote speakers) — the
  same insight as our snapshot-and-restore, implemented in hardware. Confirms that
  "it lives in one mode" is how these actually get used.
- It also streams Spotify; we are local-files-only. No architectural impact.
- Its physical build (N20 motor spinning a printed disk, NeoPixels, Li-Po + PowerBoost,
  XIAO satellite board, 3D-printed body) is out of scope here.

## Verified against OwnTone's source (2026-09-18)

Two assumptions in the original design were wrong, and both would have produced a
device that silently did not work. Neither was catchable by the test suite, because
the mocks were written from the same assumptions as the code. Both were settled by
reading OwnTone's C source rather than waiting for hardware.

### Output `type` strings

From the `.name` field of each `struct output_definition` in `src/outputs/*.c`:

| Backend | `type` value |
|---|---|
| airplay.c | `AirPlay 2` |
| raop.c | `AirPlay 1` |
| alsa.c | `ALSA` |
| pulse.c | `Pulseaudio` |
| cast.c | `Chromecast` |
| streaming.c | `streaming` |

The code compared `type == "airplay"`, which matches **nothing**. Consequences:
`airplay_output_ids()` always returned empty, `local_output_ids()` returned every
output *including the HomePods*, and `_release()` re-selected them — so the HomePods
would never have been released. That is the headline requirement, failing silently.

Two corrections follow:
- AirPlay detection is a casefolded prefix match, covering AirPlay 1 and 2.
- "Local" is an **allow-list** (`ALSA`, `Pulseaudio`), never "anything not AirPlay".
  A deny-list would have selected a neighbour's Chromecast and OwnTone's HTTP
  `streaming` output on every release.

### Smart-playlist expression grammar

From `src/parsers/smartpl_lexer.l`: valid tokens include `path` (string tag),
`includes`, `starts with`, `order by`, `asc`/`desc`, and the integer tags **`disc`**
and **`track`**.

`disc_number` / `track_number` are **JSON-API track-object field names**, not
expression grammar. The original expression would have failed to parse, returning
400 on every card tap — nothing would ever have played. Confusing these two
vocabularies is the whole bug.

### The lesson for the remaining spikes

Source-reading closed two binary risks cheaply, but it is not a running server.
Still unverified and still requiring Spikes 0-2:

- whether OwnTone *parses* the corrected expression, and whether `path` is the full
  filesystem path that the anchoring fix assumes
- whether `PUT /api/outputs/set` actually raises for a powered-off speaker, or
  returns 204 and fails asynchronously — in which case the box goes silent and no
  current code path notices
- the real PN532 reacquisition time, which sets the bump window
