#!/usr/bin/env bash
# deploy/nas-creds.sh -> /usr/local/sbin/nas-creds
#
# Write the NAS login used by the /srv/music mount. Prompts for the password
# rather than taking it as an argument, so it never lands in shell history or
# the process list.
#
#   sudo nas-creds <nas-username>
set -euo pipefail

CREDS="${NAS_CREDS:-/etc/samba/creds/nas}"

if [ $# -ne 1 ]; then
  echo "usage: sudo nas-creds <nas-username>" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "nas-creds: run with sudo" >&2
  exit 1
fi

read -r -s -p "Password for $1 on the NAS: " password
echo

install -d -m 0700 "$(dirname "$CREDS")"
umask 077
printf 'username=%s\npassword=%s\n' "$1" "$password" > "$CREDS"
chmod 0600 "$CREDS"
echo "Wrote $CREDS. The share mounts on next access, e.g. ls /srv/music"
