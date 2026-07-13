from __future__ import annotations

import json
from pathlib import Path

from dmr_iq_surveyor.inventory.runner import (
    build_inventory,
    build_inventory_from_config,
)


def _write_decoder_tree(
    decodes: Path,
    *,
    include_ids_in_extraction: bool,
) -> None:
    attempt = decodes / "C0099" / "R0099" / "iq"
    decoder = attempt / "decoder"
    decoder.mkdir(parents=True)
    extraction: dict[str, object] = {
        "candidate_frequency_hz": 164_537_500.0,
        "iq_order": "IQ",
    }
    if include_ids_in_extraction:
        extraction["candidate_id"] = "C0099"
        extraction["recording_id"] = "R0099"
    (attempt / "extraction_report.json").write_text(
        json.dumps(extraction),
        encoding="utf-8",
    )
    (decoder / "dsd_fme_normal_stderr.log").write_text(
        "20:00:00 Sync: +DMR [slot1] slot2 | Color Code=08 | VC1\n"
        "TG: 1234 TG: 5678 SRC: 9 SRC: 10\n",
        encoding="utf-8",
    )
    (decoder / "decoder_report.json").write_text(
        json.dumps(
            {
                "status": "dmr_confirmed_clean",
                "best_inversion": "normal",
                "best_quality_score": 11.0,
                "attempts": [
                    {
                        "inversion": "normal",
                        "status": "dmr_confirmed_clean",
                        "evidence": {
                            "dominant_color_code": 8,
                            "valid_color_code_ratio": 1.0,
                            "error_ratio": 0.0,
                            "slot1_sync_count": 1,
                            "slot2_sync_count": 0,
                            "talkgroup_ids": [1234, 5678],
                            "radio_ids": [9, 10],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_directory_fallback_ids_and_multiple_identity_events(
    tmp_path: Path,
) -> None:
    decodes = tmp_path / "run" / "decodes"
    _write_decoder_tree(
        decodes,
        include_ids_in_extraction=False,
    )
    output = tmp_path / "inventory"
    manifest = build_inventory(decodes, output, run_id="fallback-run")
    attempts = json.loads(
        (output / "attempts.json").read_text(encoding="utf-8")
    )
    events = json.loads(
        (output / "events.json").read_text(encoding="utf-8")
    )
    assert manifest["events"] == 3
    assert attempts[0]["candidate_id"] == "C0099"
    assert attempts[0]["recording_id"] == "R0099"
    assert len({event["event_key"] for event in events}) == 3


def test_inventory_from_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    output_root = Path("runs/session-a")
    decodes = tmp_path / output_root / "decodes"
    _write_decoder_tree(
        decodes,
        include_ids_in_extraction=True,
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "project:\n"
        "  output_root: runs/session-a\n"
        "phase5:\n"
        "  run_id: session-a\n"
        "  max_gap_lines: 8\n"
        "  output_dir: runs/session-a/inventory\n"
        "  database_path: runs/inventory/dmr_inventory.sqlite3\n",
        encoding="utf-8",
    )
    result = build_inventory_from_config(config)
    assert result["run_id"] == "session-a"
    assert result["database_attempts"] == 1
    assert result["database_channels"] == 1
    assert Path(result["database_path"]).is_file()
