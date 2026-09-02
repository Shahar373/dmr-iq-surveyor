"""Phase 7 orchestration: reference import, measurement extraction, solving.

Every entry point here takes a database path and returns a plain dictionary,
so the CLI and the field web app drive exactly the same code rather than
each growing their own half-correct version.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.geo.contours import credible_regions, regions_to_geojson
from dmr_iq_surveyor.geo.measurements import (
    MeasurementSettings,
    build_run_measurements,
    summarise,
)
from dmr_iq_surveyor.geo.model import GeoMeasurement, SolveSettings
from dmr_iq_surveyor.geo.report import render_solution_markdown
from dmr_iq_surveyor.geo.solver import SOURCE_MODEL, solve_site
from dmr_iq_surveyor.geo.store import (
    connect_geo_database,
    fetch_all_measurements,
    fetch_site_measurements,
    latest_solutions,
    replace_run_measurements,
    store_solution,
)
from dmr_iq_surveyor.inspection import write_json
from dmr_iq_surveyor.reference.p25_sites import load_p25_site_csv
from dmr_iq_surveyor.reference.store import import_snapshot, list_sites
from dmr_iq_surveyor.survey.pipeline import DEFAULT_DATABASE_PATH

METHOD = "bayesian_grid_log_distance"

# Site-level outcomes that are not solver refusals but facts about the
# inputs. Kept distinct from the solver's own statuses so "we never had a
# frequency for this site" can never be read as "we looked and the data was
# too weak".
STATUS_FREQUENCY_UNKNOWN = "frequency_unknown"
STATUS_NO_MEASUREMENTS = "no_measurements"


def _database(path: str | Path | None) -> Path:
    return Path(path).expanduser().resolve() if path else DEFAULT_DATABASE_PATH.resolve()


def import_reference_sites(
    csv_path: str | Path,
    *,
    database_path: str | Path | None = None,
    snapshot_id: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Import an external P25 site snapshot into the registry."""
    source = Path(csv_path).expanduser().resolve()
    snapshot = load_p25_site_csv(source)
    resolved_id = snapshot_id or f"{source.stem}"
    connection = connect_geo_database(_database(database_path))
    try:
        summary = import_snapshot(
            connection,
            snapshot_id=resolved_id,
            snapshot=snapshot,
            source_path=str(source),
            notes=notes,
        )
    finally:
        connection.close()
    summary["source_path"] = str(source)
    return summary


def materialise_measurements(
    *,
    database_path: str | Path | None = None,
    run_ids: Sequence[str] | None = None,
    settings: MeasurementSettings | None = None,
) -> dict[str, Any]:
    """Rebuild geolocation measurements for the given runs (all, by default).

    Rebuilding rather than appending is deliberate: a corrected reference
    import can turn an ambiguous frequency into an attributable one, and
    stale rows would keep the old verdict alive.
    """
    resolved = settings or MeasurementSettings()
    resolved.validate()
    connection = connect_geo_database(_database(database_path))
    try:
        if run_ids is None:
            run_ids = [
                str(row["survey_run_id"])
                for row in connection.execute(
                    "SELECT survey_run_id FROM survey_runs ORDER BY "
                    "COALESCE(capture_start_utc, imported_at) ASC"
                )
            ]
        gains = _campaign_gains(connection, run_ids)
        reference_gain = _modal_gain(gains)
        per_run: list[dict[str, Any]] = []
        total: list[dict[str, Any]] = []
        for run_id in run_ids:
            rows = build_run_measurements(connection, run_id, resolved)
            _flag_gain_drift(rows, gains.get(run_id), reference_gain)
            replace_run_measurements(connection, run_id, rows)
            per_run.append({"survey_run_id": run_id, **summarise(rows)})
            total.extend(rows)
    finally:
        connection.close()
    drifted = sorted(
        run_id
        for run_id, gain in gains.items()
        if reference_gain is not None and gain is not None and gain != reference_gain
    )
    return {
        "runs": per_run,
        "run_count": len(per_run),
        "summary": summarise(total),
        "settings": resolved.to_dict(),
        "reference_gain": reference_gain,
        "gain_drift_runs": drifted,
    }


def _campaign_gains(connection: Any, run_ids: Sequence[str]) -> dict[str, float | None]:
    rows = connection.execute(
        """
        SELECT r.survey_run_id, s.gain
        FROM survey_runs r LEFT JOIN sites s ON s.site_id = r.site_id
        """
    ).fetchall()
    wanted = set(run_ids)
    return {
        str(row["survey_run_id"]): (None if row["gain"] is None else float(row["gain"]))
        for row in rows
        if str(row["survey_run_id"]) in wanted
    }


def _modal_gain(gains: dict[str, float | None]) -> float | None:
    """The gain most of the campaign was recorded at.

    Levels recorded at different receiver gain are not on one scale, and the
    whole method is a comparison of levels between places. A stop recorded at
    a different gain is not a slightly worse measurement -- it is a wrong one,
    and it has to be visible rather than averaged in.
    """
    counts: dict[float, int] = {}
    for gain in gains.values():
        if gain is None:
            continue
        counts[gain] = counts.get(gain, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: (item[1], -item[0]))[0]


def _flag_gain_drift(
    rows: list[dict[str, Any]], gain: float | None, reference_gain: float | None
) -> None:
    if reference_gain is None:
        return
    if gain is None:
        for row in rows:
            row["quality_flags"] = [*row["quality_flags"], "gain_not_recorded"]
        return
    if gain == reference_gain:
        return
    for row in rows:
        row["quality_flags"] = [*row["quality_flags"], f"gain_differs_from_campaign:{gain:g}"]


def _to_geo_measurements(rows: list[dict[str, Any]]) -> list[GeoMeasurement]:
    return [
        GeoMeasurement(
            label=f"{row['survey_run_id']}@{row['frequency_hz'] / 1e6:.6f}MHz",
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            detected=bool(row["detected"]),
            level_db=None if row["level_db"] is None else float(row["level_db"]),
            censor_level_db=float(row["censor_level_db"]),
            survey_run_id=str(row["survey_run_id"]),
            frequency_hz=float(row["frequency_hz"]),
        )
        for row in rows
    ]


def solve_all_sites(
    *,
    database_path: str | Path | None = None,
    output_root: str | Path | None = None,
    site_keys: Sequence[str] | None = None,
    solve_batch_id: str | None = None,
    settings: SolveSettings | None = None,
    level_metric: str = "snr_db",
    on_progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Solve every registry site that has usable measurements.

    Sites without a control-channel frequency, and sites whose measurements
    are all excluded, are recorded with an explicit status rather than
    quietly omitted -- otherwise a map would silently show fewer sites than
    the system actually has.
    """
    resolved = settings or SolveSettings()
    resolved.validate()
    batch = solve_batch_id or datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    connection = connect_geo_database(_database(database_path))
    solutions: list[dict[str, Any]] = []
    try:
        sites = list_sites(connection)
        if site_keys is not None:
            wanted = set(site_keys)
            sites = [site for site in sites if site["site_key"] in wanted]
        for index, site in enumerate(sites):
            if on_progress is not None:
                on_progress(str(site["site_key"]), index, len(sites))
            site_id = int(site["p25_site_id"])
            usable = fetch_site_measurements(connection, site_id, usable_only=True)
            everything = fetch_site_measurements(connection, site_id, usable_only=False)
            excluded = len(everything) - len(usable)
            solved_at = datetime.now(UTC).isoformat()
            row: dict[str, Any] = {
                "p25_site_id": site_id,
                "site_key": site["site_key"],
                "rfss": site["rfss"],
                "site": site["site"],
                "solved_at": solved_at,
                "method": METHOD,
                "source_model": SOURCE_MODEL,
                "level_metric": level_metric,
                "excluded_count": excluded,
                "tool_version": __version__,
                "settings": resolved.to_dict(),
                "input_run_ids": sorted({str(item["survey_run_id"]) for item in usable}),
            }

            if not site["channels"]:
                row.update(
                    status=STATUS_FREQUENCY_UNKNOWN,
                    status_reason=(
                        "no control-channel frequency is on record for this site, so nothing "
                        "could be measured; it stays listed rather than being dropped"
                    ),
                    detection_count=0,
                    non_detection_count=0,
                )
            elif not usable:
                row.update(
                    status=STATUS_NO_MEASUREMENTS,
                    status_reason=(
                        f"{excluded} measurement slot(s) exist for this site but none is usable "
                        "(ambiguous frequency, no run position, or outside every measured passband)"
                    ),
                    detection_count=0,
                    non_detection_count=0,
                )
            else:
                result = solve_site(_to_geo_measurements(usable), resolved)
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
                    reference_level_db=result.reference_level_db,
                    residual_rms_db=result.residual_rms_db,
                    azimuth_span_deg=result.azimuth_span_deg,
                    warnings=result.warnings,
                    diagnostics=result.diagnostics,
                    residuals=result.residuals,
                )
                if result.surface is not None:
                    regions = credible_regions(result.surface, resolved.credible_levels)
                    by_level = {region["level"]: region for region in regions}
                    row["area_km2_50"] = (by_level.get(0.5) or {}).get("area_km2")
                    row["area_km2_90"] = (by_level.get(0.9) or {}).get("area_km2")
                    row["geojson"] = regions_to_geojson(
                        regions,
                        {
                            "site_key": site["site_key"],
                            "status": result.status,
                            "kind": "credible_region",
                        },
                    )
            store_solution(connection, solve_batch_id=batch, row=row)
            solutions.append(row)

        measurement_summary = summarise(fetch_all_measurements(connection))
    finally:
        connection.close()

    report = {
        "tool": "dmr-iq-surveyor",
        "tool_version": __version__,
        "solve_batch_id": batch,
        "method": METHOD,
        "source_model": SOURCE_MODEL,
        "generated_at": datetime.now(UTC).isoformat(),
        "measurement_summary": measurement_summary,
        "settings": resolved.to_dict(),
        "solutions": [
            {key: value for key, value in solution.items() if key != "geojson"}
            for solution in solutions
        ],
    }
    if output_root is not None:
        destination = Path(output_root).expanduser().resolve()
        (destination / "reports").mkdir(parents=True, exist_ok=True)
        write_json(destination / "reports" / f"geolocation_{batch}.json", report)
        (destination / "reports" / f"geolocation_{batch}.md").write_text(
            render_solution_markdown(
                solve_batch_id=batch,
                solutions=solutions,
                measurement_summary=measurement_summary,
                settings=resolved.to_dict(),
            ),
            encoding="utf-8",
        )
        write_json(
            destination / "reports" / f"geolocation_{batch}.geojson",
            build_map_geojson(database_path=database_path),
        )
        report["output_dir"] = str(destination)
    return report


def build_map_geojson(*, database_path: str | Path | None = None) -> dict[str, Any]:
    """One FeatureCollection carrying everything the map needs.

    Measurement points, solved modes and credible regions travel together so
    a region can never be displayed without the evidence that produced it.
    """
    connection = connect_geo_database(_database(database_path))
    try:
        measurements = fetch_all_measurements(connection)
        solutions = latest_solutions(connection)
    finally:
        connection.close()

    features: list[dict[str, Any]] = []
    for row in measurements:
        if row["latitude"] is None or row["longitude"] is None:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "kind": "measurement",
                    "site_key": row["site_key"],
                    "survey_run_id": row["survey_run_id"],
                    "frequency_hz": row["frequency_hz"],
                    "detected": bool(row["detected"]),
                    "level_db": row["level_db"],
                    "level_metric": row["level_metric"],
                    "censor_level_db": row["censor_level_db"],
                    "usability": row["usability"],
                    "attribution": row["attribution"],
                    "quality_flags": json.loads(row["quality_flags_json"] or "[]"),
                    "capture_start_utc": row["capture_start_utc"],
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(row["longitude"]), float(row["latitude"])],
                },
            }
        )

    for solution in solutions:
        geojson = json.loads(solution["geojson"] or "{}")
        for feature in geojson.get("features", []):
            features.append(feature)
        if solution["mode_latitude"] is not None and solution["mode_longitude"] is not None:
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "kind": "estimate",
                        "site_key": solution["site_key"],
                        "status": solution["status"],
                        "status_reason": solution["status_reason"],
                        "detection_count": solution["detection_count"],
                        "non_detection_count": solution["non_detection_count"],
                        "area_km2_50": solution["area_km2_50"],
                        "area_km2_90": solution["area_km2_90"],
                        "path_loss_exponent": solution["path_loss_exponent"],
                        "azimuth_span_deg": solution["azimuth_span_deg"],
                        "warnings": json.loads(solution["warnings_json"] or "[]"),
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [
                            float(solution["mode_longitude"]),
                            float(solution["mode_latitude"]),
                        ],
                    },
                }
            )
    return {"type": "FeatureCollection", "features": features}


def site_overview(*, database_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Registry sites joined with their measurement counts and latest status."""
    connection = connect_geo_database(_database(database_path))
    try:
        sites = list_sites(connection)
        solutions = {row["p25_site_id"]: row for row in latest_solutions(connection)}
        counts = {
            int(row["p25_site_id"]): dict(row)
            for row in connection.execute(
                """
                SELECT p25_site_id,
                       COUNT(*) AS total,
                       SUM(CASE WHEN usability = 'usable' AND detected = 1 THEN 1 ELSE 0 END)
                           AS detections,
                       SUM(CASE WHEN usability = 'usable' AND detected = 0 THEN 1 ELSE 0 END)
                           AS non_detections,
                       SUM(CASE WHEN usability != 'usable' THEN 1 ELSE 0 END) AS excluded
                FROM geo_measurements GROUP BY p25_site_id
                """
            )
        }
    finally:
        connection.close()

    overview: list[dict[str, Any]] = []
    for site in sites:
        site_id = int(site["p25_site_id"])
        solution = solutions.get(site_id)
        count = counts.get(site_id, {})
        overview.append(
            {
                "p25_site_id": site_id,
                "site_key": site["site_key"],
                "rfss": site["rfss"],
                "site": site["site"],
                "observation_status": site["observation_status"],
                "nac_hex": site["nac_hex"],
                "notes": site["notes"],
                "channels": site["channels"],
                "measurement_total": int(count.get("total", 0) or 0),
                "detections": int(count.get("detections", 0) or 0),
                "non_detections": int(count.get("non_detections", 0) or 0),
                "excluded": int(count.get("excluded", 0) or 0),
                "status": (solution or {}).get("status"),
                "status_reason": (solution or {}).get("status_reason"),
                "mode_latitude": (solution or {}).get("mode_latitude"),
                "mode_longitude": (solution or {}).get("mode_longitude"),
                "area_km2_50": (solution or {}).get("area_km2_50"),
                "area_km2_90": (solution or {}).get("area_km2_90"),
                "solved_at": (solution or {}).get("solved_at"),
                "warnings": json.loads((solution or {}).get("warnings_json") or "[]"),
            }
        )
    return overview


__all__ = [
    "METHOD",
    "STATUS_FREQUENCY_UNKNOWN",
    "STATUS_NO_MEASUREMENTS",
    "build_map_geojson",
    "import_reference_sites",
    "materialise_measurements",
    "site_overview",
    "solve_all_sites",
]
