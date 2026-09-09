#!/usr/bin/env bash
# Download Leaflet into the field app's static directory so the map works
# without reaching a CDN. Run this once, while online.
#
# The field app tries the vendored copy first and falls back to the CDN, so
# this is an optional resilience step, not a prerequisite.
set -euo pipefail

VERSION="${LEAFLET_VERSION:-1.9.4}"
DEST="$(dirname "$0")/../src/dmr_iq_surveyor/web/static/vendor"

# Two mirrors, in the order the app itself tries them. A network that blocks
# one often allows the other.
MIRRORS=(
  "https://cdnjs.cloudflare.com/ajax/libs/leaflet/${VERSION}"
  "https://unpkg.com/leaflet@${VERSION}/dist"
)

mkdir -p "$DEST" "${DEST}/images"

fetch() {  # fetch <relative path> <required: yes|no>
  local asset="$1" required="$2" base
  for base in "${MIRRORS[@]}"; do
    if curl -fsSL "${base}/${asset}" -o "${DEST}/${asset}"; then
      echo "  ${asset} <- ${base}"
      return 0
    fi
  done
  if [[ "$required" == "yes" ]]; then
    echo "could not fetch ${asset} from any mirror; the app will fall back to the CDN" >&2
    return 1
  fi
  echo "  (optional ${asset} not fetched)"
  return 0
}

echo "vendoring leaflet ${VERSION} into ${DEST}"
fetch leaflet.js yes
fetch leaflet.css yes
for image in marker-icon.png marker-icon-2x.png marker-shadow.png layers.png layers-2x.png; do
  fetch "images/${image}" no
done
echo "done"
