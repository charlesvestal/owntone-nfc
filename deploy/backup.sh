#!/usr/bin/env bash
# Archive the portable state: the card registry and config.
#
# Music is NOT included - it is large and lives on the master copy. The card
# registry is the irreplaceable part: it is built by hand, one tap at a time,
# and nothing else on the box can reconstruct it.
set -euo pipefail

OUT="${1:-jukebox-state-$(date +%Y%m%d-%H%M).tar.gz}"
tar czf "$OUT" -C / \
  etc/nfc-jukebox/cards.yaml \
  etc/nfc-jukebox/config.yaml \
  var/lib/nfc-jukebox/outputs.json 2>/dev/null || \
tar czf "$OUT" -C / etc/nfc-jukebox/cards.yaml etc/nfc-jukebox/config.yaml

echo "Wrote $OUT"
echo "Restore with: sudo tar xzf $OUT -C / && sudo systemctl restart nfc-jukebox"
