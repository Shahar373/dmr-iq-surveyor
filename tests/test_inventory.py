from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from dmr_iq_surveyor.inventory.parser import parse_log_lines
from dmr_iq_surveyor.inventory.runner import build_inventory
from dmr_iq_surveyor.inventory.sessions import correlate_sessions


def test_parser_extracts_active_slot_and_event() -> None:
    events = parse_log_lines(
        [
            (
                "19:59:06 Sync: +DMR [slot1] slot2 "
                "| Color Code=08 | CSBK"
            ),
            (
                "19:59:07 Sync: +DMR slot1 [slot2] "
                "| Color Code=08 | VC3"
            ),
            (
                "19:59:08 Sync: +DMR [slot1] slot2 "
                "| CACH/Burst FEC ERR"
            ),
        ]
    )
    assert events[0].slot == 1
    assert events[0].event_type == "control"
    assert events[0].color_code == 8
    assert events[1].slot == 2
    assert events[1].event_type == "voice"
    assert events[1].event_subtype == "vc3"
    assert events[2].event_type == "error"
    assert events[2].is_error is True


def test_parser_activity_and_identity() -> None:
    events = parse_log_lines(
        [
            "20:00:00 Activity Update TS1: Group Voice TS2: Idle",
            (
                "Talkgroup Voice Channel Grant - Target: 1234 "
                "- Source: 5678"
            ),
        ]
    )
    assert [
        (event.slot, event.event_type) for event in events[:2]
    ] == [(1, "voice"), (2, "idle")]
    identity = events[2]
    assert identity.talkgroup_id == 1234
    assert identity.radio_id == 5678


def test_sessions_attach_identity_and_split_on_idle() -> None:
    events = parse_log_lines(
        [
            (
                "20:00:00 Sync: +DMR [slot1] slot2 "
                "| Color Code=08 | VC1"
            ),
            (
                "20:00:00 Sync: +DMR [slot1] slot2 "
                "| Color Code=08 | VC2"
            ),
            "TG: 1234 SRC: 5678",
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
    sessions = correlate_sessions(events, max_gap_lines=3)
    assert len(sessions) == 2
    assert sessions[0].session_type == "voice"
    assert sessions[0].talkgroup_ids == [1234]
    assert sessions[0].radio_ids == [5678]
    assert sessions[1].session_type == "control"


def _write_attempt(
    root: Path,
    *,
    candidate: str,
    recording: str,
    frequency: float,
    cc: int,
    voice: bool,
) -> None:
    attempt_dir = root / candidate / recording / "iq"
    decoder_dir = attempt_dir / "decoder"
    decoder_dir.mkdir(parents=True)
    (attempt_dir / "extraction_report.json").write_text(
        json.dumps(
            {
                "candidate_id": candidate,
                "recording_id": recording,
                "candidate_frequency_hz": frequency,
                "iq_order": "IQ",
            }
        ),
        encoding="utf-8",
    )
    subtype = "VC1" if voice else "CSBK"
    stderr = (
        f"20:00:00 Sync: +DMR [slot1] slot2 "
        f"| Color Code={cc:02d} | {subtype}\n"
        f"20:00:01 Sync: +DMR slot1 [slot2] "
        f"| Color Code={cc:02d} | CSBK\n"
        "TG: 1234 SRC: 5678\n"
    )
    (decoder_dir / "dsd_fme_normal_stderr.log").write_text(
        stderr,
        encoding="utf-8",
    )
    (decoder_dir / "decoder_report.json").write_text(
        json.dumps(
            {
                "status": "dmr_confirmed_clean",
                "best_inversion": "normal",
                "best_quality_score": 11.5,
                "attempts": [
                    {
                        "inversion": "normal",
                        "status": "dmr_confirmed_clean",
                        "evidence": {
                            "dominant_color_code": cc,
                            "valid_color_code_ratio": 1.0,
                            "error_ratio": 0.0,
                            "slot1_sync_count": 1,
                            "slot2_sync_count": 1,
                            "talkgroup_ids": [1234],
                            "radio_ids": [5678],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_inventory_build_is_idempotent_and_persistent(
    tmp_path: Path,
) -> None:
    decodes = tmp_path / "run-a" / "decodes"
    _write_attempt(
        decodes,
        candidate="C0001",
        recording="R1",
        frequency=164_537_500.0,
        cc=8,
        voice=True,
    )
    output = tmp_path / "inventory"
    first = build_inventory(decodes, output, run_id="run-a")
    second = build_inventory(decodes, output, run_id="run-a")
    assert first["attempts_imported"] == 1
    assert second["database_attempts"] == 1
    assert second["database_channels"] == 1
    database = output / "dmr_inventory.sqlite3"
    with sqlite3.connect(database) as connection:
        attempts = connection.execute(
            "SELECT COUNT(*) FROM attempts"
        ).fetchone()[0]
        events = connection.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0]
        channel = connection.execute(
            "SELECT dominant_color_code, voice_event_count, "
            "talkgroup_ids_json FROM channels"
        ).fetchone()
    assert attempts == 1
    assert events == 3
    assert channel[0] == 8
    assert channel[1] == 1
    assert json.loads(channel[2]) == [1234]


def test_inventory_merges_multiple_runs(tmp_path: Path) -> None:
    output = tmp_path / "inventory"
    run_a = tmp_path / "run-a" / "decodes"
    run_b = tmp_path / "run-b" / "decodes"
    _write_attempt(
        run_a,
        candidate="C0001",
        recording="R1",
        frequency=164_537_500.0,
        cc=8,
        voice=False,
    )
    _write_attempt(
        run_b,
        candidate="C0001",
        recording="R2",
        frequency=164_537_500.0,
        cc=8,
        voice=True,
    )
    build_inventory(run_a, output, run_id="run-a")
    manifest = build_inventory(run_b, output, run_id="run-b")
    assert manifest["database_attempts"] == 2
    channels = json.loads(
        (output / "channels.json").read_text(encoding="utf-8")
    )
    assert channels[0]["attempt_count"] == 2
    assert channels[0]["color_code_consistency"] == 1.0
    assert channels[0]["voice_event_count"] == 1
