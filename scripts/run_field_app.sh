#!/usr/bin/env bash
# Serve the Phase 7 field app from the Raspberry Pi, reachable from a phone
# on the same hotspot.
#
# Usage: ./scripts/run_field_app.sh [band] [site]
set -euo pipefail

BAND="${1:-central_800_narrow}"
SITE="${2:-mobile}"
OUTPUT="${FIELD_OUTPUT:-runs/field}"
PORT="${FIELD_PORT:-8765}"

cd "$(dirname "$0")/.."
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if [[ ! -f "config/sites/${SITE}.yaml" && ! -f "${SITE}" ]]; then
  echo "No site profile 'config/sites/${SITE}.yaml'." >&2
  echo "Copy config/sites/home.example.yaml and record the antenna, receiver and fixed gain." >&2
  exit 1
fi

# --host 0.0.0.0 so the phone can reach it; --token auto because that also
# means anyone else on the hotspot could otherwise start a capture.
exec dmr-surveyor web serve \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --token auto \
  --band "${BAND}" \
  --site "${SITE}" \
  --output "${OUTPUT}"
