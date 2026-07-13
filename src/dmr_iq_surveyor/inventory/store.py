from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    source_dir TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    recording_id TEXT NOT NULL,
    frequency_hz REAL NOT NULL,
    iq_order TEXT NOT NULL,
    best_inversion TEXT,
    status TEXT NOT NULL,
    quality_score REAL,
    dominant_color_code INTEGER,
    valid_color_code_ratio REAL,
    error_ratio REAL,
    slot1_sync_count INTEGER NOT NULL DEFAULT 0,
    slot2_sync_count INTEGER NOT NULL DEFAULT 0,
    talkgroup_ids_json TEXT NOT NULL,
    radio_ids_json TEXT NOT NULL,
    capture_metadata_json TEXT NOT NULL DEFAULT '{}',
    output_dir TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_key TEXT PRIMARY KEY,
    attempt_key TEXT NOT NULL REFERENCES attempts(attempt_key) ON DELETE CASCADE,
    line_index INTEGER NOT NULL,
    decoder_clock TEXT,
    slot INTEGER,
    event_type TEXT NOT NULL,
    event_subtype TEXT,
    color_code INTEGER,
    talkgroup_id INTEGER,
    radio_id INTEGER,
    is_error INTEGER NOT NULL,
    raw_line TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT PRIMARY KEY,
    attempt_key TEXT NOT NULL REFERENCES attempts(attempt_key) ON DELETE CASCADE,
    session_index INTEGER NOT NULL,
    slot INTEGER,
    session_type TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_clock TEXT,
    end_clock TEXT,
    duration_seconds_estimate REAL,
    timing_confidence TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    dominant_color_code INTEGER,
    talkgroup_ids_json TEXT NOT NULL,
    radio_ids_json TEXT NOT NULL,
    event_subtypes_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    frequency_hz REAL PRIMARY KEY,
    dominant_color_code INTEGER,
    color_code_consistency REAL NOT NULL,
    attempt_count INTEGER NOT NULL,
    clean_attempts INTEGER NOT NULL,
    degraded_attempts INTEGER NOT NULL,
    sync_only_attempts INTEGER NOT NULL,
    slot1_sync_count INTEGER NOT NULL,
    slot2_sync_count INTEGER NOT NULL,
    voice_event_count INTEGER NOT NULL,
    data_event_count INTEGER NOT NULL,
    control_event_count INTEGER NOT NULL,
    error_event_count INTEGER NOT NULL,
    talkgroup_ids_json TEXT NOT NULL,
    radio_ids_json TEXT NOT NULL,
    first_run_id TEXT,
    last_run_id TEXT,
    best_quality_score REAL,
    worst_error_ratio REAL
);
"""


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    existing = {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if column not in existing:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
        )


def connect_database(path: str | Path) -> sqlite3.Connection:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(destination)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    _ensure_column(
        connection,
        "attempts",
        "capture_metadata_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    return connection


def replace_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    source_dir: str,
    attempts: Iterable[dict[str, Any]],
) -> None:
    connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
    connection.execute(
        "INSERT INTO runs(run_id, source_dir, imported_at) VALUES (?, ?, ?)",
        (run_id, source_dir, datetime.now(timezone.utc).isoformat()),
    )
    for attempt in attempts:
        connection.execute(
            """
            INSERT INTO attempts(
                attempt_key, run_id, candidate_id, recording_id, frequency_hz,
                iq_order, best_inversion, status, quality_score,
                dominant_color_code, valid_color_code_ratio, error_ratio,
                slot1_sync_count, slot2_sync_count, talkgroup_ids_json,
                radio_ids_json, capture_metadata_json, output_dir
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt["attempt_key"],
                run_id,
                attempt["candidate_id"],
                attempt["recording_id"],
                attempt["frequency_hz"],
                attempt["iq_order"],
                attempt.get("best_inversion"),
                attempt["status"],
                attempt.get("quality_score"),
                attempt.get("dominant_color_code"),
                attempt.get("valid_color_code_ratio"),
                attempt.get("error_ratio"),
                attempt.get("slot1_sync_count", 0),
                attempt.get("slot2_sync_count", 0),
                json.dumps(attempt.get("talkgroup_ids", [])),
                json.dumps(attempt.get("radio_ids", [])),
                json.dumps(attempt.get("capture_metadata", {})),
                attempt["output_dir"],
            ),
        )
        for event in attempt.get("events", []):
            connection.execute(
                """
                INSERT INTO events(
                    event_key, attempt_key, line_index, decoder_clock, slot,
                    event_type, event_subtype, color_code, talkgroup_id,
                    radio_id, is_error, raw_line
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_key"],
                    attempt["attempt_key"],
                    event["line_index"],
                    event.get("decoder_clock"),
                    event.get("slot"),
                    event["event_type"],
                    event.get("event_subtype"),
                    event.get("color_code"),
                    event.get("talkgroup_id"),
                    event.get("radio_id"),
                    int(bool(event.get("is_error"))),
                    event["raw_line"],
                ),
            )
        for session in attempt.get("sessions", []):
            connection.execute(
                """
                INSERT INTO sessions(
                    session_key, attempt_key, session_index, slot, session_type,
                    start_line, end_line, start_clock, end_clock,
                    duration_seconds_estimate, timing_confidence, event_count,
                    error_count, dominant_color_code, talkgroup_ids_json,
                    radio_ids_json, event_subtypes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["session_key"],
                    attempt["attempt_key"],
                    session["session_index"],
                    session.get("slot"),
                    session["session_type"],
                    session["start_line"],
                    session["end_line"],
                    session.get("start_clock"),
                    session.get("end_clock"),
                    session.get("duration_seconds_estimate"),
                    session["timing_confidence"],
                    session["event_count"],
                    session["error_count"],
                    session.get("dominant_color_code"),
                    json.dumps(session.get("talkgroup_ids", [])),
                    json.dumps(session.get("radio_ids", [])),
                    json.dumps(session.get("event_subtypes", [])),
                ),
            )
    rebuild_channels(connection)
    connection.commit()


def rebuild_channels(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM channels")
    frequencies = [
        row[0]
        for row in connection.execute("SELECT DISTINCT frequency_hz FROM attempts")
    ]
    for frequency in frequencies:
        attempts = list(
            connection.execute(
                "SELECT * FROM attempts WHERE frequency_hz = ?", (frequency,)
            )
        )
        attempt_keys = [row["attempt_key"] for row in attempts]
        if attempt_keys:
            placeholders = ",".join("?" for _ in attempt_keys)
            events = list(
                connection.execute(
                    f"SELECT * FROM events WHERE attempt_key IN ({placeholders})",
                    attempt_keys,
                )
            )
        else:
            events = []
        colors = [
            row["dominant_color_code"]
            for row in attempts
            if row["dominant_color_code"] is not None
        ]
        dominant = Counter(colors).most_common(1)[0][0] if colors else None
        consistency = (
            colors.count(dominant) / len(colors)
            if colors and dominant is not None
            else 0.0
        )
        talkgroups: set[int] = set()
        radio_ids: set[int] = set()
        for row in attempts:
            talkgroups.update(json.loads(row["talkgroup_ids_json"]))
            radio_ids.update(json.loads(row["radio_ids_json"]))
        talkgroups.update(
            row["talkgroup_id"]
            for row in events
            if row["talkgroup_id"] is not None
        )
        radio_ids.update(
            row["radio_id"] for row in events if row["radio_id"] is not None
        )
        statuses = Counter(row["status"] for row in attempts)
        run_ids = [row["run_id"] for row in attempts]
        qualities = [
            row["quality_score"]
            for row in attempts
            if row["quality_score"] is not None
        ]
        error_ratios = [
            row["error_ratio"]
            for row in attempts
            if row["error_ratio"] is not None
        ]
        event_types = Counter(row["event_type"] for row in events)
        connection.execute(
            """
            INSERT INTO channels(
                frequency_hz, dominant_color_code, color_code_consistency,
                attempt_count, clean_attempts, degraded_attempts,
                sync_only_attempts, slot1_sync_count, slot2_sync_count,
                voice_event_count, data_event_count, control_event_count,
                error_event_count, talkgroup_ids_json, radio_ids_json,
                first_run_id, last_run_id, best_quality_score, worst_error_ratio
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                frequency,
                dominant,
                consistency,
                len(attempts),
                statuses["dmr_confirmed_clean"],
                statuses["dmr_confirmed_degraded"],
                statuses["dmr_sync_only"],
                sum(row["slot1_sync_count"] for row in attempts),
                sum(row["slot2_sync_count"] for row in attempts),
                event_types["voice"],
                event_types["data"] + event_types["vendor_data"],
                event_types["control"] + event_types["network_state"],
                sum(row["is_error"] for row in events),
                json.dumps(sorted(talkgroups)),
                json.dumps(sorted(radio_ids)),
                min(run_ids) if run_ids else None,
                max(run_ids) if run_ids else None,
                max(qualities) if qualities else None,
                max(error_ratios) if error_ratios else None,
            ),
        )


def fetch_table(
    connection: sqlite3.Connection, table: str
) -> list[dict[str, Any]]:
    allowed = {"runs", "attempts", "events", "sessions", "channels"}
    if table not in allowed:
        raise ValueError(f"Unsupported table: {table}")
    return [
        dict(row) for row in connection.execute(f"SELECT * FROM {table}")
    ]
