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

import numpy as np

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.geo.commonmode import CommonModeSettings, estimate_offsets
from dmr_iq_surveyor.geo.commonmode import residuals_by_run as _residuals_by_run
from dmr_iq_surveyor.geo.commonmode import summarise as summarise_common_mode
from dmr_iq_surveyor.geo.contours import credible_regions, regions_to_geojson
from dmr_iq_surveyor.geo.measurements import (
    MeasurementSettings,
    build_run_measurements,
    summarise,
)
from dmr_iq_surveyor.geo.model import GeoMeasurement, LocalProjection, SolveSettings
from dmr_iq_surveyor.geo.planning import (
    PlanSettings,
    build_target,
    plan_next_stops,
    plan_to_geojson,
)
from dmr_iq_surveyor.geo.report import render_solution_markdown
from dmr_iq_surveyor.geo.solver import (
    FIT_NOT_FITTED,
    FIT_UNDERDETERMINED,
    SOURCE_MODEL,
    solve_site,
)
from dmr_iq_surveyor.geo.store import (
    connect_geo_database,
    fetch_all_measurements,
    fetch_site_measurements,
    latest_solutions,
    replace_run_measurements,
    store_plan,
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

# A local noise floor this far from the campaign's changes every level at the
# stop, in the direction the solver reads as distance.
NOISE_FLOOR_SHIFT_DB = 4.0


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
    # Measurements were derived from the PREVIOUS frequency map. Leaving them
    # would mean a corrected snapshot -- a frequency added, an ambiguity
    # resolved -- silently never took effect, while the reports went on
    # asserting the old verdict.
    connection = connect_geo_database(_database(database_path))
    try:
        stale = connection.execute("SELECT COUNT(*) AS n FROM geo_measurements").fetchone()["n"]
    finally:
        connection.close()
    if stale:
        rebuilt = materialise_measurements(database_path=database_path)
        summary["measurements_rebuilt"] = rebuilt["summary"]
        summary["warnings"] = [
            *summary["warnings"],
            (
                f"{stale} existing measurement(s) were rebuilt against the new snapshot; "
                "re-run `geo solve` for the solutions to follow"
            ),
        ]
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
        noise_floors = _campaign_noise_floors(connection, run_ids)
        reference_noise = _median_noise_floor(noise_floors)
        per_run: list[dict[str, Any]] = []
        total: list[dict[str, Any]] = []
        for run_id in run_ids:
            rows = build_run_measurements(connection, run_id, resolved)
            _flag_gain_drift(rows, gains.get(run_id), reference_gain)
            _flag_noise_floor_shift(rows, noise_floors.get(run_id), reference_noise)
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
        "reference_noise_floor_dbfs_per_hz": reference_noise,
        "noise_floor_by_run": noise_floors,
        "noise_floor_shift_runs": sorted(
            run_id
            for run_id, floor in noise_floors.items()
            if reference_noise is not None
            and floor is not None
            and abs(floor - reference_noise) >= NOISE_FLOOR_SHIFT_DB
        ),
    }


def _campaign_noise_floors(connection: Any, run_ids: Sequence[str]) -> dict[str, float | None]:
    """The local noise floor each stop measured, one number per stop."""
    rows = connection.execute(
        """
        SELECT survey_run_id, noise_floor_dbfs_per_hz
        FROM rf_observations
        """
    ).fetchall()
    wanted = set(run_ids)
    collected: dict[str, list[float]] = {}
    for row in rows:
        run_id = str(row["survey_run_id"])
        if run_id in wanted and row["noise_floor_dbfs_per_hz"] is not None:
            collected.setdefault(run_id, []).append(float(row["noise_floor_dbfs_per_hz"]))
    result: dict[str, float | None] = {run_id: None for run_id in wanted}
    for run_id, values in collected.items():
        ordered = sorted(values)
        result[run_id] = ordered[len(ordered) // 2]
    return result


def _median_noise_floor(noise_floors: dict[str, float | None]) -> float | None:
    values = sorted(value for value in noise_floors.values() if value is not None)
    return values[len(values) // 2] if values else None


def _flag_noise_floor_shift(
    rows: list[dict[str, Any]], floor: float | None, reference: float | None
) -> None:
    """Flag a stop whose noise floor sits well away from the campaign's.

    The level every measurement carries is SNR above the local noise floor. If
    that floor moves -- car electronics, an interferer, a different antenna
    environment -- every level at the stop moves with it, in the direction the
    solver reads as distance.
    """
    if reference is None or floor is None:
        return
    shift = floor - reference
    if abs(shift) < NOISE_FLOOR_SHIFT_DB:
        return
    for row in rows:
        row["quality_flags"] = [
            *row["quality_flags"],
            f"noise_floor_shift:{shift:+.1f}dB",
        ]


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


def _to_geo_measurements(
    rows: list[dict[str, Any]], offsets: dict[str, float] | None = None
) -> list[GeoMeasurement]:
    """Rows as solver inputs, optionally shifted by a per-stop common-mode offset.

    The offset moves the detection threshold with the level: whatever raised
    or lowered this stop's whole receive path raised or lowered the level at
    which the detector called something present, too.
    """
    applied = offsets or {}
    measurements = []
    for row in rows:
        offset = applied.get(str(row["survey_run_id"]), 0.0)
        measurements.append(
            GeoMeasurement(
                label=f"{row['survey_run_id']}@{row['frequency_hz'] / 1e6:.6f}MHz",
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                detected=bool(row["detected"]),
                level_db=(
                    None if row["level_db"] is None else float(row["level_db"]) - offset
                ),
                censor_level_db=float(row["censor_level_db"]) - offset,
                survey_run_id=str(row["survey_run_id"]),
                frequency_hz=float(row["frequency_hz"]),
            )
        )
    return measurements


def _solve_one(
    *,
    site: dict[str, Any],
    usable: list[dict[str, Any]],
    excluded: int,
    settings: SolveSettings,
    level_metric: str,
    offsets: dict[str, float],
) -> tuple[dict[str, Any], Any]:
    """Solve one site and shape its result row. Returns (row, SolveResult|None)."""
    row: dict[str, Any] = {
        "p25_site_id": int(site["p25_site_id"]),
        "site_key": site["site_key"],
        "rfss": site["rfss"],
        "site": site["site"],
        "solved_at": datetime.now(UTC).isoformat(),
        "method": METHOD,
        "source_model": SOURCE_MODEL,
        "level_metric": level_metric,
        "excluded_count": excluded,
        "tool_version": __version__,
        "settings": settings.to_dict(),
        "input_run_ids": sorted({str(item["survey_run_id"]) for item in usable}),
        "warnings": [],
        # Present on every row, including the ones that never reach the
        # solver. Filling these in only for solved sites left the report a
        # list of two different shapes, so a caller looping over it and
        # reading a mode crashed on the first site that had no frequency on
        # record -- the values are genuinely absent, and `None` says that
        # without making the row a different kind of thing.
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
        # `geojson` is deliberately NOT defaulted here. It is stored as a
        # serialized string, so seeding it with None writes the literal
        # "null", which reads back as None where every consumer expects a
        # mapping. Absent means absent for this one.
    }

    if not site["channels"]:
        row.update(
            status=STATUS_FREQUENCY_UNKNOWN,
            status_reason=(
                "no control-channel frequency is on record for this site, so nothing could be "
                "measured; it stays listed rather than being dropped"
            ),
            detection_count=0,
            non_detection_count=0,
        )
        return row, None
    if not usable:
        row.update(
            status=STATUS_NO_MEASUREMENTS,
            status_reason=(
                f"{excluded} measurement slot(s) exist for this site but none is usable "
                "(ambiguous frequency, no run position, excluded stop, or outside every "
                "measured passband)"
            ),
            detection_count=0,
            non_detection_count=0,
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
    # Surface the flags the contributing measurements carry. Stored-but-unread
    # flags are the same failure as no flags: the reader never learns that half
    # the evidence came from a stop whose gain was different.
    flags: dict[str, int] = {}
    for measurement in usable:
        for flag in json.loads(measurement["quality_flags_json"] or "[]"):
            flags[flag] = flags.get(flag, 0) + 1
    row["measurement_flags"] = flags
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
            {"site_key": site["site_key"], "status": result.status, "kind": "credible_region"},
        )
    return row, result


def _build_plan(
    *,
    rows: list[dict[str, Any]],
    results: dict[str, Any],
    usable_by_site: dict[str, list[dict[str, Any]]],
    settings: SolveSettings,
    plan_settings: PlanSettings,
) -> dict[str, Any]:
    """Rank where the next stop would teach the most, from the final posteriors."""
    targets = []
    projection: LocalProjection | None = None
    skipped: list[str] = []
    for row in rows:
        result = results.get(row["site_key"])
        if result is None or result.surface is None:
            continue
        if (
            not plan_settings.plan_from_underdetermined_fits
            and row.get("fit_status") == FIT_UNDERDETERMINED
        ):
            # The planner's entire value for a site is the entropy of the
            # detection probability it predicts at a candidate -- and that
            # prediction is computed from the reference level and exponent.
            # When those were never identified, the prediction is not a
            # prediction, and steering a drive by it sends the operator
            # somewhere for a reason that does not exist.
            skipped.append(str(row["site_key"]))
            continue
        projection = result.surface.projection
        measurements = usable_by_site.get(row["site_key"], [])
        thresholds = [float(item["censor_level_db"]) for item in measurements]
        target = build_target(
            site_key=row["site_key"],
            surface=result.surface,
            projection=projection,
            reference_level_db=float(row["reference_level_db"]),
            path_loss_exponent=float(row["path_loss_exponent"]),
            threshold_db=float(np.median(thresholds)) if thresholds else 0.0,
            area_km2=row.get("area_km2_90"),
            settings=plan_settings,
        )
        if target is not None:
            targets.append(target)

    note = (
        ""
        if not skipped
        else (
            f" {len(skipped)} site(s) were left out of the plan because their propagation fit "
            f"is not identifiable yet ({', '.join(sorted(skipped))}); they need more detections "
            "before they can say where a stop would help."
        )
    )
    if projection is None:
        return {
            "status": "no_targets",
            "reason": (
                "no site has a posterior to plan against yet; make a few stops spread around the "
                "area first" + note
            ),
            "candidates": [],
            "top_stops": [],
            "settings": plan_settings.to_dict(),
        }

    visited = sorted(
        {
            (float(item["latitude"]), float(item["longitude"]))
            for measurements in usable_by_site.values()
            for item in measurements
            if item["latitude"] is not None
        }
    )
    plan = plan_next_stops(
        targets=targets,
        projection=projection,
        visited=visited,
        solve=settings,
        settings=plan_settings,
    )
    # Appended rather than replacing the planner's own reason: which sites
    # were left out is the operator's business either way, and it is the
    # difference between "nothing helps" and "nothing that could vote, voted".
    plan["reason"] = (plan.get("reason", "") + note).strip()
    plan["excluded_from_plan"] = sorted(skipped)
    return plan


def solve_all_sites(
    *,
    database_path: str | Path | None = None,
    output_root: str | Path | None = None,
    site_keys: Sequence[str] | None = None,
    solve_batch_id: str | None = None,
    settings: SolveSettings | None = None,
    level_metric: str = "snr_db",
    common_mode: CommonModeSettings | None = None,
    plan_settings: PlanSettings | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Solve every registry site that has usable measurements.

    Runs in two passes. The first fits every site independently. Its residuals
    then reveal any stop whose whole receive path sat off the campaign -- a
    re-seated antenna, a raised noise floor -- and the second pass re-solves the
    sites that stop contributed to, with that offset removed. A campaign with
    consistent stops does no second pass at all.

    Sites without a control-channel frequency, and sites whose measurements are
    all excluded, are recorded with an explicit status rather than quietly
    omitted -- otherwise a map would silently show fewer sites than exist.
    """
    resolved = settings or SolveSettings()
    resolved.validate()
    common = common_mode or CommonModeSettings()
    common.validate()
    planning = plan_settings if plan_settings is not None else PlanSettings()
    planning.validate()

    batch = solve_batch_id or datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    connection = connect_geo_database(_database(database_path))
    try:
        sites = list_sites(connection)
        if site_keys is not None:
            wanted = set(site_keys)
            sites = [site for site in sites if site["site_key"] in wanted]

        usable_by_site: dict[str, list[dict[str, Any]]] = {}
        excluded_by_site: dict[str, int] = {}
        for site in sites:
            site_id = int(site["p25_site_id"])
            usable = fetch_site_measurements(connection, site_id, usable_only=True)
            everything = fetch_site_measurements(connection, site_id, usable_only=False)
            usable_by_site[site["site_key"]] = usable
            excluded_by_site[site["site_key"]] = len(everything) - len(usable)

        rows: list[dict[str, Any]] = []
        results: dict[str, Any] = {}
        for index, site in enumerate(sites):
            if on_progress is not None:
                on_progress(str(site["site_key"]), index, len(sites))
            row, result = _solve_one(
                site=site,
                usable=usable_by_site[site["site_key"]],
                excluded=excluded_by_site[site["site_key"]],
                settings=resolved,
                level_metric=level_metric,
                offsets={},
            )
            rows.append(row)
            if result is not None:
                results[site["site_key"]] = result

        offsets = estimate_offsets(_residuals_by_run(rows), common)
        applied = {
            run_id: offset.offset_db for run_id, offset in offsets.items() if offset.applied
        }
        if applied:
            affected = {
                site["site_key"]
                for site in sites
                if any(
                    str(item["survey_run_id"]) in applied
                    for item in usable_by_site[site["site_key"]]
                )
            }
            for index, site in enumerate(sites):
                if site["site_key"] not in affected:
                    continue
                if on_progress is not None:
                    on_progress(f"{site['site_key']} (common-mode corrected)", index, len(sites))
                row, result = _solve_one(
                    site=site,
                    usable=usable_by_site[site["site_key"]],
                    excluded=excluded_by_site[site["site_key"]],
                    settings=resolved,
                    level_metric=level_metric,
                    offsets=applied,
                )
                row["warnings"].append(
                    "levels were corrected for a per-stop common-mode offset at "
                    + ", ".join(f"{run_id} ({applied[run_id]:+.1f} dB)" for run_id in sorted(applied))
                )
                rows = [existing for existing in rows if existing["site_key"] != site["site_key"]]
                rows.append(row)
                if result is not None:
                    results[site["site_key"]] = result

        for row in rows:
            store_solution(connection, solve_batch_id=batch, row=row)

        plan = _build_plan(
            rows=rows,
            results=results,
            usable_by_site=usable_by_site,
            settings=resolved,
            plan_settings=planning,
        )
        store_plan(connection, solve_batch_id=batch, plan=plan, geojson=plan_to_geojson(plan))
        measurement_summary = summarise(fetch_all_measurements(connection))
    finally:
        connection.close()

    rows.sort(key=lambda item: (item.get("rfss", 0), item.get("site", 0)))
    report = {
        "tool": "dmr-iq-surveyor",
        "tool_version": __version__,
        "solve_batch_id": batch,
        "method": METHOD,
        "source_model": SOURCE_MODEL,
        "generated_at": datetime.now(UTC).isoformat(),
        "measurement_summary": measurement_summary,
        "settings": resolved.to_dict(),
        "common_mode": summarise_common_mode(offsets),
        "plan": plan,
        "solutions": [
            {key: value for key, value in solution.items() if key != "geojson"}
            for solution in rows
        ],
    }
    if output_root is not None:
        destination = Path(output_root).expanduser().resolve()
        (destination / "reports").mkdir(parents=True, exist_ok=True)
        write_json(destination / "reports" / f"geolocation_{batch}.json", report)
        (destination / "reports" / f"geolocation_{batch}.md").write_text(
            render_solution_markdown(
                solve_batch_id=batch,
                solutions=rows,
                measurement_summary=measurement_summary,
                settings=resolved.to_dict(),
                common_mode=report["common_mode"],
                plan=plan,
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
    "NOISE_FLOOR_SHIFT_DB",
    "STATUS_FREQUENCY_UNKNOWN",
    "STATUS_NO_MEASUREMENTS",
    "build_map_geojson",
    "import_reference_sites",
    "materialise_measurements",
    "site_overview",
    "solve_all_sites",
]
