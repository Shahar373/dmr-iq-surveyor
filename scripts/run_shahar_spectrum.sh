#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d .venv ]]; then
  echo "Missing .venv. Run ./scripts/bootstrap.sh first." >&2
  exit 1
fi

source .venv/bin/activate
dmr-surveyor spectrum-batch config/shahar_recordings.yaml

echo
echo "Main report:"
echo "$ROOT_DIR/runs/20260713_163671500Hz/spectrum_batch_report.md"
