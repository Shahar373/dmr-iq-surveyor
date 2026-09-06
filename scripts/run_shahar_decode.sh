#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d .venv ]]; then
  echo "Missing .venv. Run ./scripts/bootstrap.sh first." >&2
  exit 1
fi

CANDIDATES="$ROOT_DIR/runs/20260713_163671500Hz/candidates/candidates.json"
if [[ ! -f "$CANDIDATES" ]]; then
  echo "Missing Phase 3 candidates: $CANDIDATES" >&2
  echo "Run ./scripts/run_shahar_detection.sh first." >&2
  exit 1
fi

source .venv/bin/activate
python -m pip install -e . >/dev/null

export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

COMMAND=(dmr-surveyor decode-batch config/shahar_recordings.yaml)

if command -v ionice >/dev/null 2>&1; then
  ionice -c 2 -n 7 nice -n 10 "${COMMAND[@]}"
else
  nice -n 10 "${COMMAND[@]}"
fi

echo
echo "Main report:"
echo "$ROOT_DIR/runs/20260713_163671500Hz/decodes/decode_batch_report.md"
