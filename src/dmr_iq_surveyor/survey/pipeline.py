"""Phase 6A survey orchestration: recording -> discovery -> database -> reports.

`run_survey()` is the single entry point behind `dmr-surveyor survey run`.
It never assumes a frequency list, never touches reference data, and never
requires a protocol decoder -- Phase 6A is complete without one.
"""

from __future__ import annotations

import resource
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.inspection import sha256_file, write_json
from dmr_iq_surveyor.reporting.json_report import build_comparison_report, build_survey_report
from dmr_iq_surveyor.reporting.markdown import (
    render_comparison_markdown,
    render_survey_report_markdown,
)
from dmr_iq_surveyor.survey.compare import compare_runs, store_comparison
from dmr_iq_surveyor.survey.discovery import discover_observations, resolve_capture_time
from dmr_iq_surveyor.survey.profiles import (
    BandProfile,
    ComparisonTolerances,
    SiteProfile,
    resolve_band_profile,
    resolve_site_profile,
)
from dmr_iq_surveyor.survey.store import (
    SurveyRunRecord,
    connect_survey_database,
    get_run,
    get_run_observations,
    import_survey_run,
    upsert_site,
)

DEFAULT_DATABASE_PATH = Path("runs/inventory/dmr_inventory.sqlite3")


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value * 1024 if sys.platform != "darwin" else value)


class SurveyLog:
    """Small explicit stage-transition log, written to `logs/survey.log`.

    Never collapses a stage failure into a bare "FAILED" -- every entry
    names the stage and what happened, per the project's evidence-first
    logging principle.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []

    def info(self, message: str) -> None:
        self._lines.append(f"{datetime.now(UTC).isoformat()} INFO {message}")

    def warning(self, message: str) -> None:
        self._lines.append(f"{datetime.now(UTC).isoformat()} WARNING {message}")

    def write(self, path: Path) -> None:
        path.write_text("\n".join(self._lines) + "\n", encoding="utf-8")


def _default_run_id(site: SiteProfile) -> str:
    return f"{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{site.site_id}"


def run_survey(
    recording_path: str | Path,
    output_root: str | Path,
    *,
    band: str | Path | BandProfile,
    site: str | Path | SiteProfile,
    run_id: str | None = None,
    database_path: str | Path | None = None,
    assumed_iq_order: str = "IQ",
    compute_source_hash: bool = False,
    spectrum_fft_size: int = 65_536,
    spectrum_overlap_ratio: float = 0.5,
    profile_base_dir: str | Path = ".",
    raster_tolerance_hz: float | None = None,
) -> dict[str, Any]:
    started = time.time()
    log = SurveyLog()

    source = Path(recording_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)

    band_profile = (
        band if isinstance(band, BandProfile) else resolve_band_profile(band, base_dir=profile_base_dir)
    )
    site_profile = (
        site if isinstance(site, SiteProfile) else resolve_site_profile(site, base_dir=profile_base_dir)
    )
    log.info(f"resolved band profile {band_profile.name!r}, site profile {site_profile.site_id!r}")
    if not site_profile.is_gain_comparable:
        log.warning(
            f"site {site_profile.site_id!r} has no recorded gain; "
            "cross-run SNR comparisons involving this run are not gain-comparable"
        )

    destination = Path(output_root).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "reports").mkdir(exist_ok=True)
    (destination / "logs").mkdir(exist_ok=True)

    resolved_run_id = run_id or _default_run_id(site_profile)
    log.info(f"survey run {resolved_run_id!r} starting on {source}")

    source_sha256 = None
    if compute_source_hash:
        log.info("hashing source recording (--hash-source)")
        source_sha256 = sha256_file(source)

    log.info(
        f"discovery: fft_size={spectrum_fft_size}, overlap={spectrum_overlap_ratio}, "
        f"segment_seconds={band_profile.segment_seconds}, "
        f"stride_seconds={band_profile.segment_stride_seconds}, "
        f"max_segments={band_profile.max_segments}"
    )
    result = discover_observations(
        source,
        band_profile=band_profile,
        assumed_iq_order=assumed_iq_order,
        spectrum_fft_size=spectrum_fft_size,
        spectrum_overlap_ratio=spectrum_overlap_ratio,
    )
    info = result["recording"]
    passband = result["usable_passband"]
    log.info(
        f"segments analyzed: {result['segments_analyzed']}/{result['segment_count']} "
        f"({result['segments_skipped']} too short to analyze)"
    )
    log.info(
        f"usable passband measured: {passband.usable_low_hz / 1e6:.6f}-"
        f"{passband.usable_high_hz / 1e6:.6f} MHz ({passband.coverage_status})"
    )
    if passband.coverage_status == "partial":
        for low, high in passband.uncovered_ranges_hz:
            log.warning(
                f"requested band not fully covered: {low / 1e6:.6f}-{high / 1e6:.6f} MHz unanalyzed"
            )
    log.info(f"candidates found: {len(result['observations'])}")
    for observation in result["observations"]:
        log.info(
            f"candidate found at {observation.measured_center_hz / 1e6:.6f} MHz "
            f"(spectral_class={observation.spectral_class}, "
            f"persistence={observation.persistence:.2f}, occupancy={observation.occupancy_pct:.1f}%); "
            "classification remained unknown (no protocol probe in Phase 6A)"
        )

    capture_start_utc, capture_time_source = resolve_capture_time(info)

    database = Path(database_path).expanduser().resolve() if database_path else DEFAULT_DATABASE_PATH.resolve()
    connection = connect_survey_database(database)
    try:
        upsert_site(connection, site_profile)
        tolerance = (
            raster_tolerance_hz
            if raster_tolerance_hz is not None
            else band_profile.comparison.frequency_tolerance_hz
        )
        run_record = SurveyRunRecord(
            survey_run_id=resolved_run_id,
            site_id=site_profile.site_id,
            band_profile=band_profile.name,
            source_path=str(source),
            source_sha256=source_sha256,
            center_frequency_hz=float(info.center_frequency_hz),
            sample_rate_hz=float(info.fmt.sample_rate_hz),
            capture_start_utc=capture_start_utc,
            capture_time_source=capture_time_source,
            requested_start_hz=band_profile.start_frequency_hz,
            requested_stop_hz=band_profile.stop_frequency_hz,
            usable_low_hz=passband.usable_low_hz,
            usable_high_hz=passband.usable_high_hz,
            coverage_status=passband.coverage_status,
            duration_seconds=info.duration_seconds,
            analyzed_seconds=result["analyzed_seconds"],
            segment_count=result["segment_count"],
            occupancy_threshold_db=result["occupancy_threshold_db"],
            detection_settings=result["detection_settings"].to_dict(),
            tool_version=__version__,
            status="ok",
            settings=band_profile.to_dict(),
        )
        log.info(f"capture time resolved as {run_record.capture_start_utc!r} (source={run_record.capture_time_source})")
        import_summary = import_survey_run(
            connection,
            run=run_record,
            observations=result["observations"],
            raster_tolerance_hz=tolerance,
        )
        log.info(
            f"stored {import_summary['observations_imported']} observations, "
            f"touched {import_summary['rf_frequencies_touched']} catalog frequencies"
        )

        run_row = get_run(connection, resolved_run_id)
        observation_rows = get_run_observations(connection, resolved_run_id)
    finally:
        connection.close()

    assert run_row is not None
    report = build_survey_report(run=run_row, observations=observation_rows)
    write_json(destination / "reports" / "report.json", report)
    (destination / "reports" / "report.md").write_text(
        render_survey_report_markdown(run=run_row, observations=observation_rows),
        encoding="utf-8",
    )

    elapsed = time.time() - started
    manifest = {
        "tool": "dmr-iq-surveyor",
        "tool_version": __version__,
        "survey_run_id": resolved_run_id,
        "site_id": site_profile.site_id,
        "band_profile": band_profile.name,
        "database_path": str(database),
        "output_dir": str(destination),
        "observation_count": len(observation_rows),
        "usable_passband": passband.to_dict(),
        "elapsed_seconds": elapsed,
        "peak_rss_bytes": _peak_rss_bytes(),
    }
    write_json(destination / "run.json", manifest)
    log.info(f"survey run complete in {elapsed:.1f}s, peak RSS {_peak_rss_bytes() / (1024 ** 2):.1f} MiB")
    log.write(destination / "logs" / "survey.log")

    return {
        "run_id": resolved_run_id,
        "output_dir": str(destination),
        "database_path": str(database),
        "observation_count": len(observation_rows),
        "usable_passband": passband.to_dict(),
        "coverage_status": passband.coverage_status,
        "elapsed_seconds": elapsed,
        "peak_rss_bytes": _peak_rss_bytes(),
    }


def run_comparison(
    output_root: str | Path,
    *,
    baseline_run_id: str,
    target_run_id: str,
    database_path: str | Path | None = None,
    tolerances_from: BandProfile | None = None,
) -> dict[str, Any]:
    """Compare two survey runs and write `comparison_<A>_<B>.{json,md}`.

    Works with no protocol decoder installed -- it only reads the runs and
    observations already stored by `run_survey`.
    """
    database = Path(database_path).expanduser().resolve() if database_path else DEFAULT_DATABASE_PATH.resolve()
    connection = connect_survey_database(database)
    try:
        baseline_row = get_run(connection, baseline_run_id)
        if baseline_row is None:
            raise ValueError(f"Unknown survey run: {baseline_run_id}")
        tolerances = tolerances_from.comparison if tolerances_from is not None else ComparisonTolerances()
        rows = compare_runs(
            connection,
            baseline_run_id=baseline_run_id,
            target_run_id=target_run_id,
            tolerances=tolerances,
        )
        store_comparison(connection, rows)
    finally:
        connection.close()

    row_dicts = [row.to_dict() for row in rows]
    report = build_comparison_report(
        baseline_run_id=baseline_run_id, target_run_id=target_run_id, rows=row_dicts
    )
    destination = Path(output_root).expanduser().resolve()
    (destination / "reports").mkdir(parents=True, exist_ok=True)
    stem = f"comparison_{baseline_run_id}_{target_run_id}"
    write_json(destination / "reports" / f"{stem}.json", report)
    (destination / "reports" / f"{stem}.md").write_text(
        render_comparison_markdown(
            baseline_run_id=baseline_run_id, target_run_id=target_run_id, rows=row_dicts
        ),
        encoding="utf-8",
    )
    return report


__all__ = ["DEFAULT_DATABASE_PATH", "SurveyLog", "run_comparison", "run_survey"]
