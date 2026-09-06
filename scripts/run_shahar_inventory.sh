#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d .venv ]]; then
  echo "Missing .venv. Run ./scripts/bootstrap.sh first." >&2
  exit 1
fi

DECODES="$ROOT_DIR/runs/20260713_163671500Hz/decodes"
if [[ ! -d "$DECODES" ]]; then
  echo "Missing Phase 4.1 decodes: $DECODES" >&2
  echo "Run ./scripts/run_shahar_decode.sh first." >&2
  exit 1
fi

source .venv/bin/activate
python -m pip install -e . >/dev/null

dmr-surveyor inventory-batch config/shahar_recordings.yaml

echo
echo "Main report:"
echo "$ROOT_DIR/runs/20260713_163671500Hz/inventory/phase5_report.md"
echo
echo "Persistent database:"
echo "$ROOT_DIR/runs/inventory/dmr_inventory.sqlite3"
