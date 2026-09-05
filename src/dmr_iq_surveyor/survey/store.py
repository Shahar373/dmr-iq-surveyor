"""Phase 6A persistent survey database.

Extends the existing DMR inventory SQLite database (never altering or
dropping its tables) with protocol-agnostic survey tables: `sites`,
`survey_runs`, `rf_frequencies`, `rf_observations`, `run_comparisons`.

`rf_frequencies` is deliberately a frequency catalog only -- it carries no
protocol, system, site or role column, ever. The same RF frequency can
belong to different systems, sites and protocols at different times; that
must stay expressible. Everything protocol/system/site/role-shaped belongs
at the observation layer or below (added in later Phase 6 milestones).

Idempotency: re-importing a `survey_run_id` deletes that run's
`rf_observations` and `run_comparisons` rows and re-inserts them.
`rf_frequencies` rows are found-or-created (matched within a tolerance, not
by exact float equality, since the measured center drifts slightly run to
run) and their `first_seen_at`/`last_seen_at` are always recomputed from
surviving observations -- never incremented in place -- so a deleted run's
timestamps cannot linger. Runs whose capture time is unknown
(`capture_time_source='unknown'`) are excluded from that computation and
counted separately.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dmr_iq_surveyor.inventory.store import connect_database
from dmr_iq_surveyor.survey.discovery import RfObservation
from dmr_iq_surveyor.survey.profiles import BandProfile, SiteProfile

SURVEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    antenna TEXT,
    receiver TEXT,
    gain_mode TEXT,
    gain REAL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    lna_state INTEGER
);
CREATE TABLE IF NOT EXISTS survey_runs (
    survey_run_id TEXT PRIMARY KEY,
    site_id TEXT REFERENCES sites(site_id),
    band_profile TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT,
    source_basename TEXT NOT NULL,
    center_frequency_hz REAL NOT NULL,
    sample_rate_hz REAL NOT NULL,
    capture_start_utc TEXT,
    capture_time_source TEXT NOT NULL,
    requested_start_hz REAL NOT NULL,
    requested_stop_hz REAL NOT NULL,
    usable_low_hz REAL,
    usable_high_hz REAL,
    coverage_status TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    analyzed_seconds REAL NOT NULL,
    segment_count INTEGER NOT NULL,
    occupancy_threshold_db REAL NOT NULL,
    detection_settings_json TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}',
    imported_at TEXT NOT NULL,
    status TEXT NOT NULL,
    gps_latitude REAL,
    gps_longitude REAL,
    gps_altitude_m REAL,
    gps_accuracy_m REAL,
    gps_source TEXT NOT NULL DEFAULT 'unknown',
    gps_fetched_at_utc TEXT
);
CREATE TABLE IF NOT EXISTS rf_frequencies (
    rf_frequency_id INTEGER PRIMARY KEY AUTOINCREMENT,
    nominal_frequency_hz REAL NOT NULL UNIQUE,
    first_seen_at TEXT,
    last_seen_at TEXT,
    first_seen_run_id TEXT,
    last_seen_run_id TEXT,
    observation_count INTEGER NOT NULL DEFAULT 0,
    undated_observation_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rf_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_run_id TEXT NOT NULL REFERENCES survey_runs(survey_run_id) ON DELETE CASCADE,
    rf_frequency_id INTEGER NOT NULL REFERENCES rf_frequencies(rf_frequency_id),
    measured_center_hz REAL NOT NULL,
    bandwidth_hz REAL NOT NULL,
    peak_dbfs_per_hz REAL NOT NULL,
    average_dbfs_per_hz REAL NOT NULL,
    noise_floor_dbfs_per_hz REAL NOT NULL,
    power_unit TEXT NOT NULL,
    calibrated INTEGER NOT NULL,
    snr_db REAL NOT NULL,
    p95_snr_db REAL NOT NULL,
    peak_concentration_db REAL NOT NULL,
    occupancy_pct REAL NOT NULL,
    occupancy_threshold_db REAL NOT NULL,
    occupancy_sample_count INTEGER NOT NULL,
    persistence REAL NOT NULL,
    segments_detected INTEGER NOT NULL,
    segments_analyzed INTEGER NOT NULL,
    equivalent_width_hz REAL NOT NULL,
    spectral_fill REAL NOT NULL,
    symmetry REAL NOT NULL,
    nearest_raster_hz REAL NOT NULL,
    raster_spacing_hz REAL NOT NULL,
    raster_error_hz REAL NOT NULL,
    spectral_class TEXT NOT NULL,
    classification TEXT NOT NULL,
    classification_confidence REAL NOT NULL,
    classification_method TEXT NOT NULL,
    edge_warning INTEGER NOT NULL,
    dc_warning INTEGER NOT NULL,
    UNIQUE(survey_run_id, rf_frequency_id)
);
CREATE TABLE IF NOT EXISTS run_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    baseline_run_id TEXT NOT NULL,
    target_run_id TEXT NOT NULL,
    rf_frequency_id INTEGER,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    delta_json TEXT NOT NULL DEFAULT '{}',
    comparable INTEGER NOT NULL,
    not_comparable_reason TEXT,
    created_at TEXT NOT NULL
);
"""

_SURVEY_TABLES = (
    "sites",
    "survey_runs",
    "rf_frequencies",
    "rf_observations",
    "run_comparisons",
)


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def connect_survey_database(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the shared inventory database and ensure
    the survey tables exist. Never touches the pre-existing DMR tables."""
    connection = connect_database(path)
    connection.executescript(SURVEY_SCHEMA)
    # A pre-existing survey_runs table (created before GPS support was
    # added) needs these columns added in place -- CREATE TABLE IF NOT
    # EXISTS above is a no-op on an already-created table.
    for column, declaration in (
        ("gps_latitude", "REAL"),
        ("gps_longitude", "REAL"),
        ("gps_altitude_m", "REAL"),
        ("gps_accuracy_m", "REAL"),
        ("gps_source", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("gps_fetched_at_utc", "TEXT"),
    ):
        _ensure_column(connection, "survey_runs", column, declaration)
    # The LNA state is the other half of a fixed manual gain: IFGR alone does
    # not reproduce a receiver setting. It was applied to the radio but never
    # stored, so a campaign that changed it between stops could not be caught
    # by the gain check. NULL on rows written before this column existed means
    # exactly "not recorded", and readers say so rather than assuming a value.
    _ensure_column(connection, "sites", "lna_state", "INTEGER")
    connection.commit()
    return connection


def upsert_site(connection: sqlite3.Connection, site: SiteProfile) -> None:
    existing = connection.execute(
        "SELECT site_id FROM sites WHERE site_id = ?", (site.site_id,)
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO sites(
                site_id, label, latitude, longitude, antenna, receiver,
                gain_mode, gain, notes, created_at, lna_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                site.site_id,
                site.label,
                site.latitude,
                site.longitude,
                site.antenna,
                site.receiver,
                site.gain_mode,
                site.gain,
                site.notes,
                datetime.now(UTC).isoformat(),
                site.lna_state,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE sites SET
                label = ?, latitude = ?, longitude = ?, antenna = ?,
                receiver = ?, gain_mode = ?, gain = ?, notes = ?, lna_state = ?
            WHERE site_id = ?
            """,
            (
                site.label,
                site.latitude,
                site.longitude,
                site.antenna,
                site.receiver,
                site.gain_mode,
                site.gain,
                site.notes,
                site.lna_state,
                site.site_id,
            ),
        )


def _find_or_create_rf_frequency(
    connection: sqlite3.Connection,
    nominal_frequency_hz: float,
    tolerance_hz: float,
) -> int:
    """Match an existing catalog row within `tolerance_hz`, or create one.

    Exact float equality is deliberately not used: the same physical RF
    frequency measures with slightly different noise each run and can snap
    to an adjacent raster bin. Matching by nearest-within-tolerance keeps
    repeat observations of the same channel on one catalog row.
    """
    row = connection.execute(
        """
        SELECT rf_frequency_id, nominal_frequency_hz
        FROM rf_frequencies
        ORDER BY ABS(nominal_frequency_hz - ?) ASC
        LIMIT 1
        """,
        (nominal_frequency_hz,),
    ).fetchone()
    if row is not None and abs(row["nominal_frequency_hz"] - nominal_frequency_hz) <= tolerance_hz:
        return int(row["rf_frequency_id"])
    cursor = connection.execute(
        "INSERT INTO rf_frequencies(nominal_frequency_hz) VALUES (?)",
        (nominal_frequency_hz,),
    )
    return int(cursor.lastrowid)


def _delete_run(connection: sqlite3.Connection, survey_run_id: str) -> None:
    connection.execute(
        "DELETE FROM rf_observations WHERE survey_run_id = ?", (survey_run_id,)
    )
    connection.execute(
        "DELETE FROM run_comparisons WHERE baseline_run_id = ? OR target_run_id = ?",
        (survey_run_id, survey_run_id),
    )
    connection.execute("DELETE FROM survey_runs WHERE survey_run_id = ?", (survey_run_id,))


def _rebuild_rf_frequency_timestamps(connection: sqlite3.Connection, rf_frequency_id: int) -> None:
    rows = connection.execute(
        """
        SELECT o.survey_run_id, r.capture_start_utc, r.capture_time_source
        FROM rf_observations o
        JOIN survey_runs r ON r.survey_run_id = o.survey_run_id
        WHERE o.rf_frequency_id = ?
        """,
        (rf_frequency_id,),
    ).fetchall()
    dated = [row for row in rows if row["capture_time_source"] != "unknown" and row["capture_start_utc"]]
    undated_count = len(rows) - len(dated)
    if dated:
        first_row = min(dated, key=lambda row: row["capture_start_utc"])
        last_row = max(dated, key=lambda row: row["capture_start_utc"])
        connection.execute(
            """
            UPDATE rf_frequencies SET
                first_seen_at = ?, last_seen_at = ?,
                first_seen_run_id = ?, last_seen_run_id = ?,
                observation_count = ?, undated_observation_count = ?
            WHERE rf_frequency_id = ?
            """,
            (
                first_row["capture_start_utc"],
                last_row["capture_start_utc"],
                first_row["survey_run_id"],
                last_row["survey_run_id"],
                len(rows),
                undated_count,
                rf_frequency_id,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE rf_frequencies SET
                first_seen_at = NULL, last_seen_at = NULL,
                first_seen_run_id = NULL, last_seen_run_id = NULL,
                observation_count = ?, undated_observation_count = ?
            WHERE rf_frequency_id = ?
            """,
            (len(rows), undated_count, rf_frequency_id),
        )


@dataclass(slots=True)
class SurveyRunRecord:
    survey_run_id: str
    site_id: str
    band_profile: str
    source_path: str
    source_sha256: str | None
    center_frequency_hz: float
    sample_rate_hz: float
    capture_start_utc: str | None
    capture_time_source: str
    requested_start_hz: float
    requested_stop_hz: float
    usable_low_hz: float | None
    usable_high_hz: float | None
    coverage_status: str
    duration_seconds: float
    analyzed_seconds: float
    segment_count: int
    occupancy_threshold_db: float
    detection_settings: dict[str, Any]
    tool_version: str
    status: str = "ok"
    settings: dict[str, Any] | None = None
    gps_latitude: float | None = None
    gps_longitude: float | None = None
    gps_altitude_m: float | None = None
    gps_accuracy_m: float | None = None
    gps_source: str = "unknown"
    gps_fetched_at_utc: str | None = None


def import_survey_run(
    connection: sqlite3.Connection,
    *,
    run: SurveyRunRecord,
    observations: list[RfObservation],
    raster_tolerance_hz: float,
) -> dict[str, Any]:
    """Idempotently store one survey run and its observations.

    Re-importing the same `survey_run_id` replaces that run's observations;
    a different run ID accumulates alongside prior runs, exactly matching
    the existing DMR inventory's `replace_run` idempotency contract.
    """
    # Frequencies the *previous* version of this run touched must also have
    # their first/last-seen recomputed, even if the new observation set no
    # longer includes them (e.g. re-importing with zero observations) --
    # otherwise a deleted run's timestamps would linger on that catalog row.
    previously_touched_frequency_ids = {
        int(row["rf_frequency_id"])
        for row in connection.execute(
            "SELECT rf_frequency_id FROM rf_observations WHERE survey_run_id = ?",
            (run.survey_run_id,),
        )
    }
    _delete_run(connection, run.survey_run_id)
    connection.execute(
        """
        INSERT INTO survey_runs(
            survey_run_id, site_id, band_profile, source_path, source_sha256,
            source_basename, center_frequency_hz, sample_rate_hz,
            capture_start_utc, capture_time_source, requested_start_hz,
            requested_stop_hz, usable_low_hz, usable_high_hz, coverage_status,
            duration_seconds, analyzed_seconds, segment_count,
            occupancy_threshold_db, detection_settings_json, tool_version,
            settings_json, imported_at, status,
            gps_latitude, gps_longitude, gps_altitude_m, gps_accuracy_m,
            gps_source, gps_fetched_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run.survey_run_id,
            run.site_id,
            run.band_profile,
            run.source_path,
            run.source_sha256,
            Path(run.source_path).name,
            run.center_frequency_hz,
            run.sample_rate_hz,
            run.capture_start_utc,
            run.capture_time_source,
            run.requested_start_hz,
            run.requested_stop_hz,
            run.usable_low_hz,
            run.usable_high_hz,
            run.coverage_status,
            run.duration_seconds,
            run.analyzed_seconds,
            run.segment_count,
            run.occupancy_threshold_db,
            json.dumps(run.detection_settings, sort_keys=True),
            run.tool_version,
            json.dumps(run.settings or {}, sort_keys=True),
            datetime.now(UTC).isoformat(),
            run.status,
            run.gps_latitude,
            run.gps_longitude,
            run.gps_altitude_m,
            run.gps_accuracy_m,
            run.gps_source,
            run.gps_fetched_at_utc,
        ),
    )

    # rf_observations allows at most one row per (run, catalog frequency).
    # A busy passband can put two distinct observations -- already
    # deduplicated by discover_observations's own clustering -- within
    # raster_tolerance_hz of the same catalog row (dense real detections,
    # not one channel measured twice; reproduced with IFGR=25 in a home
    # environment: 23 observations from a single 15 s capture). Rather than
    # let the second insert crash the whole run on the UNIQUE constraint,
    # keep the stronger measurement per catalog frequency and record how
    # many collided so it is visible, not silently dropped.
    observations_by_frequency_id: dict[int, RfObservation] = {}
    collisions = 0
    for observation in observations:
        rf_frequency_id = _find_or_create_rf_frequency(
            connection, observation.nearest_raster_hz, raster_tolerance_hz
        )
        existing = observations_by_frequency_id.get(rf_frequency_id)
        if existing is not None:
            collisions += 1
            if observation.peak_dbfs_per_hz <= existing.peak_dbfs_per_hz:
                continue
        observations_by_frequency_id[rf_frequency_id] = observation

    touched_frequency_ids = set(observations_by_frequency_id)
    for rf_frequency_id, observation in observations_by_frequency_id.items():
        connection.execute(
            """
            INSERT INTO rf_observations(
                survey_run_id, rf_frequency_id, measured_center_hz, bandwidth_hz,
                peak_dbfs_per_hz, average_dbfs_per_hz, noise_floor_dbfs_per_hz,
                power_unit, calibrated, snr_db, p95_snr_db, peak_concentration_db,
                occupancy_pct, occupancy_threshold_db, occupancy_sample_count,
                persistence, segments_detected, segments_analyzed,
                equivalent_width_hz, spectral_fill, symmetry, nearest_raster_hz,
                raster_spacing_hz, raster_error_hz, spectral_class, classification,
                classification_confidence, classification_method, edge_warning,
                dc_warning
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                run.survey_run_id,
                rf_frequency_id,
                observation.measured_center_hz,
                observation.bandwidth_hz,
                observation.peak_dbfs_per_hz,
                observation.average_dbfs_per_hz,
                observation.noise_floor_dbfs_per_hz,
                observation.power_unit,
                int(observation.calibrated),
                observation.snr_db,
                observation.p95_snr_db,
                observation.peak_concentration_db,
                observation.occupancy_pct,
                observation.occupancy_threshold_db,
                observation.occupancy_sample_count,
                observation.persistence,
                observation.segments_detected,
                observation.segments_analyzed,
                observation.equivalent_width_hz,
                observation.spectral_fill,
                observation.symmetry,
                observation.nearest_raster_hz,
                observation.raster_spacing_hz,
                observation.raster_error_hz,
                observation.spectral_class,
                observation.classification,
                observation.classification_confidence,
                observation.classification_method,
                int(observation.edge_warning),
                int(observation.dc_warning),
            ),
        )

    for rf_frequency_id in touched_frequency_ids | previously_touched_frequency_ids:
        _rebuild_rf_frequency_timestamps(connection, rf_frequency_id)
    connection.commit()

    return {
        "survey_run_id": run.survey_run_id,
        "observations_imported": len(observations_by_frequency_id),
        "rf_frequencies_touched": len(touched_frequency_ids),
        "raster_collisions_merged": collisions,
    }


def delete_survey_run(connection: sqlite3.Connection, survey_run_id: str) -> dict[str, Any]:
    """Remove one survey run and everything derived from it.

    Additive to this module: nothing existing changes behaviour. The frequency
    catalog's first/last-seen columns are recomputed from what survives, the
    same rule `import_survey_run` follows, so a deleted run's timestamps cannot
    linger on a catalog row.
    """
    touched = {
        int(row["rf_frequency_id"])
        for row in connection.execute(
            "SELECT rf_frequency_id FROM rf_observations WHERE survey_run_id = ?",
            (survey_run_id,),
        )
    }
    existed = (
        connection.execute(
            "SELECT COUNT(*) AS n FROM survey_runs WHERE survey_run_id = ?", (survey_run_id,)
        ).fetchone()["n"]
        > 0
    )
    _delete_run(connection, survey_run_id)
    for rf_frequency_id in touched:
        _rebuild_rf_frequency_timestamps(connection, rf_frequency_id)
    connection.commit()
    return {
        "survey_run_id": survey_run_id,
        "existed": existed,
        "rf_frequencies_recomputed": len(touched),
    }


def fetch_survey_table(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table not in _SURVEY_TABLES:
        raise ValueError(f"Unsupported survey table: {table}")
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


def get_run(connection: sqlite3.Connection, survey_run_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM survey_runs WHERE survey_run_id = ?", (survey_run_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def get_run_observations(connection: sqlite3.Connection, survey_run_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT o.*, f.nominal_frequency_hz
        FROM rf_observations o
        JOIN rf_frequencies f ON f.rf_frequency_id = o.rf_frequency_id
        WHERE o.survey_run_id = ?
        ORDER BY o.measured_center_hz ASC
        """,
        (survey_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_runs(
    connection: sqlite3.Connection, *, site_id: str | None = None
) -> list[dict[str, Any]]:
    if site_id is not None:
        rows = connection.execute(
            """
            SELECT r.*, (
                SELECT COUNT(*) FROM rf_observations o WHERE o.survey_run_id = r.survey_run_id
            ) AS observation_count
            FROM survey_runs r
            WHERE r.site_id = ?
            ORDER BY COALESCE(r.capture_start_utc, r.imported_at) ASC
            """,
            (site_id,),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT r.*, (
                SELECT COUNT(*) FROM rf_observations o WHERE o.survey_run_id = r.survey_run_id
            ) AS observation_count
            FROM survey_runs r
            ORDER BY COALESCE(r.capture_start_utc, r.imported_at) ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def band_profile_settings_json(band_profile: BandProfile) -> dict[str, Any]:
    return band_profile.to_dict()


__all__ = [
    "SURVEY_SCHEMA",
    "SurveyRunRecord",
    "band_profile_settings_json",
    "connect_survey_database",
    "delete_survey_run",
    "fetch_survey_table",
    "get_run",
    "get_run_observations",
    "import_survey_run",
    "list_runs",
    "upsert_site",
]
