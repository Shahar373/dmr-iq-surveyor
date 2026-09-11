"""Persistent geolocation tables (Phase 7), added to the shared database.

`connect_geo_database()` opens the reference database (which opens the
survey database, which opens the DMR inventory database) and then creates
the geolocation tables if they are absent. Nothing pre-existing is altered,
so a Pi database built by any earlier phase upgrades in place with no user
action.

Idempotency follows the same contract as the rest of the project:
re-materialising a run's measurements replaces exactly that run's rows, and
re-solving under the same batch id replaces exactly that batch's rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dmr_iq_surveyor.reference.store import connect_reference_database
from dmr_iq_surveyor.survey.scope import WHOLE_DATABASE, CampaignScope

GEO_SCHEMA = """
CREATE TABLE IF NOT EXISTS geo_measurements (
    geo_measurement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_run_id TEXT NOT NULL REFERENCES survey_runs(survey_run_id) ON DELETE CASCADE,
    p25_site_id INTEGER NOT NULL REFERENCES p25_sites(p25_site_id) ON DELETE CASCADE,
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
    UNIQUE(survey_run_id, p25_site_id, frequency_hz)
);
CREATE INDEX IF NOT EXISTS idx_geo_measurements_site
    ON geo_measurements(p25_site_id);
CREATE TABLE IF NOT EXISTS geo_solutions (
    geo_solution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    solve_batch_id TEXT NOT NULL,
    p25_site_id INTEGER NOT NULL REFERENCES p25_sites(p25_site_id) ON DELETE CASCADE,
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
    tool_version TEXT NOT NULL,
    campaign_id TEXT,
    UNIQUE(solve_batch_id, p25_site_id)
);
CREATE INDEX IF NOT EXISTS idx_geo_solutions_site
    ON geo_solutions(p25_site_id, solved_at);
CREATE TABLE IF NOT EXISTS geo_plans (
    solve_batch_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    plan_json TEXT NOT NULL DEFAULT '{}',
    geojson TEXT NOT NULL DEFAULT '{}',
    campaign_id TEXT
);
CREATE TABLE IF NOT EXISTS geo_run_exclusions (
    survey_run_id TEXT PRIMARY KEY REFERENCES survey_runs(survey_run_id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'all'
);
"""


def connect_geo_database(path: str | Path) -> sqlite3.Connection:
    connection = connect_reference_database(path)
    connection.executescript(GEO_SCHEMA)
    # CREATE TABLE IF NOT EXISTS leaves an existing table exactly as it was,
    # so a column added later has to be applied separately. Additive, with a
    # default, so a database written by an earlier version upgrades in place
    # and keeps the behaviour it had.
    existing = {row[1] for row in connection.execute("PRAGMA table_info(geo_run_exclusions)")}
    if "scope" not in existing:
        connection.execute(
            "ALTER TABLE geo_run_exclusions ADD COLUMN scope TEXT NOT NULL DEFAULT 'all'"
        )
    solution_columns = {row[1] for row in connection.execute("PRAGMA table_info(geo_solutions)")}
    if "fit_status" not in solution_columns:
        # 'unknown', not 'identified': those batches were solved before the
        # check existed, so whether their fits were separable was never
        # established. Claiming they were would put a verdict on rows that
        # never faced the question.
        connection.execute(
            "ALTER TABLE geo_solutions ADD COLUMN fit_status TEXT NOT NULL DEFAULT 'unknown'"
        )
    # Which campaign a solve was scoped to. NULL means the solve read the
    # whole database, which is what every solve before this column did and
    # what an unscoped solve still does. It is deliberately NOT backfilled
    # from `input_run_ids_json`: a batch that happens to contain only one
    # campaign's runs was still solved against every run in the file, and
    # labelling it afterwards would claim a boundary that was never applied.
    if "campaign_id" not in solution_columns:
        connection.execute("ALTER TABLE geo_solutions ADD COLUMN campaign_id TEXT")
    plan_columns = {row[1] for row in connection.execute("PRAGMA table_info(geo_plans)")}
    if "campaign_id" not in plan_columns:
        connection.execute("ALTER TABLE geo_plans ADD COLUMN campaign_id TEXT")
    connection.commit()
    return connection


def store_plan(
    connection: sqlite3.Connection,
    *,
    solve_batch_id: str,
    plan: dict[str, Any],
    geojson: dict[str, Any],
    campaign_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO geo_plans(solve_batch_id, created_at, status, reason, "
        "plan_json, geojson, campaign_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            solve_batch_id,
            datetime.now(UTC).isoformat(),
            plan.get("status", "unknown"),
            plan.get("reason", ""),
            json.dumps(plan),
            json.dumps(geojson),
            campaign_id,
        ),
    )
    connection.commit()


def latest_plan(
    connection: sqlite3.Connection, *, scope: CampaignScope = WHOLE_DATABASE
) -> dict[str, Any] | None:
    """The most recent plan, by insertion order rather than by clock.

    Scoped to a campaign, this is the most recent plan *that campaign*
    produced. A plan written by an unscoped solve carries no campaign and is
    not offered as one campaign's next stop, because it was computed from
    every run in the file -- including rounds this one is meant to be
    separate from.

    The unassigned scope is the one case where a `NULL` plan *is* returned,
    and it is not the same claim. `geo_plans.campaign_id IS NULL` means "this
    solve read the whole database", not "this solve read the unassigned
    runs", so what comes back is the answer that was standing before
    campaigns existed -- which is exactly what a reader of the unassigned
    runs is asking for, and exactly what it must be told it is getting. The
    row carries `campaign_id`, and `FieldService.plan()` passes it on as
    `unscoped_solve` so the page can say so rather than implying the plan was
    drawn from the stops beside it.
    """
    predicate, parameters = scope.where("geo_plans")
    row = connection.execute(
        "SELECT * FROM geo_plans "
        + (f"WHERE {predicate} " if predicate else "")
        + "ORDER BY rowid DESC LIMIT 1",
        parameters,
    ).fetchone()
    return dict(row) if row is not None else None


EXCLUSION_SCOPE_ALL = "all"
EXCLUSION_SCOPE_NON_DETECTIONS = "non_detections"


def exclude_run(
    connection: sqlite3.Connection,
    survey_run_id: str,
    reason: str,
    scope: str = EXCLUSION_SCOPE_ALL,
) -> None:
    """Bar a survey run from contributing to geolocation, with a reason.

    A truncated capture is the case that matters: a signal that was there but
    was not recorded long enough to be detected becomes a *non-detection*,
    and a non-detection is evidence that pushes the site away from that stop.
    A short capture would therefore not merely lose a measurement -- it would
    manufacture a confident wrong one. The run stays in the database as
    evidence; it just stops counting.

    `scope` says how far that reasoning reaches, because two different things
    raise an exclusion and they do not mean the same:

    `non_detections`
        Capture integrity -- driver overflows, a capture that came up short.
        The recording has gaps, so silence may only mean the receiver was not
        listening at that moment. A gap cannot invent a signal, though, so
        what WAS heard stays evidence.

    `all`
        The operator's own judgement about the stop ("parked under a bridge").
        That is a claim about the whole receive path, so the levels are as
        suspect as the silences and nothing from the run counts.

    `all` is the default, and the value existing rows take on upgrade: a
    stored exclusion whose intent is no longer recoverable is treated as the
    stricter of the two.
    """
    if scope not in (EXCLUSION_SCOPE_ALL, EXCLUSION_SCOPE_NON_DETECTIONS):
        raise ValueError(f"unknown exclusion scope: {scope!r}")
    connection.execute(
        "INSERT OR REPLACE INTO geo_run_exclusions(survey_run_id, reason, created_at, scope) "
        "VALUES (?, ?, ?, ?)",
        (survey_run_id, reason, datetime.now(UTC).isoformat(), scope),
    )
    connection.commit()


def clear_run_exclusion(connection: sqlite3.Connection, survey_run_id: str) -> None:
    connection.execute(
        "DELETE FROM geo_run_exclusions WHERE survey_run_id = ?", (survey_run_id,)
    )
    connection.commit()


def run_exclusion(connection: sqlite3.Connection, survey_run_id: str) -> str | None:
    row = connection.execute(
        "SELECT reason FROM geo_run_exclusions WHERE survey_run_id = ?", (survey_run_id,)
    ).fetchone()
    return str(row["reason"]) if row is not None else None


def replace_run_measurements(
    connection: sqlite3.Connection, survey_run_id: str, rows: list[dict[str, Any]]
) -> int:
    """Replace every measurement derived from one survey run.

    Deleting first means re-running after a corrected reference import (a
    frequency added, an ambiguity resolved) leaves no stale rows behind --
    the same reason `inventory.replace_run` and `import_survey_run` work
    this way.
    """
    connection.execute("DELETE FROM geo_measurements WHERE survey_run_id = ?", (survey_run_id,))
    created = datetime.now(UTC).isoformat()
    for row in rows:
        connection.execute(
            """
            INSERT INTO geo_measurements(
                survey_run_id, p25_site_id, frequency_hz, latitude, longitude,
                position_source, position_accuracy_m, detected, level_db, level_metric,
                censor_level_db, power_unit, calibrated, attribution, attribution_detail,
                usability, exclusion_reason, quality_flags_json, measured_center_hz,
                frequency_error_hz, occupancy_pct, persistence, capture_start_utc,
                site_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                survey_run_id,
                row["p25_site_id"],
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
                row.get("attribution_detail", ""),
                row["usability"],
                row.get("exclusion_reason", ""),
                json.dumps(row.get("quality_flags", [])),
                row.get("measured_center_hz"),
                row.get("frequency_error_hz"),
                row.get("occupancy_pct"),
                row.get("persistence"),
                row.get("capture_start_utc"),
                row.get("site_id"),
                created,
            ),
        )
    connection.commit()
    return len(rows)


def fetch_site_measurements(
    connection: sqlite3.Connection,
    p25_site_id: int,
    *,
    usable_only: bool = True,
    scope: CampaignScope = WHOLE_DATABASE,
) -> list[dict[str, Any]]:
    # A measurement carries no campaign of its own; the run that produced it
    # does. The join is what makes `survey_runs.campaign_id` the one place
    # the fact lives, so a run reassigned there is reassigned everywhere.
    predicate, parameters = scope.where("r")
    query = """
        SELECT m.*, s.site_key
        FROM geo_measurements m
        JOIN p25_sites s ON s.p25_site_id = m.p25_site_id
        JOIN survey_runs r ON r.survey_run_id = m.survey_run_id
        WHERE m.p25_site_id = ?
    """
    if usable_only:
        query += " AND m.usability = 'usable'"
    if predicate:
        query += f" AND {predicate}"
    query += " ORDER BY COALESCE(m.capture_start_utc, m.created_at) ASC"
    return [
        dict(row) for row in connection.execute(query, (p25_site_id, *parameters))
    ]


def fetch_all_measurements(
    connection: sqlite3.Connection, *, scope: CampaignScope = WHOLE_DATABASE
) -> list[dict[str, Any]]:
    predicate, parameters = scope.where("r")
    query = """
        SELECT m.*, s.site_key
        FROM geo_measurements m
        JOIN p25_sites s ON s.p25_site_id = m.p25_site_id
        JOIN survey_runs r ON r.survey_run_id = m.survey_run_id
    """
    if predicate:
        query += f" WHERE {predicate}"
    query += " ORDER BY m.p25_site_id, COALESCE(m.capture_start_utc, m.created_at)"
    return [dict(row) for row in connection.execute(query, parameters)]


def store_solution(
    connection: sqlite3.Connection, *, solve_batch_id: str, row: dict[str, Any]
) -> None:
    connection.execute(
        "DELETE FROM geo_solutions WHERE solve_batch_id = ? AND p25_site_id = ?",
        (solve_batch_id, row["p25_site_id"]),
    )
    connection.execute(
        """
        INSERT INTO geo_solutions(
            solve_batch_id, p25_site_id, solved_at, method, source_model, status,
            status_reason, detection_count, non_detection_count, excluded_count,
            mode_latitude, mode_longitude, mean_latitude, mean_longitude,
            area_km2_50, area_km2_90, path_loss_exponent, reference_level_db,
            level_metric, residual_rms_db, fit_status, azimuth_span_deg, warnings_json,
            settings_json, diagnostics_json, residuals_json, geojson,
            input_run_ids_json, tool_version, campaign_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            solve_batch_id,
            row["p25_site_id"],
            row["solved_at"],
            row["method"],
            row["source_model"],
            row["status"],
            row.get("status_reason", ""),
            row["detection_count"],
            row["non_detection_count"],
            row["excluded_count"],
            row.get("mode_latitude"),
            row.get("mode_longitude"),
            row.get("mean_latitude"),
            row.get("mean_longitude"),
            row.get("area_km2_50"),
            row.get("area_km2_90"),
            row.get("path_loss_exponent"),
            row.get("reference_level_db"),
            row["level_metric"],
            row.get("residual_rms_db"),
            row.get("fit_status", "unknown"),
            row.get("azimuth_span_deg"),
            json.dumps(row.get("warnings", [])),
            json.dumps(row.get("settings", {}), sort_keys=True),
            json.dumps(row.get("diagnostics", {}), sort_keys=True),
            json.dumps(row.get("residuals", [])),
            json.dumps(row.get("geojson", {})),
            json.dumps(row.get("input_run_ids", [])),
            row["tool_version"],
            row.get("campaign_id"),
        ),
    )
    connection.commit()


def latest_solutions(
    connection: sqlite3.Connection, *, scope: CampaignScope = WHOLE_DATABASE
) -> list[dict[str, Any]]:
    """The most recent solution per site.

    History is kept deliberately -- watching a site's region shrink across
    solve batches is the point of accumulating sessions -- so "latest" is a
    query, not a destructive update.

    "Most recent" is by insertion order (`geo_solution_id`), never by
    `solved_at`. A Raspberry Pi has no real-time clock: it boots with a stale
    time and jumps when NTP arrives over the phone hotspot, so a solve run
    later in the day can carry an earlier timestamp than one run before it.
    Ranking on that string showed a superseded solution and, when two solves
    landed in the same second, returned the same site twice.
    """
    # Scoped, both the outer row and the "which is latest" subquery are
    # narrowed. Narrowing only the outer one would rank this campaign's
    # solutions against another campaign's and then find none of them
    # current, reporting a site as unsolved that this campaign had solved.
    outer_predicate, parameters = scope.where("g")
    condition = f" AND {outer_predicate}" if outer_predicate else ""
    inner_predicate, inner_parameters = scope.where("inner_solution")
    inner = f" AND {inner_predicate}" if inner_predicate else ""
    rows = connection.execute(
        f"""
        SELECT g.*, s.site_key, s.rfss, s.site, s.observation_status
        FROM geo_solutions g
        JOIN p25_sites s ON s.p25_site_id = g.p25_site_id
        WHERE g.geo_solution_id = (
            SELECT MAX(inner_solution.geo_solution_id)
            FROM geo_solutions inner_solution
            WHERE inner_solution.p25_site_id = g.p25_site_id{inner}
        ){condition}
        ORDER BY s.rfss, s.site
        """,
        (*inner_parameters, *parameters),
    ).fetchall()
    return [dict(row) for row in rows]


def latest_solutions_by_campaign(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Every campaign's own latest solution for every site it solved.

    `latest_solutions` answers "what is the current conclusion", and unscoped
    it has to pick one row per site -- the most recently inserted. That is the
    right answer for a file with one round in it, and the wrong shape for an
    overview of several: the newest round wins every site, and a round whose
    solve found nothing hides the rounds that found something. On a real file
    that meant the historical 868 analyses vanished from the overview behind a
    later campaign's `insufficient_evidence`.

    So this returns one row per (site, campaign) instead. Nothing is combined,
    averaged or re-solved -- these are the stored rows, grouped by the boundary
    each was computed under, which is the only honest way to show rounds that
    were never meant to be compared.
    """
    rows = connection.execute(
        """
        SELECT g.*, s.site_key, s.rfss, s.site, s.observation_status
        FROM geo_solutions g
        JOIN p25_sites s ON s.p25_site_id = g.p25_site_id
        WHERE g.geo_solution_id = (
            SELECT MAX(inner_solution.geo_solution_id)
            FROM geo_solutions inner_solution
            WHERE inner_solution.p25_site_id = g.p25_site_id
              AND inner_solution.campaign_id IS g.campaign_id
        )
        ORDER BY s.rfss, s.site, g.campaign_id IS NOT NULL, g.campaign_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def solution_history(
    connection: sqlite3.Connection,
    p25_site_id: int,
    *,
    scope: CampaignScope = WHOLE_DATABASE,
) -> list[dict[str, Any]]:
    predicate, parameters = scope.where("geo_solutions")
    condition = f" AND {predicate}" if predicate else ""
    return [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT solve_batch_id, solved_at, status, detection_count,
                   non_detection_count, area_km2_50, area_km2_90,
                   mode_latitude, mode_longitude
            FROM geo_solutions
            WHERE p25_site_id = ?{condition}
            ORDER BY geo_solution_id ASC
            """,
            (p25_site_id, *parameters),
        )
    ]


__all__ = [
    "GEO_SCHEMA",
    "clear_run_exclusion",
    "connect_geo_database",
    "exclude_run",
    "latest_plan",
    "fetch_all_measurements",
    "fetch_site_measurements",
    "latest_solutions",
    "latest_solutions_by_campaign",
    "replace_run_measurements",
    "run_exclusion",
    "solution_history",
    "store_plan",
    "store_solution",
]
