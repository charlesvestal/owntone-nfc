#!/usr/bin/env bash
#
# Collect artwork for every album that still has no card, and open the review
# sheet. Safe to run as often as you like: work already done is cached in the
# manifest, so a re-run only touches albums that are new, that failed, or whose
# override you have just edited.
#
# The fetching runs on the Pi because that is where the music actually is --
# library artwork is preferred over anything downloaded, and it can only be
# read there. Results are copied back here for review and printing.
#
#   ./refresh.sh                 # new albums only
#   ./refresh.sh --refetch       # start again from scratch
#
set -euo pipefail

HOST="${JUKEBOX_HOST:-pi@jukebox.local}"
REMOTE_DIR="${JUKEBOX_CARDART_DIR:-/home/pi/cardart-out}"
LOCAL_DIR="${CARDART_DIR:-$HOME/Documents/_Personal/music/cardart}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> sending tools to $HOST"
ssh "$HOST" "mkdir -p /tmp/cardart '$REMOTE_DIR'"
scp -q "$HERE/artlib.py" "$HERE/fetch_art.py" "$HOST:/tmp/cardart/"

# Overrides and the manifest live with the images on this machine, since that
# is where they get edited. Push them up so the run honours them.
mkdir -p "$LOCAL_DIR"
for f in overrides.json manifest.json; do
    [ -f "$LOCAL_DIR/$f" ] && scp -q "$LOCAL_DIR/$f" "$HOST:$REMOTE_DIR/"
done

echo "==> fetching"
ssh "$HOST" "cd /tmp/cardart && python3 fetch_art.py --out '$REMOTE_DIR' $*"

echo "==> copying results to $LOCAL_DIR"
rsync -a "$HOST:$REMOTE_DIR/" "$LOCAL_DIR/"

echo "==> building review sheet"
python3 "$HERE/make_review.py" --dir "$LOCAL_DIR"
command -v open >/dev/null && open "$LOCAL_DIR/review.html"

cat <<EOF

Anything flagged, fix in:
  $LOCAL_DIR/overrides.json

  {"Artist/Album": {"artist": "...", "album": "..."}}   search differently
  {"Artist/Album": {"url": "https://..."}}              use this image
  {"Artist/Album": {"skip": "why"}}                     no card wanted

then run this again.
EOF
