from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from dmr_iq_surveyor.inventory.parser import parse_log_file
from dmr_iq_surveyor.inventory.sessions import correlate_sessions
from dmr_iq_surveyor.inventory.store import (
    connect_database,
    fetch_table,
    replace_run,
)


def _stable_key(*parts: object) -> str:
    value = "|".join(str(part) for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _best_attempt(
    decoder_report: dict[str, Any],
) -> dict[str, Any] | None:
    inversion = decoder_report.get("best_inversion")
    for attempt in decoder_report.get("attempts", []):
        if attempt.get("inversion") == inversion:
            return attempt
    return None


def _load_attempt(
    attempt_dir: Path,
    run_id: str,
    max_gap_lines: int,
) -> dict[str, Any] | None:
    extraction_path = attempt_dir / "extraction_report.json"
    decoder_dir = attempt_dir / "decoder"
    decoder_report_path = decoder_dir / "decoder_report.json"
    if not extraction_path.is_file() or not decoder_report_path.is_file():
        return None
    extraction = json.loads(
        extraction_path.read_text(encoding="utf-8")
    )
    decoder_report = json.loads(
        decoder_report_path.read_text(encoding="utf-8")
    )
    best = _best_attempt(decoder_report)
    if best is None:
        return None
    inversion = str(decoder_report.get("best_inversion") or "normal")
    log_path = decoder_dir / f"dsd_fme_{inversion}_stderr.log"
    if not log_path.is_file():
        log_path = decoder_dir / f"dsd_fme_{inversion}_stdout.log"
    parsed = parse_log_file(log_path) if log_path.is_file() else []
    sessions = correlate_sessions(parsed, max_gap_lines=max_gap_lines)
    candidate_id = str(
        extraction.get("candidate_id") or attempt_dir.parents[1].name
    )
    recording_id = str(
        extraction.get("recording_id") or attempt_dir.parents[0].name
    )
    iq_order = str(
        extraction.get("iq_order") or attempt_dir.name.upper()
    )
    frequency = float(extraction["candidate_frequency_hz"])
    attempt_key = _stable_key(
        run_id,
        candidate_id,
        recording_id,
        iq_order,
        frequency,
    )
    evidence = best.get("evidence", {})
    event_rows: list[dict[str, Any]] = []
    for event in parsed:
        row = event.to_dict()
        row["event_key"] = _stable_key(
            attempt_key,
            event.line_index,
            event.slot,
            event.event_type,
            event.event_subtype,
            event.talkgroup_id,
            event.radio_id,
            event.raw_line,
        )
        event_rows.append(row)
    session_rows: list[dict[str, Any]] = []
    for session in sessions:
        row = session.to_dict()
        row["session_key"] = _stable_key(
            attempt_key,
            session.session_index,
            session.start_line,
            session.end_line,
            session.slot,
        )
        session_rows.append(row)
    return {
        "attempt_key": attempt_key,
        "candidate_id": candidate_id,
        "recording_id": recording_id,
        "frequency_hz": frequency,
        "iq_order": iq_order,
        "best_inversion": inversion,
        "status": str(
            decoder_report.get("status")
            or best.get("status")
            or "unknown"
        ),
        "quality_score": decoder_report.get("best_quality_score"),
        "dominant_color_code": evidence.get("dominant_color_code"),
        "valid_color_code_ratio": evidence.get(
            "valid_color_code_ratio"
        ),
        "error_ratio": evidence.get("error_ratio"),
        "slot1_sync_count": int(
            evidence.get("slot1_sync_count", 0)
        ),
        "slot2_sync_count": int(
            evidence.get("slot2_sync_count", 0)
        ),
        "talkgroup_ids": list(evidence.get("talkgroup_ids", [])),
        "radio_ids": list(evidence.get("radio_ids", [])),
        "output_dir": str(attempt_dir.resolve()),
        "events": event_rows,
        "sessions": session_rows,
    }


def _normalize_json_columns(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key, value in list(item.items()):
            if key.endswith("_json") and isinstance(value, str):
                item[key[:-5]] = json.loads(value)
                del item[key]
        normalized.append(item)
    return normalized


def build_inventory(
    decodes_dir: str | Path,
    output_dir: str | Path,
    *,
    database_path: str | Path | None = None,
    run_id: str | None = None,
    max_gap_lines: int = 12,
) -> dict[str, Any]:
    if max_gap_lines < 1:
        raise ValueError("max_gap_lines must be positive")
    source = Path(decodes_dir).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    resolved_run_id = run_id or source.parent.name
    if not resolved_run_id.strip():
        raise ValueError("run_id must not be empty")
    database = (
        Path(database_path).expanduser().resolve()
        if database_path
        else destination / "dmr_inventory.sqlite3"
    )

    attempts: list[dict[str, Any]] = []
    for attempt_dir in sorted(source.glob("C*/**/iq")):
        attempt = _load_attempt(
            attempt_dir,
            resolved_run_id,
            max_gap_lines,
        )
        if attempt is not None:
            attempts.append(attempt)

    connection = connect_database(database)
    try:
        replace_run(
            connection,
            run_id=resolved_run_id,
            source_dir=str(source),
            attempts=attempts,
        )
        tables = {
            name: fetch_table(connection, name)
            for name in (
                "runs",
                "attempts",
                "events",
                "sessions",
                "channels",
            )
        }
    finally:
        connection.close()

    public_tables = {
        name: _normalize_json_columns(rows)
        for name, rows in tables.items()
    }
    for name in ("attempts", "events", "sessions", "channels"):
        _write_csv(destination / f"{name}.csv", public_tables[name])
        (destination / f"{name}.json").write_text(
            json.dumps(public_tables[name], indent=2),
            encoding="utf-8",
        )
    with (destination / "events.jsonl").open(
        "w", encoding="utf-8"
    ) as handle:
        for row in public_tables["events"]:
            handle.write(json.dumps(row) + "\n")

    channels = public_tables["channels"]
    attempts_public = public_tables["attempts"]
    sessions_public = public_tables["sessions"]
    voice_channels = [
        row for row in channels if row["voice_event_count"]
    ]
    talkgroup_ids = sorted(
        {
            value
            for row in channels
            for value in row.get("talkgroup_ids", [])
        }
    )
    radio_ids = sorted(
        {
            value
            for row in channels
            for value in row.get("radio_ids", [])
        }
    )
    report_rows = [
        "| Frequency MHz | CC | Attempts | Clean | Degraded | "
        "S1 | S2 | Voice | Data | TG IDs | Radio IDs |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in sorted(
        channels, key=lambda item: item["frequency_hz"]
    ):
        report_rows.append(
            "| {freq:.6f} | {cc} | {attempts} | {clean} | "
            "{degraded} | {s1} | {s2} | {voice} | {data} | "
            "{tg} | {rid} |".format(
                freq=row["frequency_hz"] / 1e6,
                cc=(
                    row["dominant_color_code"]
                    if row["dominant_color_code"] is not None
                    else "-"
                ),
                attempts=row["attempt_count"],
                clean=row["clean_attempts"],
                degraded=row["degraded_attempts"],
                s1=row["slot1_sync_count"],
                s2=row["slot2_sync_count"],
                voice=row["voice_event_count"],
                data=row["data_event_count"],
                tg=row.get("talkgroup_ids") or "-",
                rid=row.get("radio_ids") or "-",
            )
        )
    report = f"""# Phase 5 DMR inventory

- Imported run: **{resolved_run_id}**
- Attempts in database: **{len(attempts_public)}**
- Channels in database: **{len(channels)}**
- Parsed events: **{len(public_tables['events'])}**
- Correlated non-idle sessions: **{len(sessions_public)}**
- Channels with voice evidence: **{len(voice_channels)}**
- Talkgroup IDs observed: **{talkgroup_ids or 'none'}**
- Radio IDs observed: **{radio_ids or 'none'}**
- Database: `{database}`

Decoder clock values are stored as evidence but are not treated as original RF capture timestamps. Session durations are estimates only when the decoder clock is monotonic.

{chr(10).join(report_rows)}
"""
    (destination / "phase5_report.md").write_text(
        report,
        encoding="utf-8",
    )
    manifest = {
        "run_id": resolved_run_id,
        "source_dir": str(source),
        "output_dir": str(destination),
        "database_path": str(database),
        "attempts_imported": len(attempts),
        "database_attempts": len(attempts_public),
        "database_channels": len(channels),
        "events": len(public_tables["events"]),
        "sessions": len(sessions_public),
        "talkgroup_ids": talkgroup_ids,
        "radio_ids": radio_ids,
        "max_gap_lines": max_gap_lines,
    }
    (destination / "import_manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    return manifest


def build_inventory_from_config(
    config_path: str | Path,
) -> dict[str, Any]:
    source = Path(config_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    project = (
        raw.get("project")
        if isinstance(raw.get("project"), dict)
        else {}
    )
    phase5 = (
        raw.get("phase5")
        if isinstance(raw.get("phase5"), dict)
        else {}
    )
    output_root_value = project.get("output_root")
    if not output_root_value:
        raise ValueError(
            "project.output_root is required for inventory-batch"
        )
    output_root = Path(str(output_root_value)).expanduser().resolve()
    decodes_dir = Path(
        str(phase5.get("decodes_dir", output_root / "decodes"))
    ).expanduser().resolve()
    output_dir = Path(
        str(phase5.get("output_dir", output_root / "inventory"))
    ).expanduser().resolve()
    database_path = Path(
        str(
            phase5.get(
                "database_path",
                output_dir / "dmr_inventory.sqlite3",
            )
        )
    ).expanduser().resolve()
    run_id = str(phase5.get("run_id") or output_root.name)
    max_gap_lines = int(phase5.get("max_gap_lines", 12))
    if max_gap_lines < 1:
        raise ValueError("phase5.max_gap_lines must be positive")
    return build_inventory(
        decodes_dir,
        output_dir,
        database_path=database_path,
        run_id=run_id,
        max_gap_lines=max_gap_lines,
    )
