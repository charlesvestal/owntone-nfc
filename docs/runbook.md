# Jukebox Runbook

Operational notes for the NFC vinyl jukebox. Design rationale lives in
`docs/superpowers/specs/2026-09-18-nfc-vinyl-jukebox-design.md`; this file is
what you want when something is broken or you are rebuilding.

## At a glance

| | |
|---|---|
| Host | `jukebox.local` (Wi-Fi 2.4 GHz `<2.4GHz-SSID>`, DHCP) |
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

With `802-11-wireless.powersave` on (the default), a Pi 4 associates normally,
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

When debugging Wi-Fi, **change one thing at a time.** A fix applied early and
then buried under other changes (a BSSID pin, a static IP) whose own failure
masks it is how this one stayed hidden for hours.

## Wi-Fi: keep the Pi's address stable

Give the Pi a **DHCP reservation** on your router. Some routers hand out a new
address when the Pi re-associates, even though its MAC never changes. The NAS
connection is still bound to the old address, so it hangs silently until CIFS
declares the server dead, and every card tapped in that window fails with
"OwnTone error while starting <album>: timed out".

`provision.sh` mounts the share with `echo_interval=5`, which cuts that window
from 180 seconds (the default, three missed 60s echoes) to about 15. The
reservation removes it. The tell in the logs is a new DHCP lease followed a
few minutes later by:

    CIFS: VFS: \\<nas-ip> has not responded in 180 seconds. Reconnecting...

## AirPlay

**`user_agent = "AirPlay/999.0.0"` in `/etc/owntone.conf` is required.** Apple
OS 27 (HomePod / tvOS / macOS, AirTunes `srcvers` 980.x) gates `GET /info` on
the User-Agent and rejects OwnTone's default with 403, before any
authentication. Without it, every Apple device stops working as it updates to
27. Upstream: owntone/owntone-server#2042.

**Apple Home → Home Settings → Allow Speakers & TV Access → "Anyone On the Same
Network".** Apple's default cannot be satisfied by a Linux sender; the symptom
is a speaker that deselects itself the moment playback starts.

**What works:** a HomePod stereo pair, with both HomePods selected as two
separate outputs. That is real stereo — verify it with a hard-panned test file
rather than by ear; impressions about stereo were wrong twice.

**What does not work:** targeting an Apple TV. It pairs, accepts the session,
shows the track and honours seeks, but never advances its playhead or produces
audio. Best explanation is that as AirPlay group leader it expects group
semantics OwnTone does not implement. Do not re-attempt without new
information.

**macOS is not usable as a test receiver** — macOS 27 returns 403 to an
unauthenticated `/info` probe even over loopback.

## Wi-Fi: prefer 2.4 GHz unless the router is close

**A Pi 4's onboard 5 GHz receive path can fail outright at a range where
2.4 GHz is fine.** If your router broadcasts the bands as separate SSIDs,
configure the 2.4 GHz one.

Measured on the reference build, same board, same spot, minutes apart:

| Band | Channel | Signal | DHCP | Rate |
|---|---|---|---|---|
| 5 GHz | 36 (non-DFS) | -69 dBm | **never completes** | - |
| 2.4 GHz | 11 | **-58 dBm** | instant | 130 Mbit/s |

The 5 GHz failure is always the same and is thoroughly misleading: it
associates, completes the 4-way handshake, logs *"Connected to wireless
network"* - and then DHCP opens a transaction and gets nothing. The box is
alive and believes it is fine, while being completely unreachable. `iw` during
the attempt:

    rate=6.0/390.0    d_rx=0

Transmit negotiated 390 Mbit/s; receive was pinned at the 6.0 Mbit/s floor and
not one byte arrived. DHCP timing out is just the first thing you notice.

### What this is NOT

Each of these was a plausible theory and each was wrong. Do not re-run them.

- **Not DFS.** The identical failure reproduces on channel 36, which is not
  radar-protected.
- **Not the signal level.** The board also failed at -63 to -65 dBm, holding
  the association for minutes with a static address (so DHCP was bypassed) and
  passing zero bytes, while a Mac in the same room used the same AP at -73 dBm
  with 0% loss.
- **Not the enclosure.** 2.4 GHz works fine from inside it.
- **Not a 2.4 GHz radio fault.** Older firmware was once seen listing no 2.4 GHz
  networks at all; a current kernel (6.18, Trixie) scans them normally. If you
  see that, update before buying hardware.

### A USB Wi-Fi adapter is usually not needed

2.4 GHz carries a FLAC in and two AirPlay streams out with a median latency of
5 ms. Jitter that looks like a reason to buy an adapter is usually the 4 MB SMB
read size, fixed in software (see *Read size* below). Measure `rsize` before
spending money on a radio.

If you do need 5 GHz, or 2.4 also fails where the box sits, pick an adapter
whose driver is in the mainline kernel so nothing has to be rebuilt when the
kernel updates: the **Alfa AWUS036ACM** (MediaTek MT7612U, `mt76x2u`,
dual-band, detachable antenna) or the cheaper **Panda PAU0B** (RT5572,
`rt2800usb`). Avoid RTL8811AU/8821AU/8812AU sticks (TP-Link Archer T2U/T3U and
most cheap AC dongles) - out-of-tree DKMS drivers are exactly the wrong
property for an appliance that must come back silently after a reboot.

### Recommended profiles

| Profile | Band | Autoconnect | Priority | Powersave |
|---|---|---|---|---|
| `<2.4GHz-SSID>` | 2.4 GHz | yes | 10 | off |
| `<5GHz-SSID>` (optional) | 5 GHz | **no** | 0 | off |

If you keep a 5 GHz profile, set it `autoconnect no`. Left enabled it can grab
`wlan0` at boot, spend 45 seconds failing DHCP, and wedge the box off the
network entirely.

## When the box vanishes from the network

**First suspect: no Wi-Fi profile at all.** Raspberry Pi Imager's Wi-Fi setup
can silently fail to take. `nmcli con show` listing only the wired connection,
and an empty `/etc/NetworkManager/system-connections/`, is the whole answer.
`provision.sh` refuses to run in that state rather than reporting success on a
box that cannot reach the network.

`jukebox.local` not resolving looks like an mDNS problem, and usually is not.
To tell in one step, ping-sweep the subnet and grep the ARP table for the Pi's
MAC prefix (`e4:5f:01`, `dc:a6:32`, `d8:3a:dd` or `2c:cf:67` for Pi 4/5 boards,
`b8:27:eb` for older ones). Absent means it never got a DHCP lease, so the
problem is the radio, not the name.

A router may list several stale `jukebox`, `jukebox-1`, `jukebox-2` entries:
that is the router disambiguating different MACs (wlan0 and eth0, or earlier
boards) that all announced the same hostname. Not a fault.

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| Box invisible on the network after a reboot | Wi-Fi power save | See above. Check `iw wlan0 link` — rx bitrate at 6 Mbit/s is the tell. |
| Box invisible and `jukebox.local` does not resolve | Usually no Wi-Fi profile, or `wlan0` on 5 GHz | First: `nmcli con show` — if there is no Wi-Fi connection, that is the whole answer. Confirm the box is absent rather than merely unnamed by ping-sweeping the subnet and grepping the ARP table for the Pi's MAC prefix; no entry means it never got a DHCP lease, so it is not mDNS. Then check it is on the 2.4 GHz SSID. |
| A card times out now and then, then works on a second tap | The Pi's address changed and the NAS connection is hanging | See *keep the Pi's address stable*. `journalctl \| grep -E "new lease\|not responded"` shows the pairing. |
| Reader stops responding; log shows `ETIMEDOUT` repeatedly | PN532 wedged out of frame sync, usually by a service restart killing it mid-transaction | Automatic: the reader pulses RSTPDN on GPIO20 after 2 failures and recovers in ~4s. If it does not, check the RSTPDN↔D20 jumper. Manual: `pinctrl set 20 op dl; sleep 1; pinctrl set 20 op dh` |
| A card plays nothing; status flaps play/pause | Card technology with unstable presence — 4-byte Mifare-style cards report present for ~8ms at a time | Already handled by `presence_debounce_s` (0.5s). If a new card still flaps, raise it. |
| An album plays shuffled or not from track 1 | Shuffle enabled in OwnTone's UI | The controller forces shuffle/repeat off per card. If it persists, check the log for an error on that call. |
| Speaker drops mid-album, status shows an error | AirPlay receiver refused pairing; OwnTone fell back to local | Check Apple Home access setting. The error names which output was lost. |
| Admin page shows no cards | Page-level failure, not data loss | Check `curl localhost:8080/api/cards` first — the registry is almost certainly intact. |

## Music on a NAS

The library can live on the SD card, or on a NAS mounted read-only at
`/srv/music` so the jukebox can never damage it:

```
//<nas-ip>/<share>/<path> /srv/music cifs \
  credentials=/etc/samba/creds/nas,uid=pi,gid=pi,file_mode=0444,dir_mode=0555,\
  iocharset=utf8,ro,nofail,_netdev,x-systemd.automount,\
  rsize=131072,wsize=131072,echo_interval=5 0 0
```

Do not add `x-systemd.idle-timeout`. Each idle unmount drops OwnTone's inotify
watches; re-adding them remounts the share and re-walks every album folder for
~6 minutes, and a card tapped during that walk fails with "OwnTone error while
starting <album>: timed out". The automount still mounts on first access after
boot, and the SMB client reconnects on its own if the NAS goes away.

`provision.sh` writes this line for you if you pass the share, and re-running
it **updates** an existing entry rather than duplicating it - which is how
mount-option fixes reach a box built before they existed:

```
sudo NAS_SHARE=//<nas-ip>/<share>/<path> bash deploy/provision.sh
```

Leave `NAS_SHARE` unset and `/etc/fstab` is left entirely alone. The address is
passed in rather than committed: this repo does not carry anyone's home
network. Credentials are separate and are not written by provisioning.

**Mount by IP, not `//<nas>.local/`.** The hostname form can work for weeks and
then stop: a NAS may begin advertising only an IPv6 address over mDNS, and the
Pi resolves with `mdns4_minimal`, which is IPv4-only. The mount fails at boot
with `could not resolve address for <nas>.local`, and because OwnTone requires
the mount, the box plays nothing.

Pinning the name in `/etc/hosts` is **not** a fix. `/etc/hosts` on Raspberry Pi
OS is managed by cloud-init and rewritten on every boot, so that repair
survives until the next restart and then fails exactly as before.

The cost of an IP is that it must not move: give the NAS a static address, or
a DHCP reservation on the router.

Credentials live in `/etc/samba/creds/nas` (`0600`, root). Create or replace
them with `sudo nas-creds <nas-username>`, which prompts rather than taking the
password as an argument.

### Read size: cap it at 128 KB, or playback stutters

**`rsize=131072` is not optional over Wi-Fi.** SMB3 defaults to 4 MB reads. On
a Wi-Fi link that is about a second of airtime delivered as a single slug, and
everything else queues behind it - including AirPlay 2's PTP timing packets,
which are what keep a HomePod stereo pair in sync. The audible symptom is a
flanging or delay effect swelling in and out: the two speakers drift apart and
you are hearing comb filtering. It sounds like a speaker fault or an OwnTone
fault. It is neither.

Measured on the reference build, pinging the gateway while reading from the
NAS with `iflag=direct` so each read is exactly one wire request:

| Read size | Worst RTT | Pings over 200 ms | Throughput |
|---|---|---|---|
| idle | 74 ms | 0 of 80 | - |
| **4 MB** (the default) | **4517 ms** | **70 of 80** | 1.6 MB/s |
| **128 KB** | 189 ms | 0 of 80 | **2.0 MB/s** |

Smaller reads are *faster as well as smoother* - nothing is traded away. Across
one-minute samples, 4 MB reads put a spike above 200 ms into 41% of minutes;
with the cap, four consecutive minutes of playback peaked at 53.6 ms.

This also disposes of a whole class of symptom that reads as a Wi-Fi fault:
multi-second stalls, ping swinging from 7 ms to 400 ms, and the rx rate
collapsing from 72.2 to 39.0 Mbit/s while tx holds steady. That is
self-inflicted congestion from one large read, not a weak or broken radio.
**Check the read size before blaming the link.**

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

  `owntone-rescan.timer` runs it nightly. It is a systemd timer rather than a
  cron entry because of `Persistent=true`: if the box is off at the scheduled
  time - a power cut, or a smart plug that switches it off at night - cron
  simply skips the run and never catches up, and a library can go days stale
  without anyone noticing. The timer runs the missed job shortly after the next
  boot, and `systemctl list-timers` shows when it last fired.

  To rescan now, use the **Rescan library** button on the admin page's System
  tab, which reports progress and the new counts. By hand:
  `curl -X PUT http://jukebox.local:3689/api/update`.

The initial bulk scan of ~2,000 files took **520 seconds** over SMB on Wi-Fi.
That is a one-time cost; incremental scans are much faster.

Moving the library (SD card to NAS, or NAS to NAS) does not affect cards, as
long as the `<Artist>/<Album>/` layout is the same: mappings store
library-relative paths.

## Reading logs across a power cut

**There is no RTC on a Pi.** On boot, systemd restores the clock from
`/var/lib/systemd/timesync/clock` - the last time written before power was
lost - and only jumps to the real time once NTP syncs a few seconds later. So
the first entries of every boot after a power cut are stamped with the time
power was lost:

    systemd[1]: System time advanced to timestamp on
                /var/lib/systemd/timesync/clock: <time power was lost>

which makes `journalctl --list-boots` show a boot that appears to begin before
the previous one ended, and looks for all the world like the box never
rebooted. `uptime -s` is the honest answer. That saved timestamp is also the
most precise record of when power was cut, since timesyncd writes it
periodically.

## Rebuilding onto a new SD card

1. `sudo bash deploy/backup.sh` on the old card; copy the archive off.
2. Burn Raspberry Pi OS Lite 64-bit. In Imager's settings: hostname
   `jukebox`, SSH with your public key, user `pi`, Wi-Fi, locale.
   **Give it the 2.4 GHz SSID**, and verify after first boot that
   `nmcli con show` actually lists it — the imager's Wi-Fi can silently fail
   to take. `provision.sh` refuses to run if no Wi-Fi profile exists.
   Reserve an address for the Pi on your router while you are there.
3. `git clone` this repo to `/home/pi/owntone-nfc`.
4. `sudo bash deploy/provision.sh`, adding `NAS_SHARE=//<nas-ip>/<share>/<path>`
   if the library is on a NAS, then `sudo nas-creds <nas-username>`.
5. `sudo tar xzf jukebox-state-*.tar.gz -C /`
6. Copy music to `/srv/music`, preserving `<Artist>/<Album>/`.
7. `sudo systemctl restart owntone nfc-jukebox`

**Cards do not need re-registering** — mappings store library-relative paths,
so they survive a rebuild. The only thing that breaks them is changing the
`<Artist>/<Album>` folder layout.

## How the record behaves

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
**4 bytes** is Mifare-Classic-style (flickers).

**4-byte cards you already have are fine. Do not "fix" them.** Measured on a
4-byte card sitting motionless on the built box:

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

It also means a one-off play/pause glitch is not flicker: one seen on that same
card was ~1.4s of absence, **53x the worst run it produces**. Look for
something mechanical, not for a timing value to tune.

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
