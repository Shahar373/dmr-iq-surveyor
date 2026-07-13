#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 RECORDING.wav METADATA.yaml [RUN_ID] [PROFILE]" >&2
  exit 2
fi

RECORDING="$1"
METADATA="$2"
RUN_ID="${3:-targeted_164537500_$(date +%Y%m%d_%H%M%S)}"
PROFILE="${4:-auto}"
OUTPUT_ROOT="runs/targeted/${RUN_ID}"
DATABASE="runs/inventory/dmr_inventory.sqlite3"

if [[ ! -d .venv ]]; then
  echo "Missing .venv. Run ./scripts/bootstrap.sh first." >&2
  exit 1
fi

source .venv/bin/activate
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1

nice -n 10 ionice -c2 -n7 dmr-surveyor targeted-decode \
  "$RECORDING" \
  --frequency 164537500 \
  --profile "$PROFILE" \
  --metadata "$METADATA" \
  --run-id "$RUN_ID" \
  --output "$OUTPUT_ROOT" \
  --database "$DATABASE"

echo
echo "Targeted run complete."
echo "Report: ${OUTPUT_ROOT}/targeted_run.md"
echo "Inventory: ${OUTPUT_ROOT}/inventory/phase5_report.md"
echo "Database: ${DATABASE}"
