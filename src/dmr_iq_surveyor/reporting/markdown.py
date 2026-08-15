"""Human-readable Markdown reports for Phase 6 survey runs and comparisons."""

from __future__ import annotations

from typing import Any


def render_survey_report_markdown(
    *,
    run: dict[str, Any],
    observations: list[dict[str, Any]],
    failures: list[dict[str, Any]] | None = None,
) -> str:
    failures = failures or []
    lines = [
        "# RF survey run",
        "",
        f"- Run ID: **{run['survey_run_id']}**",
        f"- Site: **{run['site_id']}**",
    ]
    if run.get("gps_latitude") is not None and run.get("gps_longitude") is not None:
        accuracy = run.get("gps_accuracy_m")
        accuracy_note = f", accuracy ~{accuracy:.0f} m" if accuracy is not None else ""
        lines.append(
            f"- GPS: **{run['gps_latitude']:.6f}, {run['gps_longitude']:.6f}** "
            f"(source: {run.get('gps_source', 'unknown')}{accuracy_note})"
        )
    else:
        lines.append(f"- GPS: not available (source: {run.get('gps_source', 'unknown')})")
    lines += [
        f"- Band profile: **{run['band_profile']}**",
        f"- Source: `{run['source_path']}`",
        (
            f"- Center frequency: **{run['center_frequency_hz'] / 1e6:.6f} MHz**, "
            f"sample rate **{run['sample_rate_hz'] / 1e6:.6f} MS/s**"
        ),
        (
            f"- Capture time: **{run['capture_start_utc'] or 'unknown'}** "
            f"(source: {run['capture_time_source']})"
        ),
        (
            f"- Requested band: **{run['requested_start_hz'] / 1e6:.6f}"
            f"-{run['requested_stop_hz'] / 1e6:.6f} MHz**"
        ),
        (
            f"- Measured usable passband: **"
            f"{(run['usable_low_hz'] or 0) / 1e6:.6f}-{(run['usable_high_hz'] or 0) / 1e6:.6f} MHz** "
            f"({run['coverage_status']})"
        ),
        (
            f"- Analyzed: **{run['analyzed_seconds']:.1f} s** of **{run['duration_seconds']:.1f} s**, "
            f"**{run['segment_count']}** segments"
        ),
        f"- Observations: **{len(observations)}**",
        "",
        (
            "All power values below are relative dBFS/Hz (`power_unit=dbfs_per_hz`), "
            "not calibrated dBm, unless `calibrated` is true."
        ),
        "",
        (
            "`classification` is always `unknown` in Phase 6A: no protocol decoder ran. "
            "`spectral_class` is a spectral-shape hypothesis only."
        ),
        "",
    ]
    if failures:
        lines.append(f"**{len(failures)} failure(s) during this run** (see `failures` in the JSON report).")
        lines.append("")

    lines.extend(
        [
            (
                "| Frequency MHz | Raster err Hz | BW Hz | SNR dB | P95 SNR dB | "
                "Occupancy % | Persistence | Spectral class |"
            ),
            "|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in observations:
        lines.append(
            "| {freq:.6f} | {raster_err:+.0f} | {bw:.0f} | {snr:.1f} | {p95:.1f} | "
            "{occ:.1f} | {persistence:.2f} | {spectral_class} |".format(
                freq=row["measured_center_hz"] / 1e6,
                raster_err=row["raster_error_hz"],
                bw=row["bandwidth_hz"],
                snr=row["snr_db"],
                p95=row["p95_snr_db"],
                occ=row["occupancy_pct"],
                persistence=row["persistence"],
                spectral_class=row["spectral_class"],
            )
        )
    return "\n".join(lines) + "\n"


def render_comparison_markdown(
    *,
    baseline_run_id: str,
    target_run_id: str,
    rows: list[dict[str, Any]],
) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    summary = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    lines = [
        "# Survey run comparison",
        "",
        f"- Baseline run: **{baseline_run_id}**",
        f"- Target run: **{target_run_id}**",
        f"- Status counts: {summary or 'none'}",
        "",
        "| Frequency MHz | Status | Reason | SNR delta dB | Occupancy delta pts | Persistence delta |",
        "|---:|---|---|---:|---:|---:|",
    ]
    for row in rows:
        delta = row.get("delta") or {}
        frequency = row["nominal_frequency_hz"]
        lines.append(
            "| {freq} | {status} | {reason} | {snr} | {occ} | {persistence} |".format(
                freq=(f"{frequency / 1e6:.6f}" if frequency is not None else "-"),
                status=row["status"],
                reason=row["reason"],
                snr=(f"{delta['snr_db']:+.1f}" if "snr_db" in delta else "-"),
                occ=(f"{delta['occupancy_pct']:+.1f}" if "occupancy_pct" in delta else "-"),
                persistence=(
                    f"{delta['persistence']:+.2f}" if "persistence" in delta else "-"
                ),
            )
        )
    return "\n".join(lines) + "\n"


__all__ = ["render_comparison_markdown", "render_survey_report_markdown"]
