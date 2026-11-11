"""Fixed-frequency emitter targets (Phase 8, Stage 1).

Phase 7 attaches every measurement to a P25 site, which needs a WACN, a
System ID, an RFSS and a Site number. A fixed-frequency carrier has none of
those, and inventing them would put false facts into a table whose meaning
must not change. So an emitter is its own kind of target -- "whatever is on
the air on frequency f" -- with its own tables beside the P25 ones, and the
same estimator, contouring, run exclusions and drift checks behind it.

Discovery before reference still holds. `survey run` measures the band with
no target list in hand; an emitter is matched to observations that were
already stored, only when measurements are materialised here.

See `docs/phase8-fixed-emitter-geolocation.md`.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.geo.calibration import truth_check
from dmr_iq_surveyor.geo.commonmode import CommonModeSettings, estimate_offsets
from dmr_iq_surveyor.geo.commonmode import residuals_by_run as _residuals_by_run
from dmr_iq_surveyor.geo.commonmode import summarise as summarise_common_mode
from dmr_iq_surveyor.geo.contours import credible_regions, regions_to_geojson
from dmr_iq_surveyor.geo.measurements import (
    _CENSOR_SETTING,
    USABILITY_AMBIGUOUS,
    USABILITY_LEVEL_UNRELIABLE,
    USABILITY_NO_POSITION,
    USABILITY_NOT_COVERED,
    USABILITY_RECEIVER_ARTIFACT,
    USABILITY_RUN_EXCLUDED,
    USABILITY_USABLE,
    MeasurementSettings,
    _covered,
    _nearest_observation,
    _resolve_position,
)
from dmr_iq_surveyor.geo.model import SolveSettings
from dmr_iq_surveyor.geo.pipeline import (
    METHOD,
    _as_gain,
    _campaign_gain_readings,
    _campaign_noise_floors,
    _flag_gain_drift,
    _flag_noise_floor_shift,
    _median_noise_floor,
    _modal_gain,
    _to_geo_measurements,
)
from dmr_iq_surveyor.geo.solver import (
    FIT_NOT_FITTED,
    GEOLOCATION_MATURITY,
    SOURCE_MODEL,
    solve_site,
)
from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.inspection import write_json
from dmr_iq_surveyor.survey.pipeline import DEFAULT_DATABASE_PATH
from dmr_iq_surveyor.survey.scope import WHOLE_DATABASE, CampaignScope

EMISSION_CONTINUOUS = "continuous"
EMISSION_INTERMITTENT = "intermittent"
EMISSIONS = (EMISSION_CONTINUOUS, EMISSION_INTERMITTENT)

ATTRIBUTION_FREQUENCY_MATCH = "frequency_match"
ATTRIBUTION_AMBIGUOUS_NEIGHBOUR = "ambiguous_neighbour"

# An intermittent emitter that was not heard at a stop may simply not have
# been transmitting then. For a continuous carrier "looked and heard nothing"
# bounds the level from above; for this one it bounds nothing, so the row is
# kept with its reason and the solver never sees it.
USABILITY_ABSENCE_NOT_EVIDENCE = "absence_not_evidence"

STATUS_NO_MEASUREMENTS = "no_measurements"

DEFAULT_TOLERANCE_HZ = 6250.0

EMITTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS emitter_targets (
    emitter_id INTEGER PRIMARY KEY AUTOINCREMENT,
    emitter_key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    frequency_hz REAL NOT NULL,
    tolerance_hz REAL NOT NULL,
    emission TEXT NOT NULL DEFAULT 'continuous',
    truth_latitude REAL,
    truth_longitude REAL,
    truth_source TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emitter_measurements (
    emitter_measurement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_run_id TEXT NOT NULL REFERENCES survey_runs(survey_run_id) ON DELETE CASCADE,
    emitter_id INTEGER NOT NULL REFERENCES emitter_targets(emitter_id) ON DELETE CASCADE,
    frequency_hz REAL NOT NULL,
    latitude REAL,
    longitude REAL,
    position_source TEXT NOT NULL,
    position_accuracy_m REAL,
    detected INTEGER NOT NULL,
    level_db REAL,
    level_metric TEXT NOT NULL,
    censor_level_db REAL NOT NULL,
    power_unit TEXT NOT NULL,
    calibrated INTEGER NOT NULL,
    attribution TEXT NOT NULL,
    attribution_detail TEXT NOT NULL DEFAULT '',
    usability TEXT NOT NULL,
    exclusion_reason TEXT NOT NULL DEFAULT '',
    quality_flags_json TEXT NOT NULL DEFAULT '[]',
    measured_center_hz REAL,
    frequency_error_hz REAL,
    occupancy_pct REAL,
    persistence REAL,
    capture_start_utc TEXT,
    site_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(survey_run_id, emitter_id)
);
CREATE INDEX IF NOT EXISTS idx_emitter_measurements_emitter
    ON emitter_measurements(emitter_id);
CREATE TABLE IF NOT EXISTS emitter_solutions (
    emitter_solution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    solve_batch_id TEXT NOT NULL,
    emitter_id INTEGER NOT NULL REFERENCES emitter_targets(emitter_id) ON DELETE CASCADE,
    solved_at TEXT NOT NULL,
    method TEXT NOT NULL,
    source_model TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT NOT NULL DEFAULT '',
    detection_count INTEGER NOT NULL,
    non_detection_count INTEGER NOT NULL,
    excluded_count INTEGER NOT NULL,
    mode_latitude REAL,
    mode_longitude REAL,
    mean_latitude REAL,
    mean_longitude REAL,
    area_km2_50 REAL,
    area_km2_90 REAL,
    path_loss_exponent REAL,
    reference_level_db REAL,
    level_metric TEXT NOT NULL,
    residual_rms_db REAL,
    fit_status TEXT NOT NULL DEFAULT 'unknown',
    azimuth_span_deg REAL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    settings_json TEXT NOT NULL DEFAULT '{}',
    diagnostics_json TEXT NOT NULL DEFAULT '{}',
    residuals_json TEXT NOT NULL DEFAULT '[]',
    geojson TEXT NOT NULL DEFAULT '{}',
    input_run_ids_json TEXT NOT NULL DEFAULT '[]',
    truth_json TEXT NOT NULL DEFAULT '{}',
    tool_version TEXT NOT NULL,
    campaign_id TEXT,
    UNIQUE(solve_batch_id, emitter_id)
);
CREATE INDEX IF NOT EXISTS idx_emitter_solutions_emitter
    ON emitter_solutions(emitter_id, emitter_solution_id);
"""


class EmitterError(ValueError):
    """A declared emitter that cannot be stored as given."""


def _database(path: str | Path | None) -> Path:
    return Path(path).expanduser().resolve() if path else DEFAULT_DATABASE_PATH.resolve()


def connect_emitter_database(path: str | Path) -> sqlite3.Connection:
    """Open a database with the full Phase 7 schema plus the emitter tables."""
    connection = connect_geo_database(path)
    connection.executescript(EMITTER_SCHEMA)
    connection.commit()
    return connection


# --- registry ---------------------------------------------------------------


def upsert_emitter(
    connection: sqlite3.Connection,
    *,
    emitter_key: str,
    frequency_hz: float,
    label: str = "",
    tolerance_hz: float = DEFAULT_TOLERANCE_HZ,
    emission: str = EMISSION_CONTINUOUS,
    truth_latitude: float | None = None,
    truth_longitude: float | None = None,
    truth_source: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Declare an emitter, or replace the declaration stored under its key."""
    key = emitter_key.strip()
    if not key:
        raise EmitterError("an emitter needs a non-empty key")
    if not math.isfinite(frequency_hz) or frequency_hz <= 0:
        raise EmitterError(f"{key}: frequency must be a positive number of Hz")
    if not math.isfinite(tolerance_hz) or tolerance_hz < 0:
        raise EmitterError(f"{key}: tolerance must be a non-negative number of Hz")
    if emission not in EMISSIONS:
        raise EmitterError(f"{key}: emission must be one of {list(EMISSIONS)}, got {emission!r}")
    if (truth_latitude is None) != (truth_longitude is None):
        raise EmitterError(f"{key}: a known position needs both latitude and longitude")
    if truth_latitude is not None and truth_longitude is not None:
        if not -90.0 <= truth_latitude <= 90.0 or not -180.0 <= truth_longitude <= 180.0:
            raise EmitterError(f"{key}: known position is out of range")
        if not truth_source.strip():
            # A position with no provenance cannot be weighed against the
            # result it is supposed to check.
            raise EmitterError(f"{key}: a known position needs a --truth-source")
    now = datetime.now(UTC).isoformat()
    connection.execute(
        """
        INSERT INTO emitter_targets(
            emitter_key, label, frequency_hz, tolerance_hz, emission, truth_latitude,
            truth_longitude, truth_source, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(emitter_key) DO UPDATE SET
            label = excluded.label,
            frequency_hz = excluded.frequency_hz,
            tolerance_hz = excluded.tolerance_hz,
            emission = excluded.emission,
            truth_latitude = excluded.truth_latitude,
            truth_longitude = excluded.truth_longitude,
            truth_source = excluded.truth_source,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            key,
            label,
            float(frequency_hz),
            float(tolerance_hz),
            emission,
            truth_latitude,
            truth_longitude,
            truth_source.strip(),
            notes,
            now,
            now,
        ),
    )
    connection.commit()
    return get_emitter(connection, key)


def get_emitter(connection: sqlite3.Connection, emitter_key: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM emitter_targets WHERE emitter_key = ?", (emitter_key,)
    ).fetchone()
    if row is None:
        raise EmitterError(f"unknown emitter: {emitter_key}")
    return dict(row)


def list_emitters(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM emitter_targets ORDER BY frequency_hz, emitter_key"
        )
    ]


def remove_emitter(connection: sqlite3.Connection, emitter_key: str) -> None:
    emitter = get_emitter(connection, emitter_key)
    # Explicit rather than relying on ON DELETE CASCADE, which SQLite only
    # honours when foreign keys are switched on for the connection.
    connection.execute(
        "DELETE FROM emitter_measurements WHERE emitter_id = ?", (emitter["emitter_id"],)
    )
    connection.execute(
        "DELETE FROM emitter_solutions WHERE emitter_id = ?", (emitter["emitter_id"],)
    )
    connection.execute(
        "DELETE FROM emitter_targets WHERE emitter_id = ?", (emitter["emitter_id"],)
    )
    connection.commit()


def _neighbours(emitters: list[dict[str, Any]]) -> dict[int, list[str]]:
    """Other emitters close enough that one observation could be either."""
    found: dict[int, list[str]] = {int(emitter["emitter_id"]): [] for emitter in emitters}
    for emitter in emitters:
        for other in emitters:
            if other["emitter_id"] == emitter["emitter_id"]:
                continue
            reach = max(float(emitter["tolerance_hz"]), float(other["tolerance_hz"]))
            if abs(float(emitter["frequency_hz"]) - float(other["frequency_hz"])) <= reach:
                found[int(emitter["emitter_id"])].append(str(other["emitter_key"]))
    return found


# --- measurements -----------------------------------------------------------


def build_emitter_run_measurements(
    connection: sqlite3.Connection,
    survey_run_id: str,
    settings: MeasurementSettings | None = None,
) -> list[dict[str, Any]]:
    """One measurement row per declared emitter for one survey run.

    The Phase 7 usability rules apply unchanged; see
    `geo.measurements.build_run_measurements` for the reasoning behind each.
    The two differences are attribution (frequency proximity between
    declared emitters, not a registry of sites) and intermittent emitters,
    whose non-detections are recorded but are not evidence.
    """
    resolved = settings or MeasurementSettings()
    resolved.validate()

    run_row = connection.execute(
        "SELECT * FROM survey_runs WHERE survey_run_id = ?", (survey_run_id,)
    ).fetchone()
    if run_row is None:
        raise ValueError(f"Unknown survey run: {survey_run_id}")
    run = dict(run_row)
    site_row = connection.execute(
        "SELECT * FROM sites WHERE site_id = ?", (run["site_id"],)
    ).fetchone()
    site = dict(site_row) if site_row is not None else None
    run_excluded = connection.execute(
        "SELECT reason, scope FROM geo_run_exclusions WHERE survey_run_id = ?", (survey_run_id,)
    ).fetchone()
    excludes_detections = run_excluded is not None and str(run_excluded["scope"]) != (
        "non_detections"
    )

    latitude, longitude, position_source, accuracy = _resolve_position(run, site)
    detection_settings = json.loads(run["detection_settings_json"] or "{}")
    censor_level = float(detection_settings.get(_CENSOR_SETTING[resolved.level_metric], 0.0))
    observations = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM rf_observations WHERE survey_run_id = ?", (survey_run_id,)
        )
    ]
    emitters = list_emitters(connection)
    neighbours = _neighbours(emitters)

    base_flags: list[str] = []
    if site is not None and site.get("gain") is None:
        base_flags.append("not_gain_comparable")
    if run.get("coverage_status") == "partial":
        base_flags.append("partial_coverage")
    if run.get("capture_time_source") == "unknown":
        base_flags.append("capture_time_unknown")
    if position_source == "site_profile":
        base_flags.append("position_from_site_profile")

    rows: list[dict[str, Any]] = []
    for emitter in emitters:
        emitter_id = int(emitter["emitter_id"])
        frequency_hz = float(emitter["frequency_hz"])
        tolerance_hz = float(emitter["tolerance_hz"])
        flags = list(base_flags)
        close = neighbours[emitter_id]
        if close:
            attribution = ATTRIBUTION_AMBIGUOUS_NEIGHBOUR
            attribution_detail = (
                f"declared emitter(s) {', '.join(sorted(close))} lie within tolerance of "
                f"{frequency_hz / 1e6:.6f} MHz; one observation could belong to either"
            )
        else:
            attribution = ATTRIBUTION_FREQUENCY_MATCH
            attribution_detail = (
                "attributed by frequency: one transmitter per frequency is assumed in the "
                "analysed area"
            )

        observation = _nearest_observation(observations, frequency_hz, tolerance_hz)
        detected = observation is not None
        covered = _covered(run, frequency_hz, resolved.passband_guard_hz)

        if run_excluded is not None and (excludes_detections or not detected):
            usability = USABILITY_RUN_EXCLUDED
            exclusion = str(run_excluded["reason"])
        elif attribution == ATTRIBUTION_AMBIGUOUS_NEIGHBOUR:
            usability = USABILITY_AMBIGUOUS
            exclusion = "another declared emitter is within frequency tolerance"
        elif latitude is None:
            usability = USABILITY_NO_POSITION
            exclusion = "the survey run has no coordinates, so this cannot be placed on a map"
        elif not covered and not detected:
            usability = USABILITY_NOT_COVERED
            exclusion = (
                "outside the run's measured usable passband; nothing was looked at here, "
                "which is not the same as having looked and heard nothing"
            )
        elif not detected and emitter["emission"] == EMISSION_INTERMITTENT:
            usability = USABILITY_ABSENCE_NOT_EVIDENCE
            exclusion = (
                "declared intermittent: not hearing it here may only mean it was not "
                "transmitting during this stop, so the absence bounds nothing"
            )
        elif not covered and detected:
            usability = USABILITY_LEVEL_UNRELIABLE
            exclusion = (
                "detected outside the run's measured usable passband, where the receiver "
                "roll-off understates the level by an unknown amount"
            )
            flags.append("detected_outside_measured_passband")
        else:
            usability = USABILITY_USABLE
            exclusion = ""

        if run_excluded is not None and detected and not excludes_detections:
            flags.append("detection_from_excluded_run")
        if observation is not None:
            if observation.get("edge_warning"):
                flags.append("edge_warning")
            if observation.get("dc_warning"):
                flags.append("dc_warning")
            if usability == USABILITY_USABLE and observation.get("dc_warning"):
                usability = USABILITY_RECEIVER_ARTIFACT
                exclusion = (
                    "this detection sits on the receiver's own DC/LO artifact, whose level is a "
                    "property of the radio rather than of any transmitter"
                )

        rows.append(
            {
                "emitter_id": emitter_id,
                "emitter_key": emitter["emitter_key"],
                "frequency_hz": frequency_hz,
                "latitude": latitude,
                "longitude": longitude,
                "position_source": position_source,
                "position_accuracy_m": accuracy,
                "detected": detected,
                "level_db": (
                    float(observation[resolved.level_metric]) if observation is not None else None
                ),
                "level_metric": resolved.level_metric,
                "censor_level_db": censor_level,
                "power_unit": (
                    observation["power_unit"] if observation is not None else "dbfs_per_hz"
                ),
                "calibrated": bool(observation["calibrated"]) if observation is not None else False,
                "attribution": attribution,
                "attribution_detail": attribution_detail,
                "usability": usability,
                "exclusion_reason": exclusion,
                "quality_flags": flags,
                "measured_center_hz": (
                    float(observation["measured_center_hz"]) if observation is not None else None
                ),
                "frequency_error_hz": (
                    float(observation["measured_center_hz"]) - frequency_hz
                    if observation is not None
                    else None
                ),
                "occupancy_pct": (
                    float(observation["occupancy_pct"]) if observation is not None else None
                ),
                "persistence": (
                    float(observation["persistence"]) if observation is not None else None
                ),
                "capture_start_utc": run.get("capture_start_utc"),
                "site_id": run.get("site_id"),
            }
        )
    return rows


def write_emitter_run_measurements(
    connection: sqlite3.Connection, survey_run_id: str, rows: list[dict[str, Any]]
) -> int:
    connection.execute(
        "DELETE FROM emitter_measurements WHERE survey_run_id = ?", (survey_run_id,)
    )
    created = datetime.now(UTC).isoformat()
    for row in rows:
        connection.execute(
            """
            INSERT INTO emitter_measurements(
                survey_run_id, emitter_id, frequency_hz, latitude, longitude,
                position_source, position_accuracy_m, detected, level_db, level_metric,
                censor_level_db, power_unit, calibrated, attribution, attribution_detail,
                usability, exclusion_reason, quality_flags_json, measured_center_hz,
                frequency_error_hz, occupancy_pct, persistence, capture_start_utc,
                site_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                survey_run_id,
                row["emitter_id"],
                row["frequency_hz"],
                row["latitude"],
                row["longitude"],
                row["position_source"],
                row["position_accuracy_m"],
                int(row["detected"]),
                row["level_db"],
                row["level_metric"],
                row["censor_level_db"],
                row["power_unit"],
                int(row["calibrated"]),
                row["attribution"],
                row["attribution_detail"],
                row["usability"],
                row["exclusion_reason"],
                json.dumps(row["quality_flags"]),
                row["measured_center_hz"],
                row["frequency_error_hz"],
                row["occupancy_pct"],
                row["persistence"],
                row["capture_start_utc"],
                row["site_id"],
                created,
            ),
        )
    return len(rows)


def fetch_emitter_measurements(
    connection: sqlite3.Connection,
    emitter_id: int,
    *,
    usable_only: bool = True,
    scope: CampaignScope = WHOLE_DATABASE,
) -> list[dict[str, Any]]:
    predicate, parameters = scope.where("r")
    query = """
        SELECT m.*
        FROM emitter_measurements m
        JOIN survey_runs r ON r.survey_run_id = m.survey_run_id
        WHERE m.emitter_id = ?
    """
    if usable_only:
        query += " AND m.usability = 'usable'"
    if predicate:
        query += f" AND {predicate}"
    query += " ORDER BY COALESCE(m.capture_start_utc, m.created_at) ASC"
    return [dict(row) for row in connection.execute(query, (emitter_id, *parameters))]


def summarise_emitter_measurements(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_usability: dict[str, int] = {}
    for row in rows:
        by_usability[row["usability"]] = by_usability.get(row["usability"], 0) + 1
    usable = [row for row in rows if row["usability"] == USABILITY_USABLE]
    return {
        "total": len(rows),
        "usable": len(usable),
        "detections": sum(1 for row in usable if row["detected"]),
        "non_detections": sum(1 for row in usable if not row["detected"]),
        "by_usability": dict(sorted(by_usability.items())),
    }


def materialise_emitter_measurements(
    *,
    database_path: str | Path | None = None,
    run_ids: Sequence[str] | None = None,
    settings: MeasurementSettings | None = None,
    scope: CampaignScope = WHOLE_DATABASE,
) -> dict[str, Any]:
    """Rebuild emitter measurements for the given runs (every run in scope by default).

    Gain drift and noise-floor shifts are flagged against the campaign's
    reference, exactly as Phase 7's `materialise_within` does.
    """
    resolved = settings or MeasurementSettings()
    resolved.validate()
    connection = connect_emitter_database(_database(database_path))
    try:
        predicate, parameters = scope.where("survey_runs")
        every_run = [
            str(row["survey_run_id"])
            for row in connection.execute(
                "SELECT survey_run_id FROM survey_runs"
                + (f" WHERE {predicate}" if predicate else "")
                + " ORDER BY COALESCE(capture_start_utc, imported_at) ASC",
                parameters,
            )
        ]
        selected = scope.narrow(connection, run_ids)
        if selected is None:
            selected = every_run
        readings = _campaign_gain_readings(connection, every_run)
        gains = {run_id: _as_gain(reading.value) for run_id, reading in readings.items()}
        reference_gain = _modal_gain(gains)
        noise = _campaign_noise_floors(connection, every_run)
        reference_noise = _median_noise_floor(noise)
        per_run: list[dict[str, Any]] = []
        total: list[dict[str, Any]] = []
        for run_id in selected:
            rows = build_emitter_run_measurements(connection, run_id, resolved)
            _flag_gain_drift(rows, gains.get(run_id), reference_gain)
            _flag_noise_floor_shift(rows, noise.get(run_id), reference_noise)
            write_emitter_run_measurements(connection, run_id, rows)
            per_run.append({"survey_run_id": run_id, **summarise_emitter_measurements(rows)})
            total.extend(rows)
        connection.commit()
    finally:
        connection.close()
    return {
        "runs": per_run,
        "run_count": len(per_run),
        "summary": summarise_emitter_measurements(total),
        "settings": resolved.to_dict(),
        "scope": scope.label,
        "reference_gain": reference_gain,
        "reference_noise_floor_dbfs_per_hz": reference_noise,
    }


# --- solving ----------------------------------------------------------------


def _solve_emitter(
    *,
    emitter: dict[str, Any],
    usable: list[dict[str, Any]],
    excluded: int,
    settings: SolveSettings,
    level_metric: str,
    offsets: dict[str, float],
) -> tuple[dict[str, Any], Any]:
    row: dict[str, Any] = {
        "emitter_id": int(emitter["emitter_id"]),
        "emitter_key": emitter["emitter_key"],
        "label": emitter["label"],
        "frequency_hz": float(emitter["frequency_hz"]),
        "emission": emitter["emission"],
        "solved_at": datetime.now(UTC).isoformat(),
        "method": METHOD,
        "source_model": SOURCE_MODEL,
        "level_metric": level_metric,
        "excluded_count": excluded,
        "tool_version": __version__,
        "settings": settings.to_dict(),
        "input_run_ids": sorted({str(item["survey_run_id"]) for item in usable}),
        "warnings": [],
        "mode_latitude": None,
        "mode_longitude": None,
        "mean_latitude": None,
        "mean_longitude": None,
        "path_loss_exponent": None,
        "fit_status": FIT_NOT_FITTED,
        "reference_level_db": None,
        "residual_rms_db": None,
        "azimuth_span_deg": None,
        "area_km2_50": None,
        "area_km2_90": None,
        "detection_count": 0,
        "non_detection_count": 0,
        "residuals": [],
        "diagnostics": {},
        "truth": {},
    }
    if not usable:
        row.update(
            status=STATUS_NO_MEASUREMENTS,
            status_reason=(
                f"{excluded} measurement slot(s) exist for this emitter but none is usable "
                "(ambiguous frequency, no run position, excluded stop, outside every measured "
                "passband, or an intermittent emitter's absence)"
            ),
        )
        return row, None

    result = solve_site(_to_geo_measurements(usable, offsets), settings)
    row.update(
        status=result.status,
        status_reason=result.status_reason,
        detection_count=result.detection_count,
        non_detection_count=result.non_detection_count,
        mode_latitude=result.mode_latitude,
        mode_longitude=result.mode_longitude,
        mean_latitude=result.mean_latitude,
        mean_longitude=result.mean_longitude,
        path_loss_exponent=result.path_loss_exponent,
        fit_status=result.fit_status,
        reference_level_db=result.reference_level_db,
        residual_rms_db=result.residual_rms_db,
        azimuth_span_deg=result.azimuth_span_deg,
        warnings=list(result.warnings),
        diagnostics=result.diagnostics,
        residuals=result.residuals,
    )
    flags: dict[str, int] = {}
    for measurement in usable:
        for flag in json.loads(measurement["quality_flags_json"] or "[]"):
            flags[flag] = flags.get(flag, 0) + 1
    for flag, count in sorted(flags.items()):
        row["warnings"].append(
            f"{count} of {len(usable)} contributing measurement(s) carry {flag!r}"
        )
    if result.surface is not None:
        regions = credible_regions(result.surface, settings.credible_levels)
        by_level = {region["level"]: region for region in regions}
        row["area_km2_50"] = (by_level.get(0.5) or {}).get("area_km2")
        row["area_km2_90"] = (by_level.get(0.9) or {}).get("area_km2")
        row["geojson"] = regions_to_geojson(
            regions,
            {
                "emitter_key": emitter["emitter_key"],
                "status": result.status,
                "kind": "credible_region",
            },
        )
    # Truth is read only here, after the solve, and only to score it.
    if emitter.get("truth_latitude") is not None and emitter.get("truth_longitude") is not None:
        row["truth"] = {
            **truth_check(
                result,
                float(emitter["truth_latitude"]),
                float(emitter["truth_longitude"]),
                tuple(settings.credible_levels),
            ),
            "truth_source": emitter.get("truth_source", ""),
        }
    return row, result


def _store_emitter_solution(
    connection: sqlite3.Connection, *, solve_batch_id: str, row: dict[str, Any]
) -> None:
    connection.execute(
        "DELETE FROM emitter_solutions WHERE solve_batch_id = ? AND emitter_id = ?",
        (solve_batch_id, row["emitter_id"]),
    )
    connection.execute(
        """
        INSERT INTO emitter_solutions(
            solve_batch_id, emitter_id, solved_at, method, source_model, status, status_reason,
            detection_count, non_detection_count, excluded_count, mode_latitude, mode_longitude,
            mean_latitude, mean_longitude, area_km2_50, area_km2_90, path_loss_exponent,
            reference_level_db, level_metric, residual_rms_db, fit_status, azimuth_span_deg,
            warnings_json, settings_json, diagnostics_json, residuals_json, geojson,
            input_run_ids_json, truth_json, tool_version, campaign_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?)
        """,
        (
            solve_batch_id,
            row["emitter_id"],
            row["solved_at"],
            row["method"],
            row["source_model"],
            row["status"],
            row.get("status_reason", ""),
            row["detection_count"],
            row["non_detection_count"],
            row["excluded_count"],
            row["mode_latitude"],
            row["mode_longitude"],
            row["mean_latitude"],
            row["mean_longitude"],
            row["area_km2_50"],
            row["area_km2_90"],
            row["path_loss_exponent"],
            row["reference_level_db"],
            row["level_metric"],
            row["residual_rms_db"],
            row["fit_status"],
            row["azimuth_span_deg"],
            json.dumps(row["warnings"]),
            json.dumps(row["settings"]),
            json.dumps(row["diagnostics"]),
            json.dumps(row["residuals"]),
            json.dumps(row.get("geojson") or {}),
            json.dumps(row["input_run_ids"]),
            json.dumps(row["truth"]),
            row["tool_version"],
            row.get("campaign_id"),
        ),
    )


def solve_emitters(
    *,
    database_path: str | Path | None = None,
    output_root: str | Path | None = None,
    emitter_keys: Sequence[str] | None = None,
    solve_batch_id: str | None = None,
    settings: SolveSettings | None = None,
    measurement_settings: MeasurementSettings | None = None,
    common_mode: CommonModeSettings | None = None,
    scope: CampaignScope = WHOLE_DATABASE,
) -> dict[str, Any]:
    """Rebuild the scope's emitter measurements, then solve every declared emitter.

    The rebuild is pure SQL over stored observations and costs little, and it
    means a run excluded or re-imported since the last solve can never be read
    stale. The common-mode correction runs exactly as in Phase 7: a first pass
    per emitter, then a second pass for the emitters a shifted stop touched.
    """
    resolved = settings or SolveSettings()
    resolved.validate()
    common = common_mode or CommonModeSettings()
    common.validate()
    measurement = measurement_settings or MeasurementSettings()
    measurement.validate()
    stored_campaign_id = scope.stored_campaign_id()
    batch = solve_batch_id or "_".join(
        part
        for part in ("emitters", datetime.now(UTC).strftime("%Y%m%d_%H%M%S"), stored_campaign_id)
        if part
    )
    refreshed = materialise_emitter_measurements(
        database_path=database_path, settings=measurement, scope=scope
    )

    connection = connect_emitter_database(_database(database_path))
    try:
        emitters = list_emitters(connection)
        if emitter_keys is not None:
            wanted = set(emitter_keys)
            missing = sorted(wanted - {emitter["emitter_key"] for emitter in emitters})
            if missing:
                raise EmitterError(f"unknown emitter(s): {', '.join(missing)}")
            emitters = [emitter for emitter in emitters if emitter["emitter_key"] in wanted]

        usable_by: dict[str, list[dict[str, Any]]] = {}
        excluded_by: dict[str, int] = {}
        for emitter in emitters:
            emitter_id = int(emitter["emitter_id"])
            usable = fetch_emitter_measurements(connection, emitter_id, scope=scope)
            everything = fetch_emitter_measurements(
                connection, emitter_id, usable_only=False, scope=scope
            )
            usable_by[emitter["emitter_key"]] = usable
            excluded_by[emitter["emitter_key"]] = len(everything) - len(usable)

        def solve(emitter: dict[str, Any], offsets: dict[str, float]) -> dict[str, Any]:
            row, _ = _solve_emitter(
                emitter=emitter,
                usable=usable_by[emitter["emitter_key"]],
                excluded=excluded_by[emitter["emitter_key"]],
                settings=resolved,
                level_metric=measurement.level_metric,
                offsets=offsets,
            )
            return row

        rows = {emitter["emitter_key"]: solve(emitter, {}) for emitter in emitters}
        offsets = estimate_offsets(_residuals_by_run(list(rows.values())), common)
        applied = {
            run_id: offset.offset_db for run_id, offset in offsets.items() if offset.applied
        }
        if applied:
            for emitter in emitters:
                key = emitter["emitter_key"]
                if not any(str(item["survey_run_id"]) in applied for item in usable_by[key]):
                    continue
                row = solve(emitter, applied)
                row["warnings"].append(
                    "levels were corrected for a per-stop common-mode offset at "
                    + ", ".join(
                        f"{run_id} ({applied[run_id]:+.1f} dB)" for run_id in sorted(applied)
                    )
                )
                rows[key] = row

        ordered = [rows[emitter["emitter_key"]] for emitter in emitters]
        for row in ordered:
            row["campaign_id"] = stored_campaign_id
            _store_emitter_solution(connection, solve_batch_id=batch, row=row)
        connection.commit()
    finally:
        connection.close()

    report = {
        "tool": "dmr-iq-surveyor",
        "tool_version": __version__,
        "solve_batch_id": batch,
        "campaign_id": stored_campaign_id,
        "scope": scope.label,
        "method": METHOD,
        "source_model": SOURCE_MODEL,
        "geolocation_maturity": GEOLOCATION_MATURITY,
        "generated_at": datetime.now(UTC).isoformat(),
        "measurement_summary": refreshed["summary"],
        "settings": resolved.to_dict(),
        "common_mode": summarise_common_mode(offsets),
        "solutions": [
            {key: value for key, value in row.items() if key != "geojson"} for row in ordered
        ],
    }
    if output_root is not None:
        destination = Path(output_root).expanduser().resolve()
        (destination / "reports").mkdir(parents=True, exist_ok=True)
        write_json(destination / "reports" / f"emitters_{batch}.json", report)
        write_json(
            destination / "reports" / f"emitters_{batch}.geojson",
            {
                "type": "FeatureCollection",
                "features": [
                    feature
                    for row in ordered
                    for feature in (row.get("geojson") or {}).get("features", [])
                ],
            },
        )
        report["output_dir"] = str(destination)
    return report


def latest_emitter_solutions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """The most recent stored solution per emitter, by insertion order."""
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT s.*, t.emitter_key
            FROM emitter_solutions s
            JOIN emitter_targets t ON t.emitter_id = s.emitter_id
            WHERE s.emitter_solution_id IN (
                SELECT MAX(emitter_solution_id) FROM emitter_solutions GROUP BY emitter_id
            )
            ORDER BY t.frequency_hz, t.emitter_key
            """
        )
    ]


__all__ = [
    "ATTRIBUTION_AMBIGUOUS_NEIGHBOUR",
    "ATTRIBUTION_FREQUENCY_MATCH",
    "DEFAULT_TOLERANCE_HZ",
    "EMISSIONS",
    "EMISSION_CONTINUOUS",
    "EMISSION_INTERMITTENT",
    "STATUS_NO_MEASUREMENTS",
    "USABILITY_ABSENCE_NOT_EVIDENCE",
    "EmitterError",
    "build_emitter_run_measurements",
    "connect_emitter_database",
    "fetch_emitter_measurements",
    "get_emitter",
    "latest_emitter_solutions",
    "list_emitters",
    "materialise_emitter_measurements",
    "remove_emitter",
    "solve_emitters",
    "summarise_emitter_measurements",
    "upsert_emitter",
    "write_emitter_run_measurements",
]
