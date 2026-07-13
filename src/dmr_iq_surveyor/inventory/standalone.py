from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from dmr_iq_surveyor.decode.dsd import parse_dsd_fme_log
from dmr_iq_surveyor.inventory.runner import build_inventory


def import_standalone_log(
    log_path: str | Path,
    output_root: str | Path,
    *,
    frequency_hz: float,
    run_id: str,
    recording_id: str,
    database_path: str | Path | None = None,
    capture_metadata: dict[str, Any] | None = None,
    iq_order: str = "IQ",
) -> dict[str, Any]:
    source = Path(log_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    root = Path(output_root).expanduser().resolve()
    candidate_id = f"L{int(round(frequency_hz))}"
    attempt_dir = (
        root
        / "standalone_decodes"
        / candidate_id
        / recording_id
        / iq_order.lower()
    )
    decoder_dir = attempt_dir / "decoder"
    decoder_dir.mkdir(parents=True, exist_ok=True)
    destination_log = decoder_dir / "dsd_fme_normal_stderr.log"
    shutil.copyfile(source, destination_log)
    text = source.read_text(encoding="utf-8", errors="replace")
    evidence = parse_dsd_fme_log("", text)
    status = str(evidence["evidence_tier"])
    decoder_report = {
        "status": status,
        "best_inversion": "normal",
        "best_quality_score": evidence["quality_score"],
        "probe": {
            "available": True,
            "requested_binary": "standalone-log",
            "resolved_binary": None,
            "help_text": "",
            "help_sha256": None,
            "probe_error": None,
            "supports_null_output": False,
        },
        "attempts": [
            {
                "status": status,
                "inversion": "normal",
                "command": [],
                "return_code": None,
                "timed_out": False,
                "probe": {},
                "evidence": evidence,
            }
        ],
    }
    (decoder_dir / "decoder_report.json").write_text(
        json.dumps(decoder_report, indent=2),
        encoding="utf-8",
    )
    (attempt_dir / "extraction_report.json").write_text(
        json.dumps(
            {
                "tool": "dmr-iq-surveyor",
                "input_path": str(source),
                "output_dir": str(attempt_dir),
                "candidate_id": candidate_id,
                "recording_id": recording_id,
                "candidate_frequency_hz": float(frequency_hz),
                "iq_order": iq_order,
                "extraction_profile": "standalone-log",
                "capture_metadata": dict(capture_metadata or {}),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    inventory = build_inventory(
        root / "standalone_decodes",
        root / "inventory",
        database_path=database_path,
        run_id=run_id,
    )
    result = {
        "run_id": run_id,
        "recording_id": recording_id,
        "candidate_id": candidate_id,
        "frequency_hz": float(frequency_hz),
        "status": status,
        "attempt_dir": str(attempt_dir),
        "inventory": inventory,
    }
    (root / "standalone_import.json").write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )
    return result


__all__ = ["import_standalone_log"]
