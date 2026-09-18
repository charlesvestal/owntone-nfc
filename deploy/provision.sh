#!/usr/bin/env bash
# Take a fresh Raspberry Pi OS Bookworm Lite (64-bit) install to a working
# jukebox. Idempotent: safe to re-run.
#
# Usage:  sudo bash deploy/provision.sh
set -euo pipefail

REPO="${REPO:-/home/pi/owntone-nfc}"
MUSIC="${MUSIC:-/srv/music}"
USER_NAME="${USER_NAME:-pi}"

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
for conn in $(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"{print $1}'); do
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

say "OwnTone"
if ! command -v owntone >/dev/null; then
  wget -q -O - https://raw.githubusercontent.com/owntone/owntone-apt/refs/heads/master/repo/rpi/owntone.gpg \
    | gpg --dearmor --output /usr/share/keyrings/owntone-archive-keyring.gpg
  wget -q -O /etc/apt/sources.list.d/owntone.list \
    https://raw.githubusercontent.com/owntone/owntone-apt/refs/heads/master/repo/rpi/owntone-bookworm.list
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
