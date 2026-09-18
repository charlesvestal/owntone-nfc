# NFC Vinyl Jukebox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Raspberry Pi NFC jukebox where placing a card plays an album and removing it stops playback, with audio to a local speaker and/or AirPlay 2 HomePods.

**Architecture:** OwnTone owns the music library, playback, and audio outputs (local ALSA + AirPlay 2 with PTP). A small Python service reads card presence from a PN532 over UART and drives OwnTone through its JSON REST API. A minimal web admin handles card registration only; everything player-related belongs to OwnTone's existing UI.

**Tech Stack:** Raspberry Pi 4 · Raspberry Pi OS Bookworm Lite 64-bit · OwnTone (apt) · Python 3.11 · nfcpy · httpx · Flask · pytest · systemd

**User decisions (already made):**
- "A is fine" — OwnTone as the engine with our own NFC daemon, not Phoniebox.
- "the idea is to simulate a vinyl functionality" — card on plays from track 1, card off stops.
- "Leave card on reader = plays; remove = pause."
- "No resume" — always start from the beginning.
- "why not just always output to both? web page is fine, we want the flexibility but it will 'live' in one mode."
- "so long as output actually sticks" — output persistence is an explicit acceptance criterion.
- "skipping would be in owntone's ui anyway" — build no transport controls.
- "None — cards only, volume from HomePod/phone" — no GPIO buttons, encoder, or LED.
- "we will be using all local flac files" — local files only, no Spotify.
- "can always be local if that's a constraint, it's fine" — local storage, no NAS mount.
- "let's name the repo owntone-nfc."

**Reference:** `docs/superpowers/specs/2026-09-18-nfc-vinyl-jukebox-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `src/nfc_jukebox/owntone.py` | Thin OwnTone REST client. Knows nothing about cards. |
| `src/nfc_jukebox/cards.py` | Load/save `cards.yaml`, normalise UIDs. Knows nothing about playback. |
| `src/nfc_jukebox/outputs.py` | Output snapshot persistence (save/load selected output IDs to disk). |
| `src/nfc_jukebox/reader.py` | nfcpy wrapper emitting `card_present(uid)` / `card_removed()`. Plus `FakeReader` for tests. |
| `src/nfc_jukebox/controller.py` | The vinyl state machine. The only interesting logic; fully unit tested. |
| `src/nfc_jukebox/web.py` | Flask admin: learn mode, mapping table, status. |
| `src/nfc_jukebox/config.py` | Typed settings loaded from `/etc/nfc-jukebox/config.yaml`. |
| `src/nfc_jukebox/__main__.py` | Wires reader + controller + web together; entrypoint. |
| `deploy/nfc-jukebox.service` | systemd unit. |
| `deploy/owntone.conf.example` | Reference OwnTone config. |
| `docs/runbook.md` | Spike results, hardware settings, recovery steps. |

Split by responsibility, not layer. `controller.py` is the only file that knows both about cards and about playback.

---

## Phase 1 — Hardware and OS (Tasks 0–3)

### Task 0: Burn and boot the OS

**Goal:** A headless Pi 4 running Raspberry Pi OS Bookworm Lite 64-bit, reachable over SSH.

**Files:**
- Create: `docs/runbook.md`

**Acceptance Criteria:**
- [ ] `ssh jukebox.local` succeeds from the Mac without a password prompt
- [ ] `cat /etc/os-release` shows `VERSION_CODENAME=bookworm`
- [ ] `uname -m` prints `aarch64`

**Verify:** `ssh jukebox.local 'cat /etc/os-release | grep VERSION_CODENAME && uname -m'` → `VERSION_CODENAME=bookworm` and `aarch64`

**Steps:**

- [ ] **Step 1: Install Raspberry Pi Imager on the Mac**

```bash
brew install --cask raspberry-pi-imager
```

- [ ] **Step 2: Choose the correct OS image**

In Imager: *Choose Device* → Raspberry Pi 4. *Choose OS* → **Raspberry Pi OS (other)** → **Raspberry Pi OS Lite (64-bit)**.

**This must say Bookworm.** Imager now defaults to Trixie. If the Lite 64-bit entry shows Trixie, scroll to the bottom of the OS list and pick **Raspberry Pi OS (Legacy, 64-bit) Lite** — the Bookworm release. Bookworm is chosen for PN532 library maturity; see the spec.

- [ ] **Step 3: Configure via the Imager gear icon before writing**

Set: hostname `jukebox`, enable SSH with public-key authentication (paste `~/.ssh/id_ed25519.pub`), username `pi`, Wi-Fi SSID/password, locale/timezone.

- [ ] **Step 4: Write the image, boot the Pi, and connect**

```bash
ssh pi@jukebox.local
```

- [ ] **Step 5: Update packages**

```bash
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

- [ ] **Step 6: Record the result in the runbook**

```bash
cat > docs/runbook.md <<'EOF'
# Jukebox Runbook

## Hardware
- Raspberry Pi 4
- PN532 NFC HAT (DIP switches: UART)
- Audio: onboard 3.5mm (initial)

## OS
- Raspberry Pi OS Bookworm Lite 64-bit
- Hostname: jukebox.local, user: pi

## Spike results
(filled in by Tasks 4-6)
EOF
```

- [ ] **Step 7: Commit**

```bash
git add docs/runbook.md
git commit -m "docs: add runbook with OS and hardware baseline"
```

---

### Task 1: Configure UART for the PN532

**Goal:** The PL011 UART is available at `/dev/ttyAMA0` with no login console on it, so nfcpy can talk to the PN532.

**Files:**
- Modify: `/boot/firmware/config.txt` (on the Pi)
- Modify: `/boot/firmware/cmdline.txt` (on the Pi)
- Modify: `docs/runbook.md`

**Acceptance Criteria:**
- [ ] `/dev/ttyAMA0` exists
- [ ] No `getty` process is attached to `ttyAMA0`
- [ ] `cmdline.txt` contains no `console=serial0` entry
- [ ] The `pi` user is in the `dialout` group

**Verify:** `ssh jukebox.local 'ls -l /dev/ttyAMA0; systemctl is-enabled serial-getty@ttyAMA0.service; groups pi'` → device exists, getty reports `masked` or `disabled`, groups include `dialout`

**Steps:**

- [ ] **Step 1: Set the PN532 HAT DIP switches to UART mode**

Physical step. On a Waveshare PN532 HAT the switches are labelled for I2C / SPI / UART — set UART. Power the Pi off before changing them.

We use UART rather than I2C because **nfcpy supports the PN532 over serial and USB but not I2C**, and nfcpy is the library with a real card-presence API. Presence detection is the whole design.

- [ ] **Step 2: Disable the serial console**

```bash
sudo raspi-config nonint do_serial_hw 0
sudo raspi-config nonint do_serial_cons 1
```

`do_serial_hw 0` enables the serial port hardware; `do_serial_cons 1` disables the login shell over serial.

- [ ] **Step 3: Move the PL011 UART to the GPIO header**

```bash
echo 'dtoverlay=disable-bt' | sudo tee -a /boot/firmware/config.txt
sudo systemctl disable hciuart
```

Without this, GPIO14/15 get the mini-UART, whose baud rate is tied to the VPU clock and drifts. `disable-bt` gives up onboard Bluetooth — which costs nothing in this design — and hands the good PL011 to the header. Wi-Fi is unaffected.

- [ ] **Step 4: Grant serial access and reboot**

```bash
sudo usermod -aG dialout pi
sudo reboot
```

- [ ] **Step 5: Verify**

```bash
ssh jukebox.local 'ls -l /dev/ttyAMA0 && groups pi'
```

Expected: `/dev/ttyAMA0` listed with group `dialout`, and `pi` in the `dialout` group.

- [ ] **Step 6: Record in the runbook and commit**

Append to `docs/runbook.md`:

```markdown
## UART
- PN532 HAT DIP switches: UART
- dtoverlay=disable-bt (PL011 on GPIO14/15, onboard Bluetooth given up)
- serial console disabled; device is /dev/ttyAMA0
```

```bash
git add docs/runbook.md
git commit -m "docs: record UART configuration for PN532"
```

---

### Task 2: Music storage and Samba share

**Goal:** `/srv/music` exists on the SD card with `<Artist>/<Album>` layout, seeded with a subset of the library, writable from the Mac over Samba.

**Files:**
- Modify: `/etc/samba/smb.conf` (on the Pi)
- Modify: `docs/runbook.md`

**Capacity note.** The full library is ~56GB of FLAC; a 64GB card gives ~59.6GiB usable
and the OS takes ~2.5GB. That leaves under 1GB of headroom, and a near-full filesystem
will corrupt OwnTone's SQLite database. **So: seed a subset now (target ≤ 20GB) and
prove the system.** Two ways out later, both supported and neither requiring cards to be
re-registered: a larger SD card (Task 14), or mounting the NAS (Task 15).

**Acceptance Criteria:**
- [ ] `/srv/music` exists, owned by `pi`
- [ ] The share mounts from macOS Finder at `smb://jukebox.local/music`
- [ ] At least 5 albums present under `<Artist>/<Album>/`
- [ ] `df -h /` shows at least 20GB free after copying

**Verify:** `ssh jukebox.local 'ls /srv/music && df -h / | tail -1'` → artist directories listed, ≥20GB available

**Steps:**

- [ ] **Step 1: Create the library root**

```bash
sudo mkdir -p /srv/music
sudo chown -R pi:pi /srv/music
```

Layout is `/srv/music/<Artist>/<Album>/`. This matters more than it looks: card mappings
key off the album path prefix, which is what makes them survive a rebuild onto a new SD
card. OwnTone's numeric library IDs would not.

`/srv/music` is a *library root*, not necessarily a local directory — Task 15 can turn it
into a NAS mount without touching a single card mapping, because mappings store paths
relative to this root.

- [ ] **Step 2: Install and configure Samba**

```bash
sudo apt install -y samba
sudo tee -a /etc/samba/smb.conf <<'EOF'

[music]
   path = /srv/music
   browseable = yes
   read only = no
   force user = pi
EOF
sudo smbpasswd -a pi
sudo systemctl restart smbd
```

- [ ] **Step 3: Copy a subset of albums from the Mac**

In Finder: *Go → Connect to Server* → `smb://jukebox.local` → `music`. Copy **5–10 albums**
(≤20GB total) preserving `<Artist>/<Album>/` structure. Pick albums you will actually
map to cards in Task 15, so the end-to-end test is real.

- [ ] **Step 4: Verify capacity and layout**

```bash
ssh jukebox.local 'find /srv/music -mindepth 2 -maxdepth 2 -type d | head && df -h /'
```

Expected: album directories listed, and the root filesystem showing ≥20GB available.

- [ ] **Step 5: Record in the runbook and commit**

Append to `docs/runbook.md`:

```markdown
## Music storage
- /srv/music on the SD card, layout <Artist>/<Album>/
- Currently a SUBSET of the ~56GB library (64GB card cannot hold it all)
- Larger card migration: see deploy/provision.sh and Task 14
```

```bash
git add docs/runbook.md
git commit -m "docs: record music storage layout and Samba share"
```

### Task 3: Install and configure OwnTone

**Goal:** OwnTone is running from its apt repo, has scanned `/srv/music`, and its web UI is reachable.

**Files:**
- Create: `deploy/owntone.conf.example`
- Modify: `docs/runbook.md`

**Acceptance Criteria:**
- [ ] `systemctl is-active owntone` returns `active`
- [ ] `http://jukebox.local:3689` loads the OwnTone web UI
- [ ] `GET /api/library` reports a non-zero track count
- [ ] Local ALSA output appears in `GET /api/outputs`

**Verify:** `curl -s http://jukebox.local:3689/api/library | python3 -m json.tool | grep songs` → non-zero `songs` count

**Steps:**

- [ ] **Step 1: Add the OwnTone apt repository**

```bash
wget -q -O - https://raw.githubusercontent.com/owntone/owntone-apt/refs/heads/master/repo/rpi/owntone.gpg \
  | sudo gpg --dearmor --output /usr/share/keyrings/owntone-archive-keyring.gpg
sudo wget -q -O /etc/apt/sources.list.d/owntone.list \
  https://raw.githubusercontent.com/owntone/owntone-apt/refs/heads/master/repo/rpi/owntone-bookworm.list
sudo apt update && sudo apt install -y owntone
```

We install from apt rather than building from source (as the Instructables build did) because apt gives us upgrades for free.

- [ ] **Step 2: Configure OwnTone**

```bash
sudo tee /etc/owntone.conf >/dev/null <<'EOF'
general {
        uid = "owntone"
        admin_password = ""
        ipv6 = no
}

library {
        name = "Jukebox"
        port = 3689
        directories = { "/srv/music" }
        follow_symlinks = true
}

audio {
        nickname = "Local"
        type = "alsa"
        card = "default"
}
EOF
sudo systemctl restart owntone
```

`ipv6 = no` is deliberate: OwnTone's own troubleshooting docs note that some speakers advertise IPv6 support but then fail to play over it.

- [ ] **Step 3: Confirm the library scanned**

```bash
curl -s http://jukebox.local:3689/api/library | python3 -m json.tool
```

Expected: a JSON object with a non-zero `songs` value.

- [ ] **Step 4: Confirm local audio output**

```bash
curl -s http://jukebox.local:3689/api/outputs | python3 -m json.tool
```

Expected: an output with `"type": "alsa"` and `"name": "Local"`.

- [ ] **Step 5: Save the reference config and commit**

```bash
mkdir -p deploy
ssh jukebox.local 'cat /etc/owntone.conf' > deploy/owntone.conf.example
git add deploy/owntone.conf.example docs/runbook.md
git commit -m "feat: add OwnTone reference configuration"
```

---

## Phase 2 — Spikes (Tasks 4–6)

These run before any application code exists. They are deliberately first: each one can
invalidate a design assumption, and finding that out now costs an evening instead of a
rewrite.

### Task 4: Spike 0 — prove the AirPlay send path (Mac first, then HomePods)

**Goal:** Prove OwnTone can select and play to an AirPlay 2 receiver — staged: the Mac as a positive control, then the HomePod stereo pair.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `docs/runbook.md`

**Why this is the gate.** Standalone HomePods and stereo pairs are well-proven with
OwnTone. HomePods **bound to an Apple TV as home-theater speakers** are not covered in
OwnTone's docs, and the one builder who documented a Phoniebox+HomePod setup reported
exactly this configuration as the case that failed. Every remaining task assumes AirPlay
output works.

**Why stage it through the Mac.** Testing against the Mac separates "can OwnTone send
AirPlay at all" from "will this particular Apple TV / HomePod arrangement cooperate" —
two failures that look identical from the Pi. It also keeps the HomePods quiet during
development.

**Why the Mac rather than shairport-sync.** shairport-sync is a third-party
reimplementation of AirPlay; the Mac runs Apple's own AirPlay 2 receiver stack, the same
lineage as the HomePod. A pass against the Mac carries real information about HomePod
compatibility; a pass against shairport-sync mostly tests shairport-sync. Install it
later if a permanently silent sink is wanted, but do not make it the gate.

**Read the stages asymmetrically.** A Mac success proves the send path works end to end.
A Mac *failure* does **not** predict HomePod failure — they are different authentication
paths. Never abandon the design on a Stage A failure alone; always run Stage B.

**Acceptance Criteria:**
- [ ] **Stage A:** the Mac appears as an `airplay` output and plays audibly
- [ ] `GET /api/outputs` lists the HomePod pair as an output with `"type": "airplay"`
- [ ] Selecting that output and playing a track produces **audible sound from the HomePods**
- [ ] Playback survives 60 seconds without the output deselecting itself
- [ ] Deselecting the output releases the HomePods (they return to idle)
- [ ] The working configuration is written into `docs/runbook.md`

**Verify:** `curl -s http://jukebox.local:3689/api/outputs | python3 -m json.tool | grep -A3 airplay` → HomePod output present with `"selected": true` during playback

**Steps:**

#### Stage A — the Mac as a positive control

- [ ] **Step A1: Configure the Mac's AirPlay receiver correctly**

*System Settings → General → AirDrop & Handoff → AirPlay Receiver*:

- **Allow AirPlay for: "Anyone on the Same Network"** — not "Current User". The Pi is not
  signed into your Apple ID, so "Current User" makes it invisible.
- **"Require password": OFF.** This is not optional. OwnTone cannot send an AirPlay
  password — [issue #1385](https://github.com/owntone/owntone-server/issues/1385), still
  open — and fails with a 500 and `requires a valid PIN or password` in the log. Leaving
  this on produces a failure that has nothing to do with your speakers.

- [ ] **Step A2: Turn the Mac's system volume down before anything plays**

Set it low but not muted — you need to hear *something* to confirm the path works.

- [ ] **Step A3: Confirm the Mac appears and plays**

```bash
curl -s http://jukebox.local:3689/api/outputs | python3 -m json.tool | grep -B2 -A3 airplay
MAC=$(curl -s http://jukebox.local:3689/api/outputs \
  | python3 -c "import sys,json; print([o['id'] for o in json.load(sys.stdin)['outputs'] if o['type']=='airplay'][0])")
curl -s -X PUT -H 'Content-Type: application/json' \
  -d "{\"outputs\":[\"$MAC\"]}" http://jukebox.local:3689/api/outputs/set
curl -s -X POST "http://jukebox.local:3689/api/queue/items/add?expression=media_kind+is+music&limit=1&clear=true&playback=start"
```

Expected: audible music from the Mac's speakers. Approve the pairing prompt if one
appears — the Mac raises one on each reconnect, which is normal and not a HomePod signal.

- [ ] **Step A4: Record the outcome**

If Stage A passes, the OwnTone AirPlay send path is proven and any Stage B failure is
specific to the Apple TV / HomePod arrangement. Note that distinction in the runbook —
it is what tells you which fallback to reach for.

#### Stage B — the HomePods

**Never blast the HomePods.** Output volume is settable *before* playback starts and is
independent of the queue, so every Stage B test begins by turning the target down. This
is the point of Step 2 below — do not skip it and "just be quick".

- [ ] **Step 1: Fix the Apple-side permission FIRST**

In the **Home app** on iPhone: *Home Settings → Allow Speakers & TV Access → **Anyone On
the Same Network***.

Do this before diagnosing anything else. Apple's default is *"Only People Sharing This
Home,"* which a Linux sender cannot satisfy. The symptom is that the speaker **deselects
itself the instant playback starts**, with `ANNOUNCE request failed in session startup:
400 Bad Request` in the log. It looks exactly like a compatibility failure but is a
permissions one, and it is the most likely cause of the prior builder's reported failure.

- [ ] **Step 2: Enumerate what is actually on the network**

```bash
sudo apt install -y python3-pip
pip3 install --break-system-packages pyatv
atvremote scan
```

This is faster than diagnosing through OwnTone's UI. Record whether the HomePods appear
individually, as a single pair, or only as the Apple TV.

- [ ] **Step 3: Check OwnTone's view**

```bash
curl -s http://jukebox.local:3689/api/outputs | python3 -m json.tool
```

Expected: an entry with `"type": "airplay"` named after the HomePod pair.

- [ ] **Step 4: Select the output, turn it DOWN, then play**

```bash
OUT=$(curl -s http://jukebox.local:3689/api/outputs \
  | python3 -c "import sys,json; print([o['id'] for o in json.load(sys.stdin)['outputs'] if o['type']=='airplay'][0])")
curl -s -X PUT -H 'Content-Type: application/json' \
  -d "{\"outputs\":[\"$OUT\"]}" http://jukebox.local:3689/api/outputs/set

# Turn the HomePods down BEFORE any audio is queued. Per-output volume is
# independent of playback state, so this cannot be "too early".
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"volume": 5}' "http://jukebox.local:3689/api/outputs/$OUT"
curl -s -X POST "http://jukebox.local:3689/api/queue/items/add?expression=media_kind+is+music&limit=1&clear=true&playback=start"
```

Expected: audible music from the HomePods within a few seconds.

- [ ] **Step 5: Watch the log if it fails**

```bash
sudo journalctl -u owntone -f
```

Look for `ANNOUNCE request failed` (→ redo Step 1) or IPv6 errors (→ confirm
`ipv6 = no` in `/etc/owntone.conf`).

- [ ] **Step 6: If it still fails, work the fallbacks in order**

1. **PIN-pair the Apple TV as the target.** OwnTone web UI → *Settings → Remotes &
   Outputs* → select the Apple TV → enter the PIN it displays. Required for Apple TV4 /
   tvOS 10.2+.
2. **Un-bind the HomePods from the Apple TV** in the Home app so they become standalone
   AirPlay targets. This is the known-good configuration.

- [ ] **Step 7: Confirm release works**

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"outputs":[]}' http://jukebox.local:3689/api/outputs/set
```

Expected: music stops and the HomePods return to idle, available to other senders.

- [ ] **Step 8: Record the outcome and commit**

Append to `docs/runbook.md` — which configuration worked, the AirPlay output's name and
id, and whether the Home app permission change was required.

```bash
git add docs/runbook.md
git commit -m "docs: record Spike 0 HomePod reachability results"
```

```json:metadata
{"userGate": true, "tags": ["user-gate", "spike"], "gateScope": "blocks-all-downstream", "verifyCommand": "curl -s http://jukebox.local:3689/api/outputs | python3 -m json.tool", "acceptanceCriteria": ["Stage A: Mac plays audibly as an airplay output", "airplay output listed for HomePods", "audible sound from HomePods", "survives 60s without deselecting", "deselect releases the HomePods", "config recorded in runbook"], "requireEvidenceTokens": [["stage-a", "mac-receiver"], ["stage-b", "homepod"]], "modelTier": "standard"}
```

---

### Task 5: Spike 1 — queue an album by relative path

**Goal:** Determine the exact OwnTone `expression` syntax that queues one album, in correct track order, from a library-root-relative path.

**Files:**
- Modify: `docs/runbook.md`

**Why this matters.** Card mappings store relative paths rather than numeric
`library:album:<id>` values, because numeric IDs are assigned at scan time and differ on
every rebuild — they would break every mapping on a new SD card. This spike verifies the
syntax, not the decision.

**Acceptance Criteria:**
- [ ] A single `POST /api/queue/items/add` call queues exactly one album's tracks
- [ ] Tracks are in disc/track order, not filesystem or alphabetical order
- [ ] No tracks from other albums appear in the queue
- [ ] The verified expression string is recorded in `docs/runbook.md`

**Verify:** `curl -s http://jukebox.local:3689/api/queue | python3 -c "import sys,json; q=json.load(sys.stdin)['items']; print(len(q)); [print(t['track_number'], t['title']) for t in q]"` → count matches the album, numbers ascending from 1

**Steps:**

- [ ] **Step 1: Pick a test album and clear the queue**

```bash
ALBUM="Miles Davis/Kind of Blue"   # replace with a real album from Task 2
curl -s -X PUT http://jukebox.local:3689/api/queue/clear
```

- [ ] **Step 2: Try the primary expression**

```bash
curl -s -G -X POST http://jukebox.local:3689/api/queue/items/add \
  --data-urlencode "expression=path includes \"$ALBUM\" order by disc_number asc, track_number asc" \
  --data-urlencode "clear=true"
```

- [ ] **Step 3: Inspect the queue**

```bash
curl -s http://jukebox.local:3689/api/queue \
  | python3 -c "import sys,json; q=json.load(sys.stdin)['items']; print(len(q),'tracks'); [print(t.get('disc_number'), t.get('track_number'), t['title']) for t in q]"
```

Expected: the album's track count, ascending track numbers, nothing foreign.

- [ ] **Step 4: If ordering is wrong, try without the order clause**

```bash
curl -s -X PUT http://jukebox.local:3689/api/queue/clear
curl -s -G -X POST http://jukebox.local:3689/api/queue/items/add \
  --data-urlencode "expression=path includes \"$ALBUM\"" \
  --data-urlencode "clear=true"
```

Then re-inspect. If OwnTone already returns track order by default, the simpler
expression wins.

- [ ] **Step 5: If `includes` is rejected, try `starts with`**

```bash
curl -s -G -X POST http://jukebox.local:3689/api/queue/items/add \
  --data-urlencode "expression=path starts with \"/srv/music/$ALBUM\"" \
  --data-urlencode "clear=true"
```

Note this variant needs the absolute path, so the code must join the library root before
querying. Record which variant worked — Task 8 encodes it in one constant.

- [ ] **Step 6: Record the verified expression and commit**

Append to `docs/runbook.md`:

```markdown
## Spike 1 result
Verified album expression (used by ALBUM_EXPRESSION in owntone.py):
    <paste the exact working expression string here>
Requires absolute path: yes/no
```

```bash
git add docs/runbook.md
git commit -m "docs: record Spike 1 verified album expression syntax"
```

---

### Task 6: Spike 2 — PN532 presence polling and bump-window measurement

**Goal:** Prove nfcpy gives stable continuous card-presence detection over UART, and measure the real dropped-read rate to set the bump window.

**Files:**
- Create: `spikes/presence_check.py`
- Modify: `docs/runbook.md`

**Why measure rather than guess.** The bump window decides whether a returning card
resumes or restarts. Too short and a momentary dropped read restarts the album; too long
and a deliberate lift-and-replace wrongly resumes. Phoniebox's equivalent setting has a
minimum of 0.2s, chosen specifically to ride out brief missed reads — so the real answer
is expected to be sub-second, not the several seconds originally guessed.

**Acceptance Criteria:**
- [ ] The script reports card arrival within 500ms of placing a card
- [ ] It reports removal within 500ms of lifting a card
- [ ] Over a 5-minute run with a card left stationary, the observed dropped-read gaps are recorded
- [ ] A bump-window value is chosen as `max(observed gap) * 2`, rounded up, and recorded

**Verify:** `ssh jukebox.local 'cd ~/owntone-nfc && python3 spikes/presence_check.py --duration 300'` → prints arrival/removal events and a max-gap summary

**Steps:**

- [ ] **Step 1: Install nfcpy on the Pi**

```bash
sudo apt install -y python3-venv
python3 -m venv ~/jukebox-venv
~/jukebox-venv/bin/pip install nfcpy
```

- [ ] **Step 2: Write the spike script**

```python
# spikes/presence_check.py
"""Measure PN532 card-presence stability over UART.

Prints arrival/removal events and, for a stationary card, the largest gap
between successful presence reads. That gap sets the bump window.
"""
import argparse
import time

import nfc

DEVICE = "tty:AMA0:pn532"
POLL_INTERVAL = 0.05


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=300.0)
    args = parser.parse_args()

    clf = nfc.ContactlessFrontend(DEVICE)
    print(f"Reader open on {DEVICE}. Place a card and leave it.")

    deadline = time.monotonic() + args.duration
    max_gap = 0.0

    try:
        while time.monotonic() < deadline:
            tag = clf.connect(rdwr={"on-connect": lambda tag: False})
            if tag is None:
                continue
            uid = tag.identifier.hex()
            arrived = time.monotonic()
            print(f"[{arrived:10.3f}] PRESENT {uid}")

            last_seen = time.monotonic()
            while tag.is_present:
                now = time.monotonic()
                gap = now - last_seen
                max_gap = max(max_gap, gap)
                last_seen = now
                time.sleep(POLL_INTERVAL)

            removed = time.monotonic()
            print(f"[{removed:10.3f}] REMOVED {uid} after {removed - arrived:.2f}s")
    except KeyboardInterrupt:
        pass
    finally:
        clf.close()

    print(f"\nMax gap between presence reads: {max_gap:.3f}s")
    print(f"Suggested bump window: {max(0.2, max_gap * 2):.2f}s")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run it and exercise the reader**

```bash
~/jukebox-venv/bin/python spikes/presence_check.py --duration 300
```

While it runs: place a card and leave it for several minutes, then lift and replace it a
few times at different speeds.

Expected: `PRESENT` within ~0.5s of placement, `REMOVED` within ~0.5s of lifting, and a
max gap well under 1s.

- [ ] **Step 4: If the reader does not open at all**

```bash
ls -l /dev/ttyAMA0        # device must exist (Task 1)
groups                    # must include dialout
```

If the device is missing, re-check the HAT DIP switches are on UART and that
`dtoverlay=disable-bt` is in `/boot/firmware/config.txt`.

- [ ] **Step 5: Record the measured value and commit**

Append to `docs/runbook.md`:

```markdown
## Spike 2 result
Max observed gap between presence reads: <value>s
Chosen bump_window_s: <value>
Chosen grace_period_s: 90 (design default)
```

```bash
git add spikes/presence_check.py docs/runbook.md
git commit -m "feat: add PN532 presence spike and record measured bump window"
```

---

## Phase 3 — Application (Tasks 7–14)

Written test-first. `controller.py` is the only file with non-trivial logic, so it gets
the bulk of the tests; everything else stays thin enough to be obviously correct.

### Task 7: Project scaffolding and configuration

**Goal:** An installable Python package with pytest wired up and a typed config object.

**Files:**
- Create: `pyproject.toml`
- Create: `src/nfc_jukebox/__init__.py`
- Create: `src/nfc_jukebox/config.py`
- Create: `tests/__init__.py` (empty — makes `tests` importable, required by Task 14)
- Create: `tests/test_config.py`

**Acceptance Criteria:**
- [ ] `pytest` runs and collects tests
- [ ] `Config.load()` reads a YAML file and returns typed values
- [ ] Missing config file yields documented defaults rather than an exception

**Verify:** `pytest tests/test_config.py -v` → 3 passed

**Steps:**

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "nfc-jukebox"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "PyYAML>=6.0",
    "Flask>=3.0",
    "nfcpy>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "respx>=0.21"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_config.py
from pathlib import Path

from nfc_jukebox.config import Config


def test_defaults_when_file_missing(tmp_path):
    cfg = Config.load(tmp_path / "nope.yaml")
    assert cfg.owntone_url == "http://127.0.0.1:3689"
    assert cfg.library_root == Path("/srv/music")
    assert cfg.grace_period_s == 90.0


def test_reads_values_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "owntone_url: http://pi:3689\n"
        "library_root: /mnt/music\n"
        "bump_window_s: 0.4\n"
    )
    cfg = Config.load(path)
    assert cfg.owntone_url == "http://pi:3689"
    assert cfg.library_root == Path("/mnt/music")
    assert cfg.bump_window_s == 0.4


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("owntone_url: http://pi:3689\nnonsense: 1\n")
    cfg = Config.load(path)
    assert cfg.owntone_url == "http://pi:3689"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nfc_jukebox.config'`

- [ ] **Step 4: Implement `config.py`**

```python
# src/nfc_jukebox/config.py
"""Typed settings for the jukebox service."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("/etc/nfc-jukebox/config.yaml")


@dataclass(frozen=True)
class Config:
    owntone_url: str = "http://127.0.0.1:3689"
    library_root: Path = Path("/srv/music")
    cards_file: Path = Path("/etc/nfc-jukebox/cards.yaml")
    outputs_file: Path = Path("/var/lib/nfc-jukebox/outputs.json")
    reader_device: str = "tty:AMA0:pn532"
    # Set from the Spike 2 measurement, not guessed.
    bump_window_s: float = 0.5
    grace_period_s: float = 90.0
    web_port: int = 8080

    _PATH_FIELDS = ("library_root", "cards_file", "outputs_file")

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "Config":
        """Load config, falling back to defaults for anything absent."""
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except FileNotFoundError:
            raw = {}

        known = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in raw.items() if k in known}
        for name in cls._PATH_FIELDS:
            if name in values:
                values[name] = Path(values[name])
        return cls(**values)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
touch tests/__init__.py
git add pyproject.toml src/nfc_jukebox/__init__.py src/nfc_jukebox/config.py tests/__init__.py tests/test_config.py
git commit -m "feat: add project scaffolding and typed configuration"
```

---

### Task 8: OwnTone REST client

**Goal:** A thin, fully tested client for the OwnTone endpoints this project uses.

**Files:**
- Create: `src/nfc_jukebox/owntone.py`
- Create: `tests/test_owntone.py`

**Acceptance Criteria:**
- [ ] `play_album(relpath)` clears the queue and starts playback in one call
- [ ] `selected_output_ids()` returns only outputs with `selected: true`
- [ ] `set_outputs(ids)` PUTs the expected body to `/api/outputs/set`
- [ ] `airplay_output_ids()` and `local_output_ids()` partition outputs by type
- [ ] Every method is tested against a mocked HTTP layer — no live server needed

**Verify:** `pytest tests/test_owntone.py -v` → 7 passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_owntone.py
import httpx
import pytest
import respx

from nfc_jukebox.owntone import OwnTone

BASE = "http://test:3689"

OUTPUTS = {
    "outputs": [
        {"id": "1", "name": "Local", "type": "alsa", "selected": True},
        {"id": "2", "name": "Kitchen", "type": "airplay", "selected": False},
        {"id": "3", "name": "Lounge", "type": "airplay", "selected": True},
    ]
}


@pytest.fixture
def client():
    return OwnTone(BASE)


@respx.mock
def test_selected_output_ids(client):
    respx.get(f"{BASE}/api/outputs").mock(return_value=httpx.Response(200, json=OUTPUTS))
    assert client.selected_output_ids() == ["1", "3"]


@respx.mock
def test_airplay_and_local_partition(client):
    respx.get(f"{BASE}/api/outputs").mock(return_value=httpx.Response(200, json=OUTPUTS))
    assert client.airplay_output_ids() == ["2", "3"]
    respx.get(f"{BASE}/api/outputs").mock(return_value=httpx.Response(200, json=OUTPUTS))
    assert client.local_output_ids() == ["1"]


@respx.mock
def test_set_outputs_sends_expected_body(client):
    route = respx.put(f"{BASE}/api/outputs/set").mock(return_value=httpx.Response(204))
    client.set_outputs(["2", "3"])
    assert route.called
    assert respx.calls.last.request.read() == b'{"outputs": ["2", "3"]}'


@respx.mock
def test_play_album_clears_and_starts(client):
    route = respx.post(url__startswith=f"{BASE}/api/queue/items/add").mock(
        return_value=httpx.Response(200, json={"count": 9})
    )
    client.play_album("Miles Davis/Kind of Blue")
    assert route.called
    url = str(respx.calls.last.request.url)
    assert "clear=true" in url
    assert "playback=start" in url
    assert "Kind+of+Blue" in url or "Kind%20of%20Blue" in url


@respx.mock
def test_pause_and_stop(client):
    pause = respx.put(f"{BASE}/api/player/pause").mock(return_value=httpx.Response(204))
    stop = respx.put(f"{BASE}/api/player/stop").mock(return_value=httpx.Response(204))
    client.pause()
    client.stop()
    assert pause.called and stop.called


@respx.mock
def test_clear_queue(client):
    route = respx.put(f"{BASE}/api/queue/clear").mock(return_value=httpx.Response(204))
    client.clear_queue()
    assert route.called


@respx.mock
def test_raises_on_server_error(client):
    respx.get(f"{BASE}/api/outputs").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        client.selected_output_ids()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_owntone.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nfc_jukebox.owntone'`

- [ ] **Step 3: Implement `owntone.py`**

```python
# src/nfc_jukebox/owntone.py
"""Thin client for the OwnTone JSON API.

Knows nothing about cards or playback policy — it just maps method calls to
OwnTone endpoints.
"""
from __future__ import annotations

import httpx

# Verified in Spike 1 (Task 5). If that spike found a different working syntax,
# this is the single place to change it.
ALBUM_EXPRESSION = 'path includes "{path}" order by disc_number asc, track_number asc'


class OwnTone:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=10.0)

    # --- internal ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self._client.request(method, f"{self._base}{path}", **kwargs)
        response.raise_for_status()
        return response

    def _outputs(self) -> list[dict]:
        return self._request("GET", "/api/outputs").json()["outputs"]

    # --- outputs ----------------------------------------------------------

    def selected_output_ids(self) -> list[str]:
        return [o["id"] for o in self._outputs() if o.get("selected")]

    def airplay_output_ids(self) -> list[str]:
        return [o["id"] for o in self._outputs() if o.get("type") == "airplay"]

    def local_output_ids(self) -> list[str]:
        return [o["id"] for o in self._outputs() if o.get("type") != "airplay"]

    def set_outputs(self, output_ids: list[str]) -> None:
        """Select exactly these outputs; OwnTone deselects all others."""
        self._request("PUT", "/api/outputs/set", json={"outputs": output_ids})

    # --- playback ---------------------------------------------------------

    def play_album(self, relative_path: str) -> None:
        """Clear the queue and play the album at this library-relative path."""
        self._request(
            "POST",
            "/api/queue/items/add",
            params={
                "expression": ALBUM_EXPRESSION.format(path=relative_path),
                "clear": "true",
                "playback": "start",
            },
        )

    def play(self) -> None:
        self._request("PUT", "/api/player/play")

    def pause(self) -> None:
        self._request("PUT", "/api/player/pause")

    def stop(self) -> None:
        self._request("PUT", "/api/player/stop")

    def clear_queue(self) -> None:
        self._request("PUT", "/api/queue/clear")

    def player_state(self) -> dict:
        return self._request("GET", "/api/player").json()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_owntone.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/nfc_jukebox/owntone.py tests/test_owntone.py
git commit -m "feat: add OwnTone REST client"
```

---

### Task 9: Card mapping store

**Goal:** Load and save `cards.yaml`, with UID normalisation so readers that format UIDs differently still match.

**Files:**
- Create: `src/nfc_jukebox/cards.py`
- Create: `tests/test_cards.py`

**Acceptance Criteria:**
- [ ] UIDs normalise to lowercase hex with separators stripped
- [ ] `load()` on a missing file returns an empty mapping, not an error
- [ ] `save()` then `load()` round-trips cleanly
- [ ] Stored paths are library-root-relative, so they survive a rebuild

**Verify:** `pytest tests/test_cards.py -v` → 5 passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cards.py
from nfc_jukebox.cards import Card, CardStore, normalise_uid


def test_normalise_uid_strips_separators_and_lowercases():
    assert normalise_uid("04:A2:B3:C4") == "04a2b3c4"
    assert normalise_uid("04 a2 b3 c4") == "04a2b3c4"
    assert normalise_uid("04A2B3C4") == "04a2b3c4"


def test_load_missing_file_returns_empty(tmp_path):
    store = CardStore(tmp_path / "nope.yaml")
    assert store.load() == {}


def test_round_trip(tmp_path):
    path = tmp_path / "cards.yaml"
    store = CardStore(path)
    store.save({"04a2b3c4": Card(uid="04a2b3c4", name="Kind of Blue",
                                 path="Miles Davis/Kind of Blue")})
    loaded = store.load()
    assert loaded["04a2b3c4"].name == "Kind of Blue"
    assert loaded["04a2b3c4"].path == "Miles Davis/Kind of Blue"


def test_lookup_normalises_incoming_uid(tmp_path):
    path = tmp_path / "cards.yaml"
    store = CardStore(path)
    store.save({"04a2b3c4": Card(uid="04a2b3c4", name="X", path="A/B")})
    assert store.get("04:A2:B3:C4") is not None


def test_get_unknown_uid_returns_none(tmp_path):
    store = CardStore(tmp_path / "cards.yaml")
    assert store.get("deadbeef") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_cards.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nfc_jukebox.cards'`

- [ ] **Step 3: Implement `cards.py`**

```python
# src/nfc_jukebox/cards.py
"""Card UID to album mapping, persisted as YAML.

Paths are stored relative to the library root so a mapping survives a rebuild
onto a new SD card, or a switch between local storage and a NAS mount.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path

import yaml

_SEPARATORS = re.compile(r"[^0-9a-fA-F]")


def normalise_uid(raw: str) -> str:
    """Lowercase hex, separators removed, so formatting differences still match."""
    return _SEPARATORS.sub("", raw).lower()


@dataclass
class Card:
    uid: str
    name: str
    path: str  # relative to the library root


class CardStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, Card]:
        try:
            raw = yaml.safe_load(self._path.read_text()) or {}
        except FileNotFoundError:
            return {}
        return {
            normalise_uid(uid): Card(uid=normalise_uid(uid),
                                     name=entry["name"],
                                     path=entry["path"])
            for uid, entry in raw.items()
        }

    def save(self, cards: dict[str, Card]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            card.uid: {"name": card.name, "path": card.path}
            for card in cards.values()
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=True))
        tmp.replace(self._path)

    def get(self, uid: str) -> Card | None:
        return self.load().get(normalise_uid(uid))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_cards.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/nfc_jukebox/cards.py tests/test_cards.py
git commit -m "feat: add card mapping store with UID normalisation"
```

---

### Task 10: Output snapshot persistence

**Goal:** Persist the selected-output list to disk so the speaker choice survives both a release cycle and a reboot.

**Files:**
- Create: `src/nfc_jukebox/outputs.py`
- Create: `tests/test_outputs.py`

**Acceptance Criteria:**
- [ ] `save()` then `load()` round-trips a list of output IDs
- [ ] `load()` on a missing file returns an empty list
- [ ] `load()` on a corrupt file returns an empty list rather than raising
- [ ] Writes are atomic, so a power cut cannot leave a half-written file

**Verify:** `pytest tests/test_outputs.py -v` → 4 passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_outputs.py
from nfc_jukebox.outputs import OutputSnapshot


def test_round_trip(tmp_path):
    snap = OutputSnapshot(tmp_path / "outputs.json")
    snap.save(["1", "3"])
    assert snap.load() == ["1", "3"]


def test_missing_file_returns_empty(tmp_path):
    assert OutputSnapshot(tmp_path / "nope.json").load() == []


def test_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "outputs.json"
    path.write_text("{not json")
    assert OutputSnapshot(path).load() == []


def test_save_creates_parent_directory(tmp_path):
    snap = OutputSnapshot(tmp_path / "nested" / "dir" / "outputs.json")
    snap.save(["7"])
    assert snap.load() == ["7"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_outputs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nfc_jukebox.outputs'`

- [ ] **Step 3: Implement `outputs.py`**

```python
# src/nfc_jukebox/outputs.py
"""Persisted snapshot of which OwnTone outputs were selected.

Written to disk rather than held in memory so the speaker choice survives a
reboot, not merely an idle cycle.
"""
from __future__ import annotations

import json
from pathlib import Path


class OutputSnapshot:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> list[str]:
        try:
            value = json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def save(self, output_ids: list[str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(output_ids))
        tmp.replace(self._path)  # atomic on POSIX
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_outputs.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/nfc_jukebox/outputs.py tests/test_outputs.py
git commit -m "feat: add atomic output snapshot persistence"
```

---

### Task 11: Card reader

**Goal:** A reader abstraction emitting `card_present(uid)` / `card_removed()`, with a real nfcpy implementation and a fake for tests.

**Files:**
- Create: `src/nfc_jukebox/reader.py`
- Create: `tests/test_reader.py`

**Acceptance Criteria:**
- [ ] `FakeReader` lets tests drive presence events synchronously
- [ ] `Pn532Reader` emits normalised lowercase-hex UIDs
- [ ] Reader errors are logged and retried rather than killing the process

**Verify:** `pytest tests/test_reader.py -v` → 3 passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reader.py
from nfc_jukebox.reader import FakeReader


def test_fake_reader_emits_present_and_removed():
    events = []
    reader = FakeReader()
    reader.on_present = lambda uid: events.append(("present", uid))
    reader.on_removed = lambda: events.append(("removed",))

    reader.place("04:A2:B3:C4")
    reader.lift()

    assert events == [("present", "04a2b3c4"), ("removed",)]


def test_fake_reader_normalises_uid():
    seen = []
    reader = FakeReader()
    reader.on_present = seen.append
    reader.place("04A2B3C4")
    assert seen == ["04a2b3c4"]


def test_lift_without_place_is_ignored():
    events = []
    reader = FakeReader()
    reader.on_removed = lambda: events.append("removed")
    reader.lift()
    assert events == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_reader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nfc_jukebox.reader'`

- [ ] **Step 3: Implement `reader.py`**

```python
# src/nfc_jukebox/reader.py
"""Card presence detection.

Emits exactly two events and knows nothing about music. nfcpy is imported
lazily so tests (and a dev machine) need no reader hardware.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from .cards import normalise_uid

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.05
ERROR_BACKOFF = 1.0


class BaseReader:
    def __init__(self) -> None:
        self.on_present: Callable[[str], None] = lambda uid: None
        self.on_removed: Callable[[], None] = lambda: None


class FakeReader(BaseReader):
    """Test double — presence is driven manually."""

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
    """Real reader: PN532 over UART via nfcpy."""

    def __init__(self, device: str) -> None:
        super().__init__()
        self._device = device

    def run(self) -> None:
        import nfc  # imported here so the package works without hardware

        while True:
            try:
                clf = nfc.ContactlessFrontend(self._device)
            except Exception:
                log.exception("Cannot open reader %s; retrying", self._device)
                time.sleep(ERROR_BACKOFF)
                continue

            try:
                self._poll_forever(clf)
            except Exception:
                log.exception("Reader error; reopening")
                time.sleep(ERROR_BACKOFF)
            finally:
                clf.close()

    def _poll_forever(self, clf) -> None:
        while True:
            tag = clf.connect(rdwr={"on-connect": lambda tag: False})
            if tag is None:
                continue
            self.on_present(normalise_uid(tag.identifier.hex()))
            while tag.is_present:
                time.sleep(POLL_INTERVAL)
            self.on_removed()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_reader.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/nfc_jukebox/reader.py tests/test_reader.py
git commit -m "feat: add PN532 reader with fake for tests"
```

---

### Task 12: The vinyl state machine

**Goal:** Implement the controller that turns card events into OwnTone calls, with the full vinyl semantics.

**Files:**
- Create: `src/nfc_jukebox/controller.py`
- Create: `tests/test_controller.py`

**This is the only file with interesting logic.** Everything else is thin by design so
that the behaviour worth testing is concentrated here. The clock is injected so grace and
bump timing can be tested without sleeping.

**Acceptance Criteria:**
- [ ] Card placed → queue cleared, album added, playback starts at track 1
- [ ] Card removed → pause is immediate
- [ ] Same card within the bump window → resumes in place, no restart
- [ ] Same card after the bump window → restarts from track 1
- [ ] Different card mid-playback → switches immediately
- [ ] Grace expiry → stop, queue cleared, AirPlay outputs deselected
- [ ] Output snapshot restored on the next card
- [ ] AirPlay output selected by hand while idle → snapshot does not clobber it
- [ ] Saved output no longer present → falls back to local, error surfaced, playback continues
- [ ] Unknown card → ignored with an error recorded, playback unaffected
- [ ] Every scanned UID is recorded, registered or not, so learn mode can read it

**Verify:** `pytest tests/test_controller.py -v` → 11 passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_controller.py
import pytest

from nfc_jukebox.cards import Card
from nfc_jukebox.config import Config
from nfc_jukebox.controller import Controller, State


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeOwnTone:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.outputs = [
            {"id": "1", "type": "alsa", "selected": True},
            {"id": "2", "type": "airplay", "selected": False},
        ]

    def _ids(self, predicate):
        return [o["id"] for o in self.outputs if predicate(o)]

    def selected_output_ids(self):
        return self._ids(lambda o: o["selected"])

    def airplay_output_ids(self):
        return self._ids(lambda o: o["type"] == "airplay")

    def local_output_ids(self):
        return self._ids(lambda o: o["type"] != "airplay")

    def set_outputs(self, ids):
        self.calls.append(("set_outputs", tuple(ids)))
        for o in self.outputs:
            o["selected"] = o["id"] in ids

    def play_album(self, path):
        self.calls.append(("play_album", path))

    def play(self):
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def stop(self):
        self.calls.append(("stop",))

    def clear_queue(self):
        self.calls.append(("clear_queue",))


class FakeCards:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, uid):
        return self._mapping.get(uid)


class FakeSnapshot:
    def __init__(self, initial=None):
        self.value = initial or []

    def load(self):
        return list(self.value)

    def save(self, ids):
        self.value = list(ids)


@pytest.fixture
def ctx():
    clock = FakeClock()
    owntone = FakeOwnTone()
    cards = FakeCards({
        "aaaa": Card(uid="aaaa", name="Blue", path="Miles Davis/Kind of Blue"),
        "bbbb": Card(uid="bbbb", name="Rumours", path="Fleetwood Mac/Rumours"),
    })
    snapshot = FakeSnapshot()
    config = Config(bump_window_s=0.5, grace_period_s=90.0)
    controller = Controller(owntone, cards, snapshot, config, clock=clock)
    return controller, owntone, snapshot, clock


def test_card_placed_starts_album(ctx):
    controller, owntone, _, _ = ctx
    controller.on_card_present("aaaa")
    assert ("play_album", "Miles Davis/Kind of Blue") in owntone.calls
    assert controller.state is State.PLAYING


def test_card_removed_pauses_immediately(ctx):
    controller, owntone, _, _ = ctx
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    assert owntone.calls[-1] == ("pause",)
    assert controller.state is State.PAUSED


def test_same_card_within_bump_window_resumes(ctx):
    controller, owntone, _, clock = ctx
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(0.2)
    controller.on_card_present("aaaa")
    assert owntone.calls[-1] == ("play",)
    assert ("play_album", "Miles Davis/Kind of Blue") == owntone.calls[0][:2]
    assert sum(1 for c in owntone.calls if c[0] == "play_album") == 1


def test_same_card_after_bump_window_restarts(ctx):
    controller, owntone, _, clock = ctx
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(5.0)
    controller.on_card_present("aaaa")
    assert sum(1 for c in owntone.calls if c[0] == "play_album") == 2


def test_different_card_switches_immediately(ctx):
    controller, owntone, _, _ = ctx
    controller.on_card_present("aaaa")
    controller.on_card_present("bbbb")
    assert ("play_album", "Fleetwood Mac/Rumours") in owntone.calls


def test_grace_expiry_stops_and_releases_airplay(ctx):
    controller, owntone, _, clock = ctx
    owntone.outputs[1]["selected"] = True  # AirPlay selected
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(91.0)
    controller.tick()
    assert ("stop",) in owntone.calls
    assert ("clear_queue",) in owntone.calls
    assert owntone.selected_output_ids() == ["1"]  # AirPlay deselected
    assert controller.state is State.IDLE


def test_snapshot_restored_on_next_card(ctx):
    controller, owntone, snapshot, clock = ctx
    owntone.outputs[1]["selected"] = True
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(91.0)
    controller.tick()
    assert snapshot.load() == ["1", "2"]
    controller.on_card_present("aaaa")
    assert owntone.selected_output_ids() == ["1", "2"]


def test_manual_airplay_selection_while_idle_is_not_clobbered(ctx):
    controller, owntone, snapshot, _ = ctx
    snapshot.save(["1"])
    owntone.outputs[1]["selected"] = True  # user picked AirPlay by hand
    controller.on_card_present("aaaa")
    assert owntone.selected_output_ids() == ["1", "2"]


def test_missing_saved_output_falls_back_to_local(ctx):
    controller, owntone, snapshot, _ = ctx
    snapshot.save(["99"])  # HomePod no longer on the network
    controller.on_card_present("aaaa")
    assert owntone.selected_output_ids() == ["1"]
    assert controller.last_error is not None
    assert ("play_album", "Miles Davis/Kind of Blue") in owntone.calls


def test_unknown_card_is_ignored(ctx):
    controller, owntone, _, _ = ctx
    controller.on_card_present("ffff")
    assert not any(c[0] == "play_album" for c in owntone.calls)
    assert controller.last_error is not None
    assert controller.state is State.IDLE


def test_unknown_card_is_still_recorded_for_learn_mode(ctx):
    controller, _, _, _ = ctx
    controller.on_card_present("ffff")
    assert controller.last_seen_uid == "ffff"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_controller.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nfc_jukebox.controller'`

- [ ] **Step 3: Implement `controller.py`**

```python
# src/nfc_jukebox/controller.py
"""The vinyl state machine.

Put the record on, it plays from the start. Take it off, it stops. The grace
period exists because AirPlay 2's PTP handshake costs a couple of seconds, so
the session is held through short gaps and released only when the user is
genuinely done.
"""
from __future__ import annotations

import enum
import logging
import time
from typing import Callable

log = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"


class Controller:
    def __init__(self, owntone, cards, snapshot, config,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._owntone = owntone
        self._cards = cards
        self._snapshot = snapshot
        self._config = config
        self._clock = clock

        self.state = State.IDLE
        self.last_error: str | None = None
        self.now_playing: str | None = None
        # Every scanned UID, known or not — this is what learn mode reads.
        self.last_seen_uid: str | None = None
        self.last_seen_at: float = 0.0
        self._last_uid: str | None = None
        self._paused_at = 0.0

    # --- events -----------------------------------------------------------

    def on_card_present(self, uid: str) -> None:
        # Recorded before the lookup so unregistered cards are still learnable.
        self.last_seen_uid = uid
        self.last_seen_at = self._clock()

        card = self._cards.get(uid)
        if card is None:
            self.last_error = f"Unknown card {uid}"
            log.warning(self.last_error)
            return

        if self._is_bump(uid):
            self._owntone.play()
            self.state = State.PLAYING
            return

        self._restore_outputs()
        self._owntone.play_album(card.path)
        self._last_uid = uid
        self.now_playing = card.name
        self.state = State.PLAYING

    def on_card_removed(self) -> None:
        if self.state is not State.PLAYING:
            return
        self._owntone.pause()
        self._paused_at = self._clock()
        self.state = State.PAUSED

    def tick(self) -> None:
        """Called periodically; releases the outputs once grace expires."""
        if self.state is not State.PAUSED:
            return
        if self._clock() - self._paused_at > self._config.grace_period_s:
            self._release()

    # --- internals --------------------------------------------------------

    def _is_bump(self, uid: str) -> bool:
        """A dropped read, not a deliberate lift — resume rather than restart."""
        return (
            self.state is State.PAUSED
            and uid == self._last_uid
            and self._clock() - self._paused_at <= self._config.bump_window_s
        )

    def _release(self) -> None:
        selected = self._owntone.selected_output_ids()
        if selected:
            self._snapshot.save(selected)
        self._owntone.stop()
        self._owntone.clear_queue()
        # Keep local selected, drop AirPlay so the HomePods go idle and are
        # free for other senders.
        self._owntone.set_outputs(self._owntone.local_output_ids())
        self.state = State.IDLE
        self.now_playing = None
        self._last_uid = None

    def _restore_outputs(self) -> None:
        airplay = set(self._owntone.airplay_output_ids())
        current = self._owntone.selected_output_ids()

        # Guard rail: an AirPlay output selected by hand while idle is a
        # deliberate choice. Never clobber it with the snapshot. Local-only
        # selection is just our own post-release state, so it does not count.
        if any(output_id in airplay for output_id in current):
            return

        desired = self._snapshot.load()
        if not desired:
            return

        available = airplay | set(self._owntone.local_output_ids())
        usable = [output_id for output_id in desired if output_id in available]
        if not usable:
            self.last_error = "Saved outputs unavailable; falling back to local"
            log.warning(self.last_error)
            usable = self._owntone.local_output_ids()

        self._owntone.set_outputs(usable)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_controller.py -v`
Expected: 11 passed

- [ ] **Step 5: Run the whole suite**

Run: `pytest -v`
Expected: 33 passed

- [ ] **Step 6: Commit**

```bash
git add src/nfc_jukebox/controller.py tests/test_controller.py
git commit -m "feat: add vinyl state machine with grace period and output restore"
```

---

### Task 13: Entrypoint and systemd service

> **Ordering correction:** implement **Task 14 (web admin) BEFORE this task.**
> `__main__.py` imports `create_app` from `web.py`, so building this first leaves an
> entrypoint that cannot run. The numbering here reflects the original draft order; the
> dependency graph is the authority.

**Goal:** The service runs on boot, wires reader to controller, and ticks the grace timer.

**Files:**
- Create: `src/nfc_jukebox/__main__.py`
- Create: `deploy/nfc-jukebox.service`
- Create: `deploy/config.yaml.example`

**Acceptance Criteria:**
- [ ] `systemctl is-active nfc-jukebox` returns `active` after boot
- [ ] The service waits for OwnTone before starting
- [ ] Reader events appear in `journalctl -u nfc-jukebox`
- [ ] The service restarts automatically if it crashes

**Verify:** `ssh jukebox.local 'systemctl is-active nfc-jukebox && journalctl -u nfc-jukebox -n 20 --no-pager'` → `active`, log shows "Reader open"

**Steps:**

- [ ] **Step 1: Write the entrypoint**

```python
# src/nfc_jukebox/__main__.py
"""Wire reader, controller and web admin together."""
from __future__ import annotations

import logging
import threading
import time

from .cards import CardStore
from .config import Config
from .controller import Controller
from .outputs import OutputSnapshot
from .owntone import OwnTone
from .reader import Pn532Reader
from .web import create_app

TICK_INTERVAL = 1.0


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("nfc_jukebox")

    config = Config.load()
    store = CardStore(config.cards_file)
    controller = Controller(
        owntone=OwnTone(config.owntone_url),
        cards=store,
        snapshot=OutputSnapshot(config.outputs_file),
        config=config,
    )

    reader = Pn532Reader(config.reader_device)
    reader.on_present = controller.on_card_present
    reader.on_removed = controller.on_card_removed

    app = create_app(config, controller, store)
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=config.web_port,
                               threaded=True, use_reloader=False),
        daemon=True,
    ).start()

    def tick_forever() -> None:
        while True:
            try:
                controller.tick()
            except Exception:
                log.exception("Tick failed")
            time.sleep(TICK_INTERVAL)

    threading.Thread(target=tick_forever, daemon=True).start()

    log.info("Reader open on %s", config.reader_device)
    reader.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the example config**

```yaml
# deploy/config.yaml.example -> /etc/nfc-jukebox/config.yaml
owntone_url: http://127.0.0.1:3689
library_root: /srv/music
cards_file: /etc/nfc-jukebox/cards.yaml
outputs_file: /var/lib/nfc-jukebox/outputs.json
reader_device: "tty:AMA0:pn532"
# Set from the Spike 2 measurement (Task 6), not guessed.
bump_window_s: 0.5
grace_period_s: 90.0
web_port: 8080
```

- [ ] **Step 3: Write the systemd unit**

```ini
# deploy/nfc-jukebox.service -> /etc/systemd/system/nfc-jukebox.service
[Unit]
Description=NFC Vinyl Jukebox
# Start after OwnTone, but do NOT share its fate: Requires= would deactivate
# this service if owntone.service ever failed or stopped, and because that is a
# clean stop, Restart=always would not bring it back. One 2am OwnTone segfault
# would kill the reader loop and the admin page until someone SSHed in. The
# controller already contains and surfaces an unreachable OwnTone, so Wants=
# keeps the ordering and keeps that resilience.
After=network-online.target owntone.service
Wants=network-online.target owntone.service
# Do not start before the music is actually mounted. Harmless for local
# storage; essential if library_root becomes a NAS mount (Task 16).
RequiresMountsFor=/srv/music

[Service]
Type=simple
User=pi
# dialout is for the UART the PN532 sits on. As Group= it would also become the
# primary group of every file the service writes; as a supplementary group it
# grants the serial access without touching file ownership.
SupplementaryGroups=dialout
# systemd creates these before ExecStart and hands them to User=, so the
# service can actually write cards.yaml. Without it /etc/nfc-jukebox stays
# root-owned 0755 and every card registration dies with PermissionError.
ConfigurationDirectory=nfc-jukebox
ConfigurationDirectoryMode=0755
StateDirectory=nfc-jukebox
ExecStart=/opt/nfc-jukebox/venv/bin/python -m nfc_jukebox
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Install on the Pi**

```bash
sudo mkdir -p /opt/nfc-jukebox /etc/nfc-jukebox /var/lib/nfc-jukebox
# The service runs as pi and writes cards.yaml into /etc/nfc-jukebox, so that
# directory must be pi-writable too -- otherwise every card registration fails
# with PermissionError. The unit's ConfigurationDirectory=/StateDirectory= also
# assert this on each start; these chowns make a first run before the unit is
# installed (and a hand-copied config) behave the same way.
sudo chown pi:pi /etc/nfc-jukebox /var/lib/nfc-jukebox
python3 -m venv /opt/nfc-jukebox/venv
/opt/nfc-jukebox/venv/bin/pip install -e ~/owntone-nfc
sudo cp ~/owntone-nfc/deploy/config.yaml.example /etc/nfc-jukebox/config.yaml
sudo cp ~/owntone-nfc/deploy/nfc-jukebox.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nfc-jukebox
```

- [ ] **Step 5: Verify**

```bash
systemctl is-active nfc-jukebox
journalctl -u nfc-jukebox -n 20 --no-pager
```

Expected: `active`, and a log line reading `Reader open on tty:AMA0:pn532`.

- [ ] **Step 6: Commit**

```bash
git add src/nfc_jukebox/__main__.py deploy/nfc-jukebox.service deploy/config.yaml.example
git commit -m "feat: add service entrypoint and systemd unit"
```

---

### Task 14: Web admin — card registration

**Goal:** A minimal page for registering cards and seeing status. Nothing player-related.

**Files:**
- Create: `src/nfc_jukebox/web.py`
- Create: `src/nfc_jukebox/templates/index.html`
- Create: `tests/test_web.py`

**Scope discipline.** OwnTone's own UI already ships transport, volume, queue, library
browsing and speaker selection. This page builds none of that — it does only what OwnTone
cannot: map a card UID to an album.

**Acceptance Criteria:**
- [ ] `GET /api/status` returns state, now playing, last error, and last scanned UID
- [ ] `GET /api/albums` lists album directories relative to the library root
- [ ] `POST /api/cards` saves a mapping and it survives a restart
- [ ] `DELETE /api/cards/<uid>` removes a mapping
- [ ] Tapping an unregistered card makes its UID appear in learn mode within 2 seconds

**Verify:** `pytest tests/test_web.py -v` → 5 passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web.py
import pytest

from nfc_jukebox.cards import Card, CardStore
from nfc_jukebox.config import Config
from nfc_jukebox.controller import Controller, State
from nfc_jukebox.web import create_app
from tests.test_controller import FakeClock, FakeOwnTone, FakeSnapshot


@pytest.fixture
def app_ctx(tmp_path):
    (tmp_path / "Miles Davis" / "Kind of Blue").mkdir(parents=True)
    (tmp_path / "Fleetwood Mac" / "Rumours").mkdir(parents=True)
    config = Config(library_root=tmp_path, cards_file=tmp_path / "cards.yaml")
    store = CardStore(config.cards_file)
    controller = Controller(FakeOwnTone(), store, FakeSnapshot(), config,
                            clock=FakeClock())
    app = create_app(config, controller, store)
    app.config.update(TESTING=True)
    return app.test_client(), controller, store


def test_status_reports_state(app_ctx):
    client, controller, _ = app_ctx
    controller.state = State.IDLE
    body = client.get("/api/status").get_json()
    assert body["state"] == "idle"
    assert "last_seen_uid" in body


def test_albums_are_listed_relative_to_library_root(app_ctx):
    client, _, _ = app_ctx
    albums = client.get("/api/albums").get_json()["albums"]
    assert "Miles Davis/Kind of Blue" in albums
    assert "Fleetwood Mac/Rumours" in albums


def test_post_card_saves_mapping(app_ctx):
    client, _, store = app_ctx
    response = client.post("/api/cards", json={
        "uid": "04:A2:B3:C4", "name": "Blue", "path": "Miles Davis/Kind of Blue"})
    assert response.status_code == 201
    assert store.get("04a2b3c4").path == "Miles Davis/Kind of Blue"


def test_delete_card_removes_mapping(app_ctx):
    client, _, store = app_ctx
    store.save({"04a2b3c4": Card(uid="04a2b3c4", name="Blue", path="A/B")})
    assert client.delete("/api/cards/04a2b3c4").status_code == 204
    assert store.get("04a2b3c4") is None


def test_learn_mode_sees_unregistered_card(app_ctx):
    client, controller, _ = app_ctx
    controller.on_card_present("deadbeef")
    assert client.get("/api/status").get_json()["last_seen_uid"] == "deadbeef"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_web.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nfc_jukebox.web'`

- [ ] **Step 3: Implement `web.py`**

```python
# src/nfc_jukebox/web.py
"""Card registration admin.

Deliberately tiny: OwnTone's UI on :3689 owns everything player-related.
"""
from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from .cards import Card, normalise_uid


def create_app(config, controller, store) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("index.html", owntone_url=config.owntone_url)

    @app.get("/api/status")
    def status():
        return jsonify(
            state=controller.state.value,
            now_playing=controller.now_playing,
            last_error=controller.last_error,
            last_seen_uid=controller.last_seen_uid,
        )

    @app.get("/api/albums")
    def albums():
        root = config.library_root
        found = sorted(
            str(path.relative_to(root))
            for path in root.glob("*/*")
            if path.is_dir()
        )
        return jsonify(albums=found)

    @app.get("/api/cards")
    def list_cards():
        return jsonify(cards=[
            {"uid": c.uid, "name": c.name, "path": c.path}
            for c in store.load().values()
        ])

    @app.post("/api/cards")
    def add_card():
        payload = request.get_json(force=True)
        uid = normalise_uid(payload["uid"])
        cards = store.load()
        cards[uid] = Card(uid=uid, name=payload["name"], path=payload["path"])
        store.save(cards)
        return jsonify(uid=uid), 201

    @app.delete("/api/cards/<uid>")
    def delete_card(uid: str):
        cards = store.load()
        cards.pop(normalise_uid(uid), None)
        store.save(cards)
        return "", 204

    return app
```

- [ ] **Step 4: Write the template**

```html
<!-- src/nfc_jukebox/templates/index.html -->
<!doctype html>
<title>Jukebox Admin</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 48rem; margin: 2rem auto; padding: 0 1rem; }
  table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
  td, th { text-align: left; padding: .4rem; border-bottom: 1px solid #ddd; }
  #uid { font-family: ui-monospace, monospace; font-size: 1.3rem; }
  .muted { color: #666; }
</style>

<h1>Jukebox</h1>
<p id="status" class="muted">…</p>
<p class="muted">Player controls, volume and speakers live in
  <a href="{{ owntone_url }}">OwnTone</a>.</p>

<h2>Register a card</h2>
<p>Tap a card on the reader. Its ID appears here.</p>
<p id="uid">—</p>
<label>Name <input id="name"></label>
<label>Album <select id="album"></select></label>
<button onclick="save()">Save</button>

<h2>Cards</h2>
<table><thead><tr><th>Name</th><th>Album</th><th>UID</th><th></th></tr></thead>
<tbody id="cards"></tbody></table>

<script>
async function refresh() {
  const s = await (await fetch('/api/status')).json();
  document.getElementById('status').textContent =
    `${s.state}${s.now_playing ? ' — ' + s.now_playing : ''}` +
    (s.last_error ? ` · ${s.last_error}` : '');
  if (s.last_seen_uid) document.getElementById('uid').textContent = s.last_seen_uid;

  const c = await (await fetch('/api/cards')).json();
  document.getElementById('cards').innerHTML = c.cards.map(card =>
    `<tr><td>${card.name}</td><td>${card.path}</td><td>${card.uid}</td>
     <td><button onclick="del('${card.uid}')">Delete</button></td></tr>`).join('');
}
async function loadAlbums() {
  const a = await (await fetch('/api/albums')).json();
  document.getElementById('album').innerHTML =
    a.albums.map(x => `<option>${x}</option>`).join('');
}
async function save() {
  const uid = document.getElementById('uid').textContent;
  if (uid === '—') return alert('Tap a card first.');
  await fetch('/api/cards', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({uid,
      name: document.getElementById('name').value,
      path: document.getElementById('album').value})});
  document.getElementById('name').value = '';
  refresh();
}
async function del(uid) {
  await fetch('/api/cards/' + uid, {method: 'DELETE'});
  refresh();
}
loadAlbums(); refresh(); setInterval(refresh, 1000);
</script>
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: 5 passed

- [ ] **Step 6: Run the whole suite and commit**

Run: `pytest -v`
Expected: 38 passed

```bash
git add src/nfc_jukebox/web.py src/nfc_jukebox/templates/index.html tests/test_web.py
git commit -m "feat: add card registration web admin"
```

---

## Phase 4 — Deploy, migrate, operate (Tasks 15–18)

### Task 15: Provisioning script and SD card migration

**Goal:** One script takes a fresh Bookworm Pi to a working jukebox, and moving to a larger card never requires re-registering cards.

**Files:**
- Create: `deploy/provision.sh`
- Create: `deploy/backup.sh`
- Modify: `docs/runbook.md`

**Why this exists.** The library is ~56GB and the current card holds a subset. A larger
card is coming, so redeployment is a planned event, not a disaster-recovery scenario. The
portable state is tiny: `cards.yaml` plus `config.yaml`. Mappings store library-relative
paths, so they stay valid as long as the `<Artist>/<Album>` layout is preserved.

**Acceptance Criteria:**
- [ ] `provision.sh` runs on a fresh Bookworm Lite install and ends with both services active
- [ ] `backup.sh` produces a single archive containing `cards.yaml` and `config.yaml`
- [ ] Restoring that archive onto a fresh build makes every existing card work again with no re-registration
- [ ] The script is idempotent — running it twice is harmless

**Verify:** `ssh jukebox.local 'sudo bash /home/pi/owntone-nfc/deploy/provision.sh && systemctl is-active owntone nfc-jukebox'` → `active` twice

**Steps:**

- [ ] **Step 1: Write `deploy/provision.sh`**

```bash
#!/usr/bin/env bash
# Take a fresh Raspberry Pi OS Bookworm Lite install to a working jukebox.
# Idempotent: safe to re-run.
set -euo pipefail

REPO="${REPO:-/home/pi/owntone-nfc}"
MUSIC="${MUSIC:-/srv/music}"

echo "==> Base packages"
apt update
apt install -y samba python3-venv python3-pip wget

echo "==> UART for the PN532"
grep -q '^dtoverlay=disable-bt' /boot/firmware/config.txt \
  || echo 'dtoverlay=disable-bt' >> /boot/firmware/config.txt
raspi-config nonint do_serial_hw 0
raspi-config nonint do_serial_cons 1
systemctl disable hciuart || true
usermod -aG dialout pi

echo "==> Music library root"
mkdir -p "$MUSIC"
chown -R pi:pi "$MUSIC"

echo "==> OwnTone"
if ! command -v owntone >/dev/null; then
  wget -q -O - https://raw.githubusercontent.com/owntone/owntone-apt/refs/heads/master/repo/rpi/owntone.gpg \
    | gpg --dearmor --output /usr/share/keyrings/owntone-archive-keyring.gpg
  wget -q -O /etc/apt/sources.list.d/owntone.list \
    https://raw.githubusercontent.com/owntone/owntone-apt/refs/heads/master/repo/rpi/owntone-bookworm.list
  apt update
  apt install -y owntone
fi
cp "$REPO/deploy/owntone.conf.example" /etc/owntone.conf
systemctl enable --now owntone

echo "==> Jukebox service"
mkdir -p /opt/nfc-jukebox /etc/nfc-jukebox /var/lib/nfc-jukebox
# /etc/nfc-jukebox holds cards.yaml, which the service writes as pi. Leaving it
# root-owned makes every card registration fail with PermissionError, i.e. the
# box can never be taught a single album.
chown pi:pi /etc/nfc-jukebox /var/lib/nfc-jukebox
[ -d /opt/nfc-jukebox/venv ] || python3 -m venv /opt/nfc-jukebox/venv
/opt/nfc-jukebox/venv/bin/pip install -q -e "$REPO"
[ -f /etc/nfc-jukebox/config.yaml ] \
  || cp "$REPO/deploy/config.yaml.example" /etc/nfc-jukebox/config.yaml
cp "$REPO/deploy/nfc-jukebox.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nfc-jukebox

echo "==> Done. Reboot if UART settings changed."
```

- [ ] **Step 2: Write `deploy/backup.sh`**

```bash
#!/usr/bin/env bash
# Archive the portable state: card mappings and config.
# Music is NOT included — it is large and lives on the master copy.
set -euo pipefail

OUT="${1:-jukebox-state-$(date +%Y%m%d).tar.gz}"
tar czf "$OUT" \
  -C / \
  etc/nfc-jukebox/cards.yaml \
  etc/nfc-jukebox/config.yaml
echo "Wrote $OUT"
echo "Restore with: sudo tar xzf $OUT -C /"
```

- [ ] **Step 3: Make both executable and test provisioning is idempotent**

```bash
chmod +x deploy/provision.sh deploy/backup.sh
sudo bash deploy/provision.sh
sudo bash deploy/provision.sh   # second run must also succeed
systemctl is-active owntone nfc-jukebox
```

Expected: both runs succeed, `active` printed twice.

- [ ] **Step 4: Document the migration procedure**

Append to `docs/runbook.md`:

```markdown
## Migrating to a larger SD card
1. On the old card:  sudo bash deploy/backup.sh
   Copy the archive off the Pi.
2. Burn a fresh Bookworm Lite 64-bit image (Task 0 settings).
3. git clone the repo to /home/pi/owntone-nfc
4. sudo bash deploy/provision.sh
5. sudo tar xzf jukebox-state-YYYYMMDD.tar.gz -C /
6. Copy music to /srv/music, preserving <Artist>/<Album>/ layout.
7. sudo systemctl restart owntone nfc-jukebox

Cards do NOT need re-registering: mappings store library-relative paths.
The only thing that breaks mappings is changing the <Artist>/<Album> layout.
```

- [ ] **Step 5: Commit**

```bash
git add deploy/provision.sh deploy/backup.sh docs/runbook.md
git commit -m "feat: add provisioning and state backup scripts"
```

---

### Task 16: Optional — mount the NAS as the library root

**Goal:** Allow `/srv/music` to be a NAS mount instead of local storage, with the two known failure modes mitigated.

**Files:**
- Modify: `/etc/fstab` (on the Pi)
- Create: `deploy/nas-mount.md`
- Modify: `docs/runbook.md`

**When to do this task.** Only if you want the full ~56GB library before a larger SD card
arrives. Skip it otherwise — local storage is simpler and strictly more reliable.

**Acceptance Criteria:**
- [ ] `/srv/music` mounts automatically and survives a reboot
- [ ] `owntone.service` does not start before the mount is present
- [ ] A cron job triggers a rescan, and a newly added album appears within its interval
- [ ] Existing card mappings work unchanged after the switch

**Verify:** `ssh jukebox.local 'sudo reboot'; sleep 60; ssh jukebox.local 'mountpoint -q /srv/music && systemctl is-active owntone && curl -s localhost:3689/api/library | grep -o "\"songs\":[0-9]*"'` → mount present, `active`, non-zero songs

**Steps:**

- [ ] **Step 1: Add the mount with automount semantics**

```bash
sudo apt install -y cifs-utils
sudo mkdir -p /etc/samba/creds
printf 'username=NASUSER\npassword=NASPASS\n' | sudo tee /etc/samba/creds/nas >/dev/null
sudo chmod 600 /etc/samba/creds/nas

echo '//nas.local/music /srv/music cifs credentials=/etc/samba/creds/nas,uid=pi,gid=pi,iocharset=utf8,ro,x-systemd.automount,x-systemd.idle-timeout=600,nofail 0 0' \
  | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount -a
```

`x-systemd.automount` is the important flag: the mount is established on first access
rather than at boot, so a slow-waking NAS cannot leave OwnTone staring at an empty
directory.

- [ ] **Step 2: Order OwnTone behind the mount**

```bash
sudo mkdir -p /etc/systemd/system/owntone.service.d
sudo tee /etc/systemd/system/owntone.service.d/mount.conf >/dev/null <<'EOF'
[Unit]
RequiresMountsFor=/srv/music
EOF
sudo systemctl daemon-reload
```

This is the mitigation for OwnTone issue #690 — if the share is not mounted when OwnTone
starts, the music stays unavailable even after a later remount, and only a full rescan
recovers it.

- [ ] **Step 3: Add the periodic rescan**

```bash
sudo tee /etc/cron.hourly/owntone-rescan >/dev/null <<'EOF'
#!/bin/sh
# Network mounts send no inotify events, so OwnTone cannot see changes in
# real time. This trigger file is OwnTone's documented workaround.
touch /srv/music/.init-rescan 2>/dev/null || true
EOF
sudo chmod +x /etc/cron.hourly/owntone-rescan
```

Note this needs the share mounted read-write. If it is mounted `ro`, drop the `ro` option
in Step 1 or trigger the rescan from the NAS side instead.

- [ ] **Step 4: Reboot and verify the full path**

```bash
sudo reboot
# wait, then:
mountpoint -q /srv/music && echo mounted
systemctl is-active owntone
curl -s localhost:3689/api/library | python3 -m json.tool | grep songs
```

Expected: `mounted`, `active`, non-zero song count.

- [ ] **Step 5: Confirm cards still work**

Tap an already-registered card. It must play. Mappings are library-root-relative, so
switching the storage behind `/srv/music` changes nothing — provided the
`<Artist>/<Album>` layout matches.

- [ ] **Step 6: Commit the documentation**

```bash
git add deploy/nas-mount.md docs/runbook.md
git commit -m "docs: add optional NAS mount with boot-order and rescan mitigations"
```

---

### Task 17: Register the cards

**Goal:** Every physical card you own is mapped to an album.

**Files:**
- Creates on the Pi: `/etc/nfc-jukebox/cards.yaml`

**Your existing cards work unchanged.** Only the UID is ever read, and every NFC card has
one. Nothing was written to them by the old Phoniebox build that matters here.

**Acceptance Criteria:**
- [ ] Each card tapped shows its UID in learn mode within 2 seconds
- [ ] Each card is saved with a name and an album path
- [ ] Tapping each registered card starts the right album from track 1
- [ ] `cards.yaml` contains only library-relative paths, no absolute paths

**Verify:** `ssh jukebox.local 'cat /etc/nfc-jukebox/cards.yaml'` → one entry per card, paths like `Miles Davis/Kind of Blue`

**Steps:**

- [ ] **Step 1: Open the admin page**

Browse to `http://jukebox.local:8080`.

- [ ] **Step 2: Register each card**

For each card: place it on the reader, confirm its UID appears, type a name, choose the
album from the dropdown, click Save. Repeat.

- [ ] **Step 3: Test each one**

Place each card and confirm the right album starts from track 1. Remove it and confirm
playback pauses.

- [ ] **Step 4: Back up immediately**

```bash
sudo bash deploy/backup.sh
```

Copy the archive off the Pi. This is the file that saves you from ever doing Step 2 again.

- [ ] **Step 5: Verify no absolute paths crept in**

```bash
ssh jukebox.local 'grep -c "^\s*path: /" /etc/nfc-jukebox/cards.yaml || true'
```

Expected: `0`. An absolute path here would break on the next rebuild.

---

### Task 18: End-to-end acceptance

**Goal:** Confirm the whole system behaves like a record player, and that the speaker choice actually sticks.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `docs/runbook.md`

**Acceptance Criteria:**
- [ ] Card placed → correct album plays from track 1, on the selected output
- [ ] Card removed → playback pauses within 1 second
- [ ] Card replaced quickly (a bump) → resumes without restarting
- [ ] Card replaced after the bump window → restarts from track 1
- [ ] Swapping cards mid-playback → switches immediately
- [ ] After the grace period → playback stops and the HomePods go idle and are usable by another AirPlay sender
- [ ] **Output selection survives a full release cycle** — pick HomePods in OwnTone, let it idle past grace, tap a card, and it returns to HomePods
- [ ] **Output selection survives a reboot** — same check with `sudo reboot` in between
- [ ] Unplugging/powering off a HomePod mid-idle → next card plays locally instead of silently failing

**Verify:** `ssh jukebox.local 'systemctl is-active nfc-jukebox owntone && curl -s localhost:8080/api/status'` → both `active`, status reports a sane state

**Steps:**

- [ ] **Step 1: Vinyl behaviour**

Run through each behaviour in the criteria list with a real card and a real album. Note
the observed timings.

- [ ] **Step 2: Output persistence across a release cycle**

```bash
# Select the HomePods in OwnTone's UI, then:
curl -s localhost:3689/api/outputs | python3 -m json.tool | grep -A2 airplay
```

Tap a card, play, remove it, wait out the grace period, then confirm the HomePods
released. Tap the card again and confirm playback returns **to the HomePods**, not local.

- [ ] **Step 3: Output persistence across a reboot**

```bash
sudo reboot
# wait for boot, then tap a card
cat /var/lib/nfc-jukebox/outputs.json
```

Expected: the file contains the AirPlay output id, and playback resumes on the HomePods.
This is the check that "output actually sticks" — the reason the snapshot is written to
disk rather than held in memory.

- [ ] **Step 4: Degraded-HomePod behaviour**

Power off a HomePod, then tap a card. Expected: music plays **locally**, and
`/api/status` reports a `last_error`. The box must never be silent.

- [ ] **Step 5: Record results and commit**

Append observed timings and any surprises to `docs/runbook.md`.

```bash
git add docs/runbook.md
git commit -m "docs: record end-to-end acceptance results"
```

```json:metadata
{"userGate": true, "tags": ["user-gate", "acceptance"], "requireEvidenceTokens": [["before-reboot", "release-cycle"], ["after-reboot", "persisted"]], "verifyCommand": "ssh jukebox.local 'cat /var/lib/nfc-jukebox/outputs.json && systemctl is-active nfc-jukebox owntone'", "acceptanceCriteria": ["album plays from track 1 on card placement", "pause within 1s of removal", "bump resumes without restart", "post-bump replace restarts", "card swap switches immediately", "grace expiry releases HomePods", "output survives release cycle", "output survives reboot", "unreachable HomePod falls back to local"], "modelTier": "standard"}
```

---

## Open decisions deferred to execution

**None.** Every choice in this plan was settled during brainstorming and is recorded in
the header. The three spikes resolve *facts* (does AirPlay reach the HomePods, what is the
expression syntax, what is the real dropped-read rate), not decisions — each has a
documented fallback path, so none of them can block on a question to the user.
