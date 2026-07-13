#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
pytest

echo
echo "Ready. Activate later with: source .venv/bin/activate"
echo "Run Shahar batch: ./scripts/run_shahar_recordings.sh"
