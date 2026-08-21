"""Spreadsheet export of stored survey results.

Produces three tables, because the interesting question is rarely about a
single run:

- `runs` -- one row per survey run: when, where, what was covered.
- `observations` -- one row per detection, carrying its run's context so the
  sheet stands alone without a join.
- `frequencies` -- one row per catalog frequency, aggregated across every
  run that saw it. This is the table that shows whether a signal is a
  one-off or a fixture: a frequency seen in several runs, at a consistent
  measured centre, with high persistence, is a very different thing from
  one that appeared once at the detection threshold.

CSV is written with a UTF-8 BOM so Excel opens it directly with correct
encoding rather than mangling non-ASCII site labels and notes.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any

_RUN_COLUMNS = (
    "survey_run_id",
    "site_id",
    "capture_start_utc",
    "capture_time_source",
    "band_profile",
    "center_frequency_hz",
    "sample_rate_hz",
    "usable_low_hz",
    "usable_high_hz",
    "coverage_status",
    "duration_seconds",
    "analyzed_seconds",
    "segment_count",
    "observation_count",
    "gps_latitude",
    "gps_longitude",
    "gps_source",
    "source_basename",
    "tool_version",
)

_OBSERVATION_COLUMNS = (
    "survey_run_id",
    "site_id",
    "capture_start_utc",
    "measured_center_hz",
    "measured_center_mhz",
    "nominal_frequency_hz",
    "raster_error_hz",
    "bandwidth_hz",
    "snr_db",
    "p95_snr_db",
    "persistence",
    "occupancy_pct",
    "segments_detected",
    "segments_analyzed",
    "spectral_class",
    "classification",
    "classification_method",
    "peak_dbfs_per_hz",
    "noise_floor_dbfs_per_hz",
    "power_unit",
    "edge_warning",
    "dc_warning",
)

_FREQUENCY_COLUMNS = (
    "nominal_frequency_hz",
    "nominal_frequency_mhz",
    "run_count",
    "site_count",
    "runs_seen_in",
    "sites_seen_in",
    "first_seen_at",
    "last_seen_at",
    "best_p95_snr_db",
    "max_persistence",
    "max_occupancy_pct",
    "mean_measured_center_hz",
    "measured_center_spread_hz",
)


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    # utf-8-sig: Excel needs the BOM to detect UTF-8 in a .csv.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def collect_runs(connection: sqlite3.Connection, *, site_id: str | None = None) -> list[dict[str, Any]]:
    clause = "WHERE r.site_id = ?" if site_id else ""
    parameters = (site_id,) if site_id else ()
    rows = connection.execute(
        f"""
        SELECT r.*, (
            SELECT COUNT(*) FROM rf_observations o WHERE o.survey_run_id = r.survey_run_id
        ) AS observation_count
        FROM survey_runs r
        {clause}
        ORDER BY COALESCE(r.capture_start_utc, r.imported_at) ASC
        """,
        parameters,
    ).fetchall()
    return [dict(row) for row in rows]


def collect_observations(
    connection: sqlite3.Connection, *, site_id: str | None = None
) -> list[dict[str, Any]]:
    clause = "WHERE r.site_id = ?" if site_id else ""
    parameters = (site_id,) if site_id else ()
    rows = connection.execute(
        f"""
        SELECT o.*, f.nominal_frequency_hz, r.site_id, r.capture_start_utc
        FROM rf_observations o
        JOIN rf_frequencies f ON f.rf_frequency_id = o.rf_frequency_id
        JOIN survey_runs r ON r.survey_run_id = o.survey_run_id
        {clause}
        ORDER BY r.capture_start_utc ASC, o.measured_center_hz ASC
        """,
        parameters,
    ).fetchall()
    observations = []
    for row in rows:
        record = dict(row)
        record["measured_center_mhz"] = record["measured_center_hz"] / 1e6
        observations.append(record)
    return observations


def collect_frequencies(
    connection: sqlite3.Connection, *, site_id: str | None = None
) -> list[dict[str, Any]]:
    """Aggregate every catalog frequency across the runs that observed it.

    `measured_center_spread_hz` is the peak-to-peak disagreement between
    runs on where the signal actually sits. A few hundred Hz across
    independent captures is strong evidence of a real, stable emitter; a
    spread of several kHz suggests the rows were merged by raster proximity
    rather than being the same signal.
    """
    clause = "WHERE r.site_id = ?" if site_id else ""
    parameters = (site_id,) if site_id else ()
    rows = connection.execute(
        f"""
        SELECT
            f.nominal_frequency_hz,
            f.first_seen_at,
            f.last_seen_at,
            COUNT(DISTINCT o.survey_run_id) AS run_count,
            COUNT(DISTINCT r.site_id) AS site_count,
            GROUP_CONCAT(DISTINCT o.survey_run_id) AS runs_seen_in,
            GROUP_CONCAT(DISTINCT r.site_id) AS sites_seen_in,
            MAX(o.p95_snr_db) AS best_p95_snr_db,
            MAX(o.persistence) AS max_persistence,
            MAX(o.occupancy_pct) AS max_occupancy_pct,
            AVG(o.measured_center_hz) AS mean_measured_center_hz,
            MAX(o.measured_center_hz) - MIN(o.measured_center_hz) AS measured_center_spread_hz
        FROM rf_frequencies f
        JOIN rf_observations o ON o.rf_frequency_id = f.rf_frequency_id
        JOIN survey_runs r ON r.survey_run_id = o.survey_run_id
        {clause}
        GROUP BY f.rf_frequency_id
        ORDER BY run_count DESC, best_p95_snr_db DESC
        """,
        parameters,
    ).fetchall()
    frequencies = []
    for row in rows:
        record = dict(row)
        record["nominal_frequency_mhz"] = record["nominal_frequency_hz"] / 1e6
        frequencies.append(record)
    return frequencies


def export_survey(
    connection: sqlite3.Connection,
    output_dir: str | Path,
    *,
    site_id: str | None = None,
    write_xlsx: bool = False,
) -> dict[str, Any]:
    """Write runs/observations/frequencies tables as CSV, and optionally as
    a single multi-sheet workbook."""
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    runs = collect_runs(connection, site_id=site_id)
    observations = collect_observations(connection, site_id=site_id)
    frequencies = collect_frequencies(connection, site_id=site_id)

    written = []
    for name, columns, rows in (
        ("runs", _RUN_COLUMNS, runs),
        ("observations", _OBSERVATION_COLUMNS, observations),
        ("frequencies", _FREQUENCY_COLUMNS, frequencies),
    ):
        path = destination / f"{name}.csv"
        _write_csv(path, columns, rows)
        written.append(str(path))

    xlsx_path = None
    if write_xlsx:
        xlsx_path = str(_write_xlsx(destination / "survey.xlsx", runs, observations, frequencies))
        written.append(xlsx_path)

    return {
        "output_dir": str(destination),
        "files": written,
        "xlsx_path": xlsx_path,
        "run_count": len(runs),
        "observation_count": len(observations),
        "frequency_count": len(frequencies),
    }


def _write_xlsx(
    path: Path,
    runs: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    frequencies: list[dict[str, Any]],
) -> Path:
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover - depends on an optional package
        raise RuntimeError(
            "writing .xlsx needs openpyxl, which this project does not install by default. "
            "Either `pip install openpyxl` or use the CSV files, which Excel opens directly."
        ) from exc

    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, columns, rows in (
        ("runs", _RUN_COLUMNS, runs),
        ("observations", _OBSERVATION_COLUMNS, observations),
        ("frequencies", _FREQUENCY_COLUMNS, frequencies),
    ):
        sheet = workbook.create_sheet(name)
        sheet.append(list(columns))
        for row in rows:
            sheet.append([row.get(column) for column in columns])
        sheet.freeze_panes = "A2"
    workbook.save(path)
    return path


__all__ = [
    "collect_frequencies",
    "collect_observations",
    "collect_runs",
    "export_survey",
]
