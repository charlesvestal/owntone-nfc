# Jukebox Runbook

Operational notes for the NFC vinyl jukebox. Design rationale lives in
`docs/superpowers/specs/2026-09-18-nfc-vinyl-jukebox-design.md`; this file is
what you want when something is broken or you are rebuilding.

## At a glance

| | |
|---|---|
| Host | `jukebox.local` (Wi-Fi, DHCP) |
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

With it off: rx 433 Mbit/s, DHCP instant. `provision.sh` applies this to every
Wi-Fi connection it finds. The setting lives only in NetworkManager on the SD
card, so a rebuild without provision.sh will hit this again.

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

## Wi-Fi: this Pi 4 cannot see 2.4 GHz

Unexplained and worth knowing before diagnosing anything else. This board:

- sees 5 GHz networks fine, including neighbours at signal 20 (very weak)
- sees **zero** 2.4 GHz networks, ever
- fails a *directed* probe for an SSID confirmed to be broadcasting on
  channel 1 with six clients connected
- reports both bands supported and channels 1-13 enabled at 20 dBm
- loads firmware cleanly with no errors in `dmesg`

So it is 5 GHz-only in practice, on DFS channel 100, with no fallback band.
Bare on a bench that is fine (-57 dBm, rx 433 Mbit/s). Inside a plastic
enclosure it drops to about -68 to -70 dBm and the receive rate collapses to
6 Mbit/s while transmit stays healthy - DHCP then never completes and the box
silently vanishes from the network.

Note the failure is binary, not gradual: rx is either ~400 Mbit/s or 6. The
same signature appeared with Wi-Fi power save enabled.

Things tried that did NOT fix it: disabling power save (necessary but not
sufficient here), pinning the BSSID to the stronger AP, forcing the 2.4 GHz
band, a static IP, `ipv4.may-fail no`.

Options if it recurs, cheapest first:
1. A USB Wi-Fi adapter with an external antenna - gets the antenna out of the
   enclosure rather than fighting it, and restores 2.4 GHz. The reliable fix.
2. Reflash onto a newer Raspberry Pi OS, in case the 2.4 GHz fault is the
   August 2023 firmware blob.
3. A different board. The Pi 3B is 2.4 GHz-only, which penetrates an enclosure
   better - but it is slower, and bets on 2.4 working.

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| Box invisible on the network after a reboot | Wi-Fi power save | See above. Check `iw wlan0 link` — rx bitrate at 6 Mbit/s is the tell. |
| Reader stops responding; log shows `ETIMEDOUT` repeatedly | PN532 wedged out of frame sync, usually by a service restart killing it mid-transaction | Automatic: the reader pulses RSTPDN on GPIO20 after 2 failures and recovers in ~4s. If it does not, check the RSTPDN↔D20 jumper. Manual: `pinctrl set 20 op dl; sleep 1; pinctrl set 20 op dh` |
| A card plays nothing; status flaps play/pause | Card technology with unstable presence — 4-byte Mifare-style cards report present for ~8ms at a time | Already handled by `presence_debounce_s` (0.5s). If a new card still flaps, raise it. |
| An album plays shuffled or not from track 1 | Shuffle enabled in OwnTone's UI | The controller forces shuffle/repeat off per card. If it persists, check the log for an error on that call. |
| Speaker drops mid-album, status shows an error | AirPlay receiver refused pairing; OwnTone fell back to local | Check Apple Home access setting. The error names which output was lost. |
| Admin page shows no cards | Page-level failure, not data loss | Check `curl localhost:8080/api/cards` first — the registry is almost certainly intact. |

## Music on the NAS

The library is an SMB mount from the UGREEN NAS, mounted read-only at
`/srv/music` so the jukebox can never damage it.

```
//vestnas.local/Media/music/library /srv/music cifs \
  credentials=/etc/samba/creds/nas,uid=pi,gid=pi,file_mode=0444,dir_mode=0555,\
  iocharset=utf8,ro,nofail,_netdev,x-systemd.automount,x-systemd.idle-timeout=600 0 0
```

Credentials live in `/etc/samba/creds/nas` (`0600`, root). Recreate with
`sudo /usr/local/sbin/nas-creds charlesvestal`, which prompts rather than
taking the password as an argument.

Three details that matter:

- **`x-systemd.automount` + `RequiresMountsFor=/srv/music`** on `owntone.service`.
  If OwnTone starts before the mount exists, the music stays unavailable *even
  after it mounts later* - only a full rescan recovers (upstream issue #690).
- **`nofail`** so a NAS that is off or asleep cannot stop the Pi booting.
- **Nightly rescan** via `/etc/cron.d/owntone-rescan`. Network mounts send no
  inotify events, so OwnTone never notices new albums on its own. The mount is
  read-only so OwnTone's `.init-rescan` trigger file is not an option; the cron
  calls `PUT /api/update` instead. Trigger one by hand from OwnTone's web UI or
  `curl -X PUT localhost:3689/api/update`.

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
3. `git clone` this repo to `/home/pi/owntone-nfc`.
4. `sudo bash deploy/provision.sh`
5. `sudo tar xzf jukebox-state-*.tar.gz -C /`
6. Copy music to `/srv/music`, preserving `<Artist>/<Album>/`.
7. `sudo systemctl restart owntone nfc-jukebox`

**Cards do not need re-registering** — mappings store library-relative paths,
so they survive a rebuild. The only thing that breaks them is changing the
`<Artist>/<Album>` folder layout.

## Retuning after the enclosure is built

Both timing values are config, not code — edit `/etc/nfc-jukebox/config.yaml`
and restart:

- `presence_debounce_s` (0.5) — an enclosure adds air gap between card and
  reader, which will widen the absent-run. If cards start stuttering, raise it.
- `bump_window_s` (0.25) — near-redundant now that presence is debounced.

Re-measure with `spikes/presence_check.py` against the built box rather than
guessing. Note the original measurement was taken on one card type and did not
generalise: an NTAG213 held continuously for 11s, a 4-byte card dropped out
every 8ms. **Measure with the worst card you own, not the first one to hand.**
