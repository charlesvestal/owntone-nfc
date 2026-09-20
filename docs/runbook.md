# Jukebox Runbook

Operational notes for the NFC vinyl jukebox. Design rationale lives in
`docs/superpowers/specs/2026-09-18-nfc-vinyl-jukebox-design.md`; this file is
what you want when something is broken or you are rebuilding.

## At a glance

| | |
|---|---|
| Host | `jukebox.local` (Wi-Fi 2.4 GHz `tincanphoney24`, DHCP) |
| Admin page | http://jukebox.local:8080 — card registration only |
| OwnTone | http://jukebox.local:3689 — player, volume, speakers, library |
| Music | `/srv/music/<Artist>/<Album>/` — Samba share `smb://jukebox.local` (guest) |
| Card registry | `/etc/nfc-jukebox/cards.yaml` |
| Speaker snapshot | `/var/lib/nfc-jukebox/outputs.json` |
| Services | `nfc-jukebox`, `owntone` |
| Logs | `journalctl -u nfc-jukebox -f` / `journalctl -u owntone -f` |

## Hardware

**Raspberry Pi 4**, Raspberry Pi OS Lite 64-bit.

Bookworm was originally chosen because Phoniebox required it. **Phoniebox is
not used**, so that constraint does not apply and a newer release is fine -
`provision.sh` reads the codename from `/etc/os-release` and picks the matching
OwnTone repository, which publishes both bookworm and trixie.

**Waveshare PN532 NFC HAT**, on the GPIO header, **UART mode**:

- DIP switches 1-6 (SCK, MISO, MOSI, NSS, SCL, SDA) **OFF**; 7 and 8 (RX, TX) **ON**
- Jumpers **I0 = L and I1 = L** (the board's own table: UART is L/L)
- **RSTPDN jumpered to D20** — not optional, see "reader stops responding"

UART rather than I2C because `nfcpy` has no I2C support, and continuous
presence detection is the entire product. `dtoverlay=disable-bt` puts the real
PL011 on GPIO14/15; the mini-UART's baud rate drifts with the VPU clock and
gives a reader that works only intermittently. Onboard Bluetooth is sacrificed
and not missed.

## Wi-Fi: power save must stay OFF

**This is the single most expensive thing to rediscover.**

With `802-11-wireless.powersave` on (the default), this Pi associates normally,
completes the handshake, transmits at 260 Mbit/s — and receives at **6 Mbit/s**,
the floor. DHCP times out, nothing routes, and the box vanishes from the
network while believing it is connected. It looks exactly like a dead router, a
failing repeater, or a shielded antenna, and it is none of those.

```bash
nmcli connection modify <wifi-connection> 802-11-wireless.powersave 2
```

With it off: rx 433 Mbit/s, DHCP instant. Transmit suffers too: 263 Mbit/s
down to 24 with power save on. `provision.sh` applies this to every Wi-Fi
connection it finds, and on Trixie the setting survives a reboot despite
netplan generating the NetworkManager config. The setting lives only in
NetworkManager on the SD card, so a rebuild without provision.sh will hit this
again.

Diagnosing this took hours, mostly because the fix was applied early and then
buried under two other changes (a BSSID pin and a static IP) whose failure
masked it. **Change one thing at a time.**

## AirPlay

**`user_agent = "AirPlay/999.0.0"` in `/etc/owntone.conf` is required.** Apple
OS 27 (HomePod / tvOS / macOS, AirTunes `srcvers` 980.x) gates `GET /info` on
the User-Agent and rejects OwnTone's default with 403, before any
authentication. Without it, every Apple device stops working as it updates to
27. Upstream: owntone/owntone-server#2042.

**Apple Home → Home Settings → Allow Speakers & TV Access → "Anyone On the Same
Network".** Apple's default cannot be satisfied by a Linux sender; the symptom
is a speaker that deselects itself the moment playback starts.

**What works:** both HomePods selected as two separate outputs. That is real
stereo — verified with the hard-panned test file at `ZZ Test/Stereo Test/` in
the library. Keep that file; impressions about stereo were wrong twice.

**What does not work:** targeting the Apple TV ("Living Room"). It pairs,
accepts the session, shows the track and honours seeks, but never advances its
playhead or produces audio. Best explanation is that as AirPlay group leader it
expects group semantics OwnTone does not implement. Do not re-attempt without
new information.

**macOS is not usable as a test receiver** — macOS 27 returns 403 to an
unauthenticated `/info` probe even over loopback.

## Wi-Fi: use the 2.4 GHz SSID, not 5 GHz

**This board's 5 GHz path fails in the jukebox's normal position, and 2.4 GHz
works. Configure `tincanphoney24`.** Everything below is the evidence, because
two earlier outages were blamed on the wrong thing.

Measured 2026-09-19, same board, same spot, minutes apart:

| Band | SSID | Channel | Signal | DHCP | Rate |
|---|---|---|---|---|---|
| 5 GHz | `tincanphoney` | 36 (non-DFS) | -69 dBm | **never completes** | - |
| 2.4 GHz | `tincanphoney24` | 11 | **-58 dBm** | instant | 130 Mbit/s |

Eleven dB, and the difference between working and not. 2.4 GHz penetrates; the
box sits where it sits.

The 5 GHz failure is always the same and is thoroughly misleading: it
associates, completes the 4-way handshake, logs *"Connected to wireless
network"* - and then DHCP opens a transaction and gets nothing. The box is
alive and believes it is fine, while being completely unreachable.

### What this is NOT - three theories that were wrong

Each cost real time. Do not re-run them.

**Not DFS.** The earlier outage was blamed on the router defaulting 5 GHz to
channels 100/104/108/112, which are radar-protected, where a client may not
probe actively. Plausible, and wrong: on 2026-09-19 the identical failure
reproduced on **channel 36, a non-DFS channel**, with the router's 5 GHz band
correctly pinned. DFS is not necessary to produce this.

What actually correlates across every observation is the **5 GHz signal
level**, with a cliff somewhere around -67 dBm: -66 dBm worked, -69 dBm failed,
twice. The earlier "changing to channel 36 fixed it" result was almost
certainly a few dB gained from moving the box, not the channel.

**Not a 2.4 GHz radio fault.** This runbook previously stated the board sees
zero 2.4 GHz networks, ever, and fails even a directed probe. That is no longer
true - after the 2026-09-15 rebuild onto Trixie (kernel 6.18.50) a scan shows
five 2.4 GHz networks, `tincanphoney24` among them at the strongest signal of
any AP the board can see. If the fault was ever real, the newer firmware fixed
it. Note also that the two bands are *separate SSIDs* here, so the old
"forcing the 2.4 GHz band didn't work" test proved nothing: it was forcing a
profile for `tincanphoney`, which does not exist on 2.4.

**Not the enclosure.** The box ran for months in that same enclosure in that
same spot, and 2.4 GHz works fine inside it today. The enclosure does not
change, so it cannot explain a box that stops working.

### A USB Wi-Fi adapter is not currently needed

Earlier advice here was to buy one. With 2.4 GHz working at -58 dBm that is
unnecessary. If the box is ever moved somewhere 2.4 also fails, pick one whose
driver is in the mainline kernel so nothing has to be rebuilt when the kernel
updates: the **Alfa AWUS036ACM** (MediaTek MT7612U, `mt76x2u`, dual-band,
detachable antenna) or the cheaper **Panda PAU0B** (RT5572, `rt2800usb`).
Avoid RTL8811AU/8821AU/8812AU sticks (TP-Link Archer T2U/T3U and most cheap AC
dongles) - out-of-tree DKMS drivers are exactly the wrong property for an
appliance that must come back silently after a reboot.

## Wi-Fi: the current configuration

As left on 2026-09-19:

| Profile | Band | Autoconnect | Priority | Powersave |
|---|---|---|---|---|
| `tincanphoney24` | 2.4 GHz | yes | 10 | off |
| `tincanphoney` | 5 GHz | **no** | 0 | off |

The 5 GHz profile is deliberately `autoconnect no`. Left enabled it grabs
`wlan0` at boot, spends 45 seconds failing DHCP, and can wedge the box off the
network entirely. It is kept only so it is there if the box is ever moved
somewhere 5 GHz is strong.

Verified by reboot: `wlan0` came back unattended in about 30 seconds on
`192.168.2.188`, -59 dBm, 130 Mbit/s, with `nfc-jukebox`, `owntone` and
`avahi-daemon` all active.

## The outage of 2026-09-19

**Cause: the box had no Wi-Fi configuration at all.** `nmcli con show` listed
only the wired connection, and `/etc/NetworkManager/system-connections/` was
empty. The card was rebuilt on 2026-09-15 and re-provisioned on 2026-09-18;
Raspberry Pi Imager's Wi-Fi setup did not take, and nothing noticed.

`provision.sh` made it silent. Its power-save loop only *modifies* Wi-Fi
connections that already exist, so with no profile it matched nothing, `|| true`
swallowed it, and provisioning reported success on a box that could not reach
the network. It now fails loudly in that case instead.

Worth knowing for next time: the symptom was `jukebox.local` not resolving,
which looks like mDNS. It was not. The way to tell in one step is to ping-sweep
the subnet and grep the ARP table for this Pi 4's OUI, `e4:5f:01` - absent
means it never got a DHCP lease, so the problem is the radio, not the name.

The router lists several stale `jukebox*` entries, all offline: `.188`
(`E4:5F:01:91:CE:ED`, wlan0) and `.189` (`E4:5F:01:91:CE:EC`, eth0) are this Pi
4; `.224` and `.225` are `B8:27:EB:...`, earlier boards. The `-1/-2/-3` suffixes
are the Speedport disambiguating different MACs that all announced the hostname
`jukebox`. Not a name conflict on the Pi, and not a fault.

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| Box invisible on the network after a reboot | Wi-Fi power save | See above. Check `iw wlan0 link` — rx bitrate at 6 Mbit/s is the tell. |
| Box invisible and `jukebox.local` does not resolve | Usually no Wi-Fi profile, or `wlan0` on 5 GHz | First: `nmcli con show` — if there is no Wi-Fi connection, that is the whole answer. Confirm the box is absent rather than merely unnamed by ping-sweeping the subnet and grepping the ARP table for `e4:5f:01`; no entry means it never got a DHCP lease, so it is not mDNS. Then check it is on `tincanphoney24`, not `tincanphoney`. |
| Reader stops responding; log shows `ETIMEDOUT` repeatedly | PN532 wedged out of frame sync, usually by a service restart killing it mid-transaction | Automatic: the reader pulses RSTPDN on GPIO20 after 2 failures and recovers in ~4s. If it does not, check the RSTPDN↔D20 jumper. Manual: `pinctrl set 20 op dl; sleep 1; pinctrl set 20 op dh` |
| A card plays nothing; status flaps play/pause | Card technology with unstable presence — 4-byte Mifare-style cards report present for ~8ms at a time | Already handled by `presence_debounce_s` (0.5s). If a new card still flaps, raise it. |
| An album plays shuffled or not from track 1 | Shuffle enabled in OwnTone's UI | The controller forces shuffle/repeat off per card. If it persists, check the log for an error on that call. |
| Speaker drops mid-album, status shows an error | AirPlay receiver refused pairing; OwnTone fell back to local | Check Apple Home access setting. The error names which output was lost. |
| Admin page shows no cards | Page-level failure, not data loss | Check `curl localhost:8080/api/cards` first — the registry is almost certainly intact. |

## Music on the NAS

The library is an SMB mount from the UGREEN NAS, mounted read-only at
`/srv/music` so the jukebox can never damage it.

```
//192.168.2.46/Media/music/library /srv/music cifs \
  credentials=/etc/samba/creds/nas,uid=pi,gid=pi,file_mode=0444,dir_mode=0555,\
  iocharset=utf8,ro,nofail,_netdev,x-systemd.automount,x-systemd.idle-timeout=600 0 0
```

**By IP, deliberately, not `//vestnas.local/`.** The hostname version worked
for weeks and then stopped: the NAS began advertising only an IPv6 address
over mDNS, and the Pi resolves with `mdns4_minimal`, which is IPv4-only. The
mount failed at boot with `could not resolve address for vestnas.local`, and
because both services required the mount, the box went completely silent -
no music and no admin page to say why.

Pinning the name in `/etc/hosts` is **not** a fix. `/etc/hosts` here is
managed by cloud-init and rewritten on every boot, so that repair survives
until the next restart and then fails exactly as before.

The cost of an IP is that it must not move: give the NAS a static address, or
a DHCP reservation on the router.

Credentials live in `/etc/samba/creds/nas` (`0600`, root). Recreate with
`sudo /usr/local/sbin/nas-creds charlesvestal`, which prompts rather than
taking the password as an argument.

Three details that matter:

- **`x-systemd.automount` + `RequiresMountsFor=/srv/music`** on `owntone.service`
  only. If OwnTone starts before the mount exists, the music stays unavailable
  *even after it mounts later* - only a full rescan recovers (upstream issue
  #690). `nfc-jukebox.service` deliberately does **not** require the mount: it
  starts regardless, and `/api/status` reports `library_ok: false` with the
  reason, so a missing NAS produces a page that explains itself instead of a
  box that looks dead.
- **`nofail`** so a NAS that is off or asleep cannot stop the Pi booting.
- **Rescanning.** Network mounts send no inotify events, so OwnTone never
  notices new albums on its own, and the mount is read-only so OwnTone's
  `.init-rescan` trigger file is not an option either. `PUT /api/update` is the
  only route.

  **The nightly cron at 04:30 has probably never run.** `/etc/cron.d/owntone-rescan`
  is correct and cron is healthy, but the box is switched off overnight, and
  without `anacron` a missed `cron.d` job is never caught up. Found 2026-09-20:
  OwnTone's `updated_at` was stuck three days back, and two manual scans took it
  from 168 to 172 albums and 2085 to 2153 songs - four albums and 68 songs it had
  never seen.

  Use the **Rescan library** button on the admin page's System tab. It reports
  progress and the new counts. By hand:
  `curl -X PUT http://jukebox.local:3689/api/update`.

  The durable fix is a systemd timer with `Persistent=true`, which runs a missed
  job shortly after boot instead of skipping it. Not done yet.

The initial bulk scan of ~2,000 files took **520 seconds** over SMB on Wi-Fi.
That is a one-time cost; incremental scans are much faster.

**Card mappings were unaffected by the move from SD to NAS** - all seven
resolved with correct track counts immediately, because they store
library-relative paths. The NAS uses the same `<Artist>/<Album>/` layout.

The previous local copy is at `/srv/music.local`. Delete it to reclaim SD
space once you are happy with the NAS.

## Rebuilding onto a new SD card

1. `sudo bash deploy/backup.sh` on the old card; copy the archive off.
2. Burn Raspberry Pi OS Lite 64-bit. In Imager's settings: hostname
   `jukebox`, SSH with your public key, user `pi`, Wi-Fi, locale.
   **Give it the 2.4 GHz SSID**, and verify after first boot that
   `nmcli con show` actually lists it — the imager's Wi-Fi silently did not
   take on the 2026-09-15 rebuild, which is what caused that outage.
   `provision.sh` now refuses to run if no Wi-Fi profile exists.
3. `git clone` this repo to `/home/pi/owntone-nfc`.
4. `sudo bash deploy/provision.sh`
5. `sudo tar xzf jukebox-state-*.tar.gz -C /`
6. Copy music to `/srv/music`, preserving `<Artist>/<Album>/`.
7. `sudo systemctl restart owntone nfc-jukebox`

**Cards do not need re-registering** — mappings store library-relative paths,
so they survive a rebuild. The only thing that breaks them is changing the
`<Artist>/<Album>` folder layout.

## How the record behaves (changed 2026-09-18)

Lifting a card **pauses**; putting the same card back **resumes where it left
off**, however long it has been. The record stays on the platter until a
different one is put on.

- **Same card back** → resumes. No time limit.
- **Different card** → clears the queue and starts that album from track 1.
- **Album plays to its end** → the place is forgotten, so the next tap of that
  card starts side one.
- **Start over** on the admin page → restarts the loaded album from track 1.
  This is the only way to rewind a record mid-side.
- **Overnight** → `resume_reset_hour` (default 3am) draws a line under the day:
  an album loaded before the most recent 3am is no longer resumable, so a
  record abandoned at midnight starts fresh in the morning. Set it to `null` to
  turn that off.

**A reboot loses your place.** The position lives in OwnTone's queue and is not
persisted anywhere - restarting `owntone` or `nfc-jukebox`, or rebooting the
Pi, means the next tap starts from track 1. Nothing is broken when that
happens; it is the same as lifting the tonearm and switching the amp off. The
speaker choice is a different thing and *does* survive a reboot
(`/var/lib/nfc-jukebox/outputs.json`).

Note the grace timer no longer stops playback. At expiry it deselects the
AirPlay outputs - so the HomePods still go idle and are free for other senders,
which was always the point - and leaves the queue paused where it was.

## Retuning after the enclosure is built

The timing value is config, not code — edit `/etc/nfc-jukebox/config.yaml`
and restart:

- `presence_debounce_s` (0.5) — an enclosure adds air gap between card and
  reader, which will widen the absent-run. If cards start stuttering, raise it.

`bump_window_s` was removed on 2026-09-18 with the switch to resume-on-return:
a dropped read and a deliberate lift both resume now, so there was nothing left
for the window to decide. An old config file that still sets it is harmless -
unknown keys are ignored - but the line does nothing and can be deleted.

Re-measure with `spikes/presence_check.py` against the built box rather than
guessing. Note the original measurement was taken on one card type and did not
generalise: an NTAG213 held continuously for 11s, a 4-byte card dropped out
every 8ms. **Measure with the worst card you own, not the first one to hand.**

## Card technology: buy NTAG21x, not Mifare Classic

The UID tells you which you have: **7 bytes starting `04`** is an NTAG (good);
**4 bytes** is Mifare-Classic-style (flickers). As of 2026-09-20 the registry
holds 78 of the bad kind and 54 of the good.

**The 4-byte cards already registered are fine. Do not "fix" them.** Measured
2026-09-20 on `acd997ee` (José González/Veneer) sitting motionless on the built
box:

| | |
|---|---|
| present/removed episodes | 4325 in 150s (~29/s) |
| every hold | 0.01s |
| absence run, median | 26.1 ms |
| absence run, p99.9 | 26.5 ms |
| absence run, **worst** | **26.5 ms** |
| runs long enough to read as a lift | **0 of 1742** |

So the flicker is real and fast, and completely irrelevant: the absence run is
metronomic, p99.9 and worst differ by 0.1 ms, and `presence_debounce_s` at 0.5
has ~19x margin over it. Raising the debounce buys nothing and costs lag on
every lift. **This was proposed and rejected on the measurement.**

It also clears flicker of the 2026-09-19 play/pause glitch on this very card:
that was ~1.4s of absence, **53x the worst run this card produces**. Still
unexplained; look for something mechanical, not for a timing value to tune.

Re-measure with `spikes/presence_check.py` rather than reasoning about it. Both
probes used here pulse RSTPDN on GPIO20 first, because stopping the service
leaves the PN532 out of frame sync and the plain spike cannot open it.

For *new* cards buy **NTAG213 or NTAG215**, 25-30mm round wet inlay - better
engineered and with margin to spare, not because the existing ones misbehave. The chip name must be in
the listing; "13.56MHz NFC sticker" with no chip named is usually Mifare
Classic. 213/215/216 differ only in memory, which is irrelevant - this project
never writes to a tag, it reads the UID and nothing else. Avoid on-metal
(ferrite) tags, and avoid UID-changeable "magic" tags: unpredictable presence
is the exact failure this reader is most sensitive to.
