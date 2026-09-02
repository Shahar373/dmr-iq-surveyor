"""Human-readable rendering of geolocation results.

Every table in here leads with the evidence and the status, not with a
coordinate. A mode with `unbounded_region` next to it is a very different
claim from the same mode with `ok`, and a reader must not have to go
looking for that difference.
"""

from __future__ import annotations

from typing import Any


def _format_position(latitude: float | None, longitude: float | None) -> str:
    if latitude is None or longitude is None:
        return "-"
    return f"{latitude:.5f}, {longitude:.5f}"


def _format_area(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 1.0:
        return f"{value * 100:.0f} ha"
    return f"{value:.2f} km2"


def render_solution_markdown(
    *,
    solve_batch_id: str,
    solutions: list[dict[str, Any]],
    measurement_summary: dict[str, Any],
    settings: dict[str, Any],
) -> str:
    lines: list[str] = [
        "# P25 site geolocation",
        "",
        f"Solve batch: `{solve_batch_id}`",
        "",
        "## What these results are",
        "",
        "Each region below is a Bayesian credible region for one site's transmitter, fitted to",
        "passive received-level measurements with a log-distance propagation model. A region is a",
        "search-area reduction, not a tower coordinate. The mode is the single most probable cell,",
        "not a confirmed position.",
        "",
        "Sites are attributed to frequencies from an external reference snapshot, not from",
        "control-channel decoder evidence. A frequency used by more than one site is excluded",
        "rather than guessed at.",
        "",
        "## Measurement inventory",
        "",
        "| Category | Count |",
        "|---|---:|",
        f"| Total measurement slots | {measurement_summary.get('total', 0)} |",
        f"| Usable | {measurement_summary.get('usable', 0)} |",
        f"| Detections | {measurement_summary.get('detections', 0)} |",
        f"| Non-detections (looked, heard nothing) | {measurement_summary.get('non_detections', 0)} |",
        f"| Not covered (outside measured passband) | {measurement_summary.get('not_covered', 0)} |",
        f"| Excluded, ambiguous frequency | {measurement_summary.get('ambiguous', 0)} |",
        f"| Excluded, run had no position | {measurement_summary.get('no_position', 0)} |",
        "",
        "## Sites",
        "",
        "| Site | Status | Detections | Non-det. | Mode | 50% | 90% | n | Azimuth span |",
        "|---|---|---:|---:|---|---:|---:|---:|---:|",
    ]
    for solution in sorted(solutions, key=lambda row: (row.get("rfss", 0), row.get("site", 0))):
        exponent = solution.get("path_loss_exponent")
        span = solution.get("azimuth_span_deg")
        lines.append(
            "| {key} | {status} | {det} | {non} | {mode} | {a50} | {a90} | {n} | {span} |".format(
                key=solution.get("site_key", "?"),
                status=solution.get("status", "?"),
                det=solution.get("detection_count", 0),
                non=solution.get("non_detection_count", 0),
                mode=_format_position(
                    solution.get("mode_latitude"), solution.get("mode_longitude")
                ),
                a50=_format_area(solution.get("area_km2_50")),
                a90=_format_area(solution.get("area_km2_90")),
                n=f"{exponent:.2f}" if exponent is not None else "-",
                span=f"{span:.0f} deg" if span is not None else "-",
            )
        )

    lines += ["", "## Why a site has no region", ""]
    unresolved = [
        solution
        for solution in solutions
        if solution.get("status") != "ok" or not solution.get("area_km2_90")
    ]
    if not unresolved:
        lines.append("Every site with usable measurements produced a bounded region.")
    else:
        for solution in sorted(unresolved, key=lambda row: (row.get("rfss", 0), row.get("site", 0))):
            reason = solution.get("status_reason") or "-"
            lines.append(f"- **{solution.get('site_key')}** (`{solution.get('status')}`): {reason}")
            for warning in solution.get("warnings", []):
                lines.append(f"  - warning: {warning}")

    lines += [
        "",
        "## Settings",
        "",
        "| Parameter | Value |",
        "|---|---|",
    ]
    for key in sorted(settings):
        lines.append(f"| `{key}` | {settings[key]} |")

    lines += [
        "",
        "## Limitations",
        "",
        "- Levels are relative channel SNR derived from `dBFS/Hz`, never calibrated dBm.",
        "- A single logical P25 site may be transmitted by several towers simultaneously",
        "  (simulcast). This estimator fits one point source and does not model that.",
        "- Terrain and line of sight are not modelled. A distant hilltop transmitter can",
        "  measure stronger than a nearby obstructed one.",
        "- Sites are attributed by frequency alone; nothing here confirms that the signal",
        "  measured on a frequency came from the site the snapshot associates with it.",
        "",
    ]
    return "\n".join(lines)


__all__ = ["render_solution_markdown"]
