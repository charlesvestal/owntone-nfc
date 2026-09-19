#!/usr/bin/env bash
# Archive the portable state: the card registry, config, and artwork pins.
#
# Music is NOT included - it is large and lives on the master copy. The card
# registry is the irreplaceable part: it is built by hand, one tap at a time,
# and nothing else on the box can reconstruct it.
#
# overrides.json is in for the same reason. The collected images are not - they
# are big and a Collect run re-fetches them - but the overrides are the record
# of every album a search got wrong and a human corrected. Losing them means
# rediscovering each one at the printer.
set -euo pipefail

OUT="${1:-jukebox-state-$(date +%Y%m%d-%H%M).tar.gz}"
# Optional files are listed separately: tar fails the whole archive on a
# missing path, and a box that has never collected artwork has no overrides.
FILES=(etc/nfc-jukebox/cards.yaml etc/nfc-jukebox/config.yaml)
for optional in var/lib/nfc-jukebox/outputs.json \
                var/lib/nfc-jukebox/artwork/overrides.json; do
  if [ -e "/$optional" ]; then FILES+=("$optional"); fi
done
tar czf "$OUT" -C / "${FILES[@]}"

echo "Wrote $OUT"
echo "Restore with: sudo tar xzf $OUT -C / && sudo systemctl restart nfc-jukebox"
