#!/usr/bin/env bash
# Desk-side card registration: reads UIDs from the USB NFC reader on this
# Mac and posts them to the jukebox, same as tapping the box itself.
#
#   ./remote-tag.sh                  -- talk to jukebox.local:8080
#   ./remote-tag.sh --host other:8080 [any other tools/desk_reader.py flag]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -x .venv/bin/python3 ]; then
    echo "No .venv here -- set one up first (python3 -m venv .venv)." >&2
    exit 1
fi

.venv/bin/python3 -c "import smartcard" 2>/dev/null \
    || .venv/bin/pip install --quiet pyscard requests

exec .venv/bin/python3 tools/desk_reader.py --host jukebox.local:8080 "$@"
