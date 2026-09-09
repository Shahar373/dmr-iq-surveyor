#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

RECORDING_1="/home/shahar/Documents/SDRconnect_IQ_20260713_150242_163671500HZ.wav"
RECORDING_2="/home/shahar/Documents/SDRconnect_IQ_20260713_150256_163671500HZ.wav"

for recording in "$RECORDING_1" "$RECORDING_2"; do
  if [[ ! -f "$recording" ]]; then
    echo "ERROR: Recording not found: $recording" >&2
    exit 1
  fi
done

if [[ ! -d .venv ]]; then
  echo "ERROR: .venv does not exist. Run ./scripts/bootstrap.sh first." >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

dmr-surveyor inspect-batch config/shahar_recordings.yaml

echo
echo "Main report:"
echo "$PROJECT_DIR/runs/20260713_163671500Hz/batch_report.md"
echo
echo "To package the results for review:"
echo "cd '$PROJECT_DIR' && python3 -m zipfile -c shahar_iq_inspection_results.zip runs/20260713_163671500Hz"
