#!/usr/bin/env bash
# Take a fresh Raspberry Pi OS Bookworm Lite (64-bit) install to a working
# jukebox. Idempotent: safe to re-run.
#
# Usage:  sudo bash deploy/provision.sh
set -euo pipefail

REPO="${REPO:-/home/pi/owntone-nfc}"
MUSIC="${MUSIC:-/srv/music}"
USER_NAME="${USER_NAME:-pi}"

# The NAS is not hardcoded: this repo does not carry the home network. Pass the
# share to have the mount written for you, e.g.
#   sudo NAS_SHARE=//10.0.0.5/Media/music/library bash deploy/provision.sh
# Leave it unset and /etc/fstab is left entirely alone.
NAS_SHARE="${NAS_SHARE:-}"
NAS_CREDS="${NAS_CREDS:-/etc/samba/creds/nas}"

say() { printf '\n==> %s\n' "$1"; }

say "Base packages"
apt-get update
apt-get install -y samba python3-venv wget

say "UART for the PN532"
# nfcpy has no I2C support, and presence detection is the whole product, so the
# reader runs over UART. This needs the login console off the port and the real
# PL011 on GPIO14/15 - the mini-UART's baud rate drifts with the VPU clock and
# produces a reader that works only intermittently.
grep -q '^dtoverlay=disable-bt' /boot/firmware/config.txt \
  || echo 'dtoverlay=disable-bt' >> /boot/firmware/config.txt
raspi-config nonint do_serial_hw 0
raspi-config nonint do_serial_cons 1
systemctl disable hciuart 2>/dev/null || true
usermod -aG dialout "$USER_NAME"

say "Wi-Fi: disable power save  <-- DO NOT SKIP"
# With power save on, this Pi 4 associated fine and then received almost
# nothing: rx stuck at 6 Mbit/s against tx 260, DHCP timing out, no traffic
# passing. It looks exactly like a dead network, a bad router or a shielded
# antenna, and it cost hours to find. With it off: rx 433 Mbit/s.
#
# This loop only *modifies* connections that already exist - Wi-Fi itself is
# expected to come from Raspberry Pi Imager. When the imager's Wi-Fi did not
# take, the loop silently matched nothing and provisioning reported success on
# a box with no way onto the network. That cost an evening. Fail loudly now.
wifi_conns=$(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"{print $1}')

if [ -z "$wifi_conns" ]; then
  cat >&2 <<'EOF'

    !!  NO WI-FI CONNECTION IS CONFIGURED  !!

    This box has no Wi-Fi profile, so it will be unreachable the moment it is
    unplugged from Ethernet. Raspberry Pi Imager's Wi-Fi setup did not take,
    or was skipped.

    Configure it, then re-run this script:

        sudo nmcli --ask dev wifi connect <SSID>

    Prefer the 2.4 GHz SSID. This board's 5 GHz path fails at around -69 dBm
    in the jukebox's normal position: it associates, completes the handshake,
    and then never completes DHCP. See docs/runbook.md.

EOF
  exit 1
fi

for conn in $wifi_conns; do
  nmcli connection modify "$conn" 802-11-wireless.powersave 2 || true
  echo "    power save disabled on '$conn'"
done

say "Music library"
mkdir -p "$MUSIC"
chown -R "$USER_NAME:$USER_NAME" "$MUSIC"

say "Samba share"
grep -q '^\[music\]' /etc/samba/smb.conf || cat >> /etc/samba/smb.conf <<EOF

[music]
   path = $MUSIC
   browseable = yes
   read only = no
   guest ok = yes
   force user = $USER_NAME
   create mask = 0664
   directory mask = 0775
EOF
systemctl restart smbd

# nmbd is NetBIOS name resolution for old Windows clients. Nothing here needs
# it -- macOS and Linux find SMB shares over mDNS -- and it starts before the
# Wi-Fi has an address, then spends 90 seconds failing with "No local IPv4
# non-loopback interfaces available" before systemd kills it. That is 90
# seconds of every boot bought for nothing. smbd, the actual file server, is
# untouched.
systemctl disable --now nmbd 2>/dev/null || true

say "Boot: wait for a network, not for every interface"
# NetworkManager-wait-online waits for *all* managed interfaces by default, so
# with a connection profile for ethernet that is normally unplugged it sits
# through the full 60s timeout on every boot -- and that wait is most of the
# time between power-on and being able to play a record. --any returns as soon
# as one interface has an address.
install -d /etc/systemd/system/NetworkManager-wait-online.service.d
install -m 0644 "$REPO/deploy/dropins/NetworkManager-wait-online-any.conf" \
    /etc/systemd/system/NetworkManager-wait-online.service.d/any.conf

# OwnTone waits for the library rather than trusting boot ordering. See the
# drop-in: network-online.target is reached before the radio has an address,
# and a single missed mount leaves a box with a library and no music server.
install -d /etc/systemd/system/owntone.service.d
install -m 0644 "$REPO/deploy/dropins/owntone-wait-for-music.conf" \
    /etc/systemd/system/owntone.service.d/mount.conf

# Keep the journal across reboots. Volatile by default, which means every
# restart erases the evidence -- and the failures on this box are intermittent
# and get noticed days later. Capped, because an unbounded journal on an SD
# card is how SD cards die.
install -d /etc/systemd/journald.conf.d
install -m 0644 "$REPO/deploy/dropins/journald-persistent.conf" \
    /etc/systemd/journald.conf.d/persistent.conf
install -d -m 2755 -o root -g systemd-journal /var/log/journal
systemctl daemon-reload
systemctl restart systemd-journald
# Restarting alone leaves it writing to /run until the next boot; the flush is
# what migrates it. Raspberry Pi OS ships Storage=volatile explicitly, so the
# drop-in is an override rather than a default being filled in.
journalctl --flush || true

say "Music mount"
# rsize is the load-bearing option here, not a tuning nicety. SMB3 defaults to
# 4MB reads; over Wi-Fi that is roughly a second of airtime delivered as one
# slug, and AirPlay 2's PTP timing packets queue behind it until a HomePod
# stereo pair audibly loses sync - a flanging effect that reads as a speaker
# fault. Measured on the box: 4MB reads put 70 of 80 pings over 200ms with a
# 4517ms worst case; 128KB reads put zero over 200ms *and* ran faster
# (2.0 vs 1.6 MB/s). Nothing is traded away. See docs/runbook.md.
#
# This lives here rather than only in the runbook because a rebuild onto a
# fresh SD card would otherwise come back with the 4MB default, and the symptom
# is audible rather than obvious.
MOUNT_OPTS="credentials=${NAS_CREDS},uid=${USER_NAME},gid=${USER_NAME}"
MOUNT_OPTS="${MOUNT_OPTS},file_mode=0444,dir_mode=0555,iocharset=utf8,ro"
MOUNT_OPTS="${MOUNT_OPTS},nofail,_netdev,x-systemd.automount,x-systemd.idle-timeout=600"
MOUNT_OPTS="${MOUNT_OPTS},rsize=131072,wsize=131072"

if [ -n "$NAS_SHARE" ]; then
  cp -n /etc/fstab /etc/fstab.orig 2>/dev/null || true
  fstab_tmp="$(mktemp)"
  # Drop any existing entry for this mountpoint before appending, so re-running
  # on a box that already has the mount UPDATES the options. Without that the
  # read-size fix would never reach an install that predates it.
  grep -vE "^[^#]*[[:space:]]${MUSIC}[[:space:]]+cifs([[:space:]]|$)" /etc/fstab       > "$fstab_tmp" || true
  printf '%s %s cifs %s 0 0
' "$NAS_SHARE" "$MUSIC" "$MOUNT_OPTS" >> "$fstab_tmp"
  install -m 0644 "$fstab_tmp" /etc/fstab
  rm -f "$fstab_tmp"
  systemctl daemon-reload
  echo "  /etc/fstab entry for $MUSIC written (original kept at /etc/fstab.orig)"
  echo "  Credentials are NOT written here - see 'sudo /usr/local/sbin/nas-creds'."
else
  echo "  NAS_SHARE unset, leaving /etc/fstab alone."
  echo "  If $MUSIC is already mounted, confirm it has rsize=131072:"
  echo "    mount | grep $MUSIC"
fi

say "Nightly library rescan"
# A read-only network mount sends no inotify events and cannot carry OwnTone's
# .init-rescan trigger file, so new albums are invisible until it is told.
#
# A systemd timer rather than cron, and the reason is Persistent=true: this box
# is switched off at night, so a 04:30 cron entry never fires and cron does not
# catch up. The timer runs the missed job shortly after the next boot instead.
# Found the hard way -- the cron it replaces had never run once.
install -m 0644 "$REPO/deploy/owntone-rescan.service" \
    /etc/systemd/system/owntone-rescan.service
install -m 0644 "$REPO/deploy/owntone-rescan.timer" \
    /etc/systemd/system/owntone-rescan.timer
rm -f /etc/cron.d/owntone-rescan
systemctl daemon-reload
systemctl enable --now owntone-rescan.timer

say "OwnTone"
if ! command -v owntone >/dev/null; then
  wget -q -O - https://raw.githubusercontent.com/owntone/owntone-apt/refs/heads/master/repo/rpi/owntone.gpg \
    | gpg --dearmor --output /usr/share/keyrings/owntone-archive-keyring.gpg
  # Match the running release rather than hardcoding one: OwnTone publishes
  # bookworm, trixie and others, and this box may be reflashed onto a newer
  # image to chase a Wi-Fi firmware fix.
  CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
  wget -q -O /etc/apt/sources.list.d/owntone.list \
    "https://raw.githubusercontent.com/owntone/owntone-apt/refs/heads/master/repo/rpi/owntone-${CODENAME}.list"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y owntone
fi
cp "$REPO/deploy/owntone.conf.example" /etc/owntone.conf
systemctl enable --now owntone

say "Power control permission"
# The admin page offers shut down / restart: pulling the plug on a running Pi
# is how SD cards get corrupted, and this is an appliance people will unplug.
# Scoped to exactly those two commands so the grant cannot be used for
# anything else. Raspberry Pi OS ships pi with NOPASSWD: ALL, which makes this
# redundant today - it is here so the feature survives that being tightened.
cat > /etc/sudoers.d/nfc-jukebox-power <<EOF
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot
EOF
chmod 440 /etc/sudoers.d/nfc-jukebox-power
visudo -c -f /etc/sudoers.d/nfc-jukebox-power

say "Jukebox service"
mkdir -p /opt/nfc-jukebox /etc/nfc-jukebox /var/lib/nfc-jukebox
chown "$USER_NAME:$USER_NAME" /etc/nfc-jukebox /var/lib/nfc-jukebox
[ -d /opt/nfc-jukebox/venv ] || python3 -m venv /opt/nfc-jukebox/venv
/opt/nfc-jukebox/venv/bin/pip install -q -e "$REPO"
[ -f /etc/nfc-jukebox/config.yaml ] \
  || cp "$REPO/deploy/config.yaml.example" /etc/nfc-jukebox/config.yaml
chown "$USER_NAME:$USER_NAME" /etc/nfc-jukebox/config.yaml
cp "$REPO/deploy/nfc-jukebox.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nfc-jukebox

say "Done. Reboot if the UART settings changed."
echo "    Restore cards with:  sudo tar xzf jukebox-state-*.tar.gz -C /"
echo "    Admin page:          http://\$(hostname).local:8080"
echo "    OwnTone:             http://\$(hostname).local:3689"
