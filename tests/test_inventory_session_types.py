from __future__ import annotations

import json
from pathlib import Path

from dmr_iq_surveyor.inventory.parser import parse_log_lines
from dmr_iq_surveyor.inventory.runner import build_inventory
from dmr_iq_surveyor.inventory.sessions import correlate_sessions


def test_error_only_session_is_separate_from_meaningful_activity() -> None:
    events = parse_log_lines(
        [
            (
                "20:00:00 Sync: +DMR [slot1] slot2 "
                "| CACH/Burst FEC ERR"
            ),
            (
                "20:00:01 Sync: +DMR [slot1] slot2 "
                "| Color Code=08 | IDLE"
            ),
            (
                "20:00:02 Sync: +DMR [slot1] slot2 "
                "| Color Code=08 | CSBK"
            ),
            (
                "20:00:02 Sync: +DMR [slot1] slot2 "
                "| SLCO CRC ERR"
            ),
        ]
    )
    sessions = correlate_sessions(events, max_gap_lines=3)
    assert [item.session_type for item in sessions] == [
        "error_only",
        "control",
    ]
    assert sessions[0].error_count == sessions[0].event_count == 1
    assert sessions[1].error_count == 1
    assert sessions[1].event_count == 2


def _write_attempt(root: Path) -> None:
    attempt_dir = root / "C0001" / "R1" / "iq"
    decoder_dir = attempt_dir / "decoder"
    decoder_dir.mkdir(parents=True)
    (attempt_dir / "extraction_report.json").write_text(
        json.dumps(
            {
                "candidate_id": "C0001",
                "recording_id": "R1",
                "candidate_frequency_hz": 164_537_500.0,
                "iq_order": "IQ",
            }
        ),
        encoding="utf-8",
    )
    (decoder_dir / "dsd_fme_normal_stderr.log").write_text(
        "\n".join(
            [
                (
                    "20:00:00 Sync: +DMR [slot1] slot2 "
                    "| CACH/Burst FEC ERR"
                ),
                (
                    "20:00:01 Sync: +DMR [slot1] slot2 "
                    "| Color Code=08 | IDLE"
                ),
                (
                    "20:00:02 Sync: +DMR [slot1] slot2 "
                    "| Color Code=08 | CSBK"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (decoder_dir / "decoder_report.json").write_text(
        json.dumps(
            {
                "status": "dmr_confirmed_degraded",
                "best_inversion": "normal",
                "best_quality_score": 8.0,
                "attempts": [
                    {
                        "inversion": "normal",
                        "status": "dmr_confirmed_degraded",
                        "evidence": {
                            "dominant_color_code": 8,
                            "valid_color_code_ratio": 1.0,
                            "error_ratio": 0.5,
                            "slot1_sync_count": 3,
                            "slot2_sync_count": 0,
                            "talkgroup_ids": [],
                            "radio_ids": [],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_manifest_distinguishes_total_and_meaningful_sessions(
    tmp_path: Path,
) -> None:
    decodes = tmp_path / "run-a" / "decodes"
    _write_attempt(decodes)
    output = tmp_path / "inventory"
    manifest = build_inventory(decodes, output, run_id="run-a")
    assert manifest["sessions"] == 2
    assert manifest["meaningful_sessions"] == 1
    assert manifest["error_only_sessions"] == 1
    assert manifest["session_types"] == {
        "control": 1,
        "error_only": 1,
    }
    report = (output / "phase5_report.md").read_text(encoding="utf-8")
    assert "Meaningful voice/data/control/mixed sessions: **1**" in report
    assert "Error-only quality sessions: **1**" in report
