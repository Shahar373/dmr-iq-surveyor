from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from dmr_iq_surveyor.decode import (
    DecoderSettings,
    run_channel_extraction,
    run_decoder_profiles,
)
from dmr_iq_surveyor.inventory import build_inventory


def load_capture_metadata(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() == ".json":
        payload = json.loads(source.read_text(encoding="utf-8"))
    else:
        payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("Capture metadata must be a YAML or JSON mapping")
    return dict(payload)


def run_targeted_capture(
    input_path: str | Path,
    output_root: str | Path,
    *,
    frequency_hz: float,
    profile_name: str = "auto",
    recording_id: str | None = None,
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    database_path: str | Path | None = None,
    decoder_settings: DecoderSettings | None = None,
    iq_order: str = "IQ",
) -> dict[str, Any]:
    source = Path(input_path).expanduser().resolve()
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    resolved_recording_id = recording_id or source.stem
    resolved_run_id = run_id or root.name
    candidate_id = f"T{int(round(frequency_hz))}"
    attempt_dir = (
        root
        / "decodes"
        / candidate_id
        / resolved_recording_id
        / iq_order.lower()
    )
    extraction = run_channel_extraction(
        source,
        attempt_dir,
        candidate_frequency_hz=frequency_hz,
        profile_name=profile_name,
        assumed_iq_order=iq_order,
        candidate_id=candidate_id,
        recording_id=resolved_recording_id,
        capture_metadata=metadata,
    )
    decoder = run_decoder_profiles(
        extraction["wav_path"],
        attempt_dir / "decoder",
        settings=decoder_settings,
    )
    inventory = build_inventory(
        root / "decodes",
        root / "inventory",
        database_path=database_path,
        run_id=resolved_run_id,
    )
    result = {
        "run_id": resolved_run_id,
        "candidate_id": candidate_id,
        "recording_id": resolved_recording_id,
        "frequency_hz": float(frequency_hz),
        "profile_name": extraction["extraction_profile"],
        "attempt_dir": str(attempt_dir),
        "extraction": extraction,
        "decoder": decoder,
        "inventory": inventory,
    }
    (root / "targeted_run.json").write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )
    report = f"""# Targeted DMR capture run

- Run ID: **{resolved_run_id}**
- Recording: **{resolved_recording_id}**
- Frequency: **{frequency_hz / 1e6:.6f} MHz**
- Extraction profile: **{extraction['extraction_profile']}**
- Input rate: **{extraction['input_sample_rate_hz']:,} S/s**
- Decoder status: **{decoder['status']}**
- Best polarity: **{decoder['best_inversion']}**
- Inventory events: **{inventory['events']}**
- Meaningful sessions: **{inventory['meaningful_sessions']}**
- Error-only sessions: **{inventory['error_only_sessions']}**
- Talkgroup IDs: **{inventory['talkgroup_ids'] or 'none'}**
- Radio IDs: **{inventory['radio_ids'] or 'none'}**

Artifacts:

- `{attempt_dir}`
- `{root / 'inventory'}`
- `{inventory['database_path']}`
"""
    (root / "targeted_run.md").write_text(report, encoding="utf-8")
    return result


__all__ = ["load_capture_metadata", "run_targeted_capture"]
