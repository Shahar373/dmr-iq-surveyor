"""Human-readable rendering of geolocation results.

Every table in here leads with the evidence and the status, not with a
coordinate. A mode with `unbounded_region` next to it is a very different
claim from the same mode with `ok`, and a reader must not have to go
looking for that difference.
"""

from __future__ import annotations

from typing import Any

from dmr_iq_surveyor.geo.solver import FIT_UNDERDETERMINED


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
    common_mode: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    geolocation_maturity: str | None = None,
    validation_note: str | None = None,
) -> str:
    lines: list[str] = [
        "# P25 site geolocation",
        "",
        f"Solve batch: `{solve_batch_id}`",
    ]
    if geolocation_maturity:
        lines.append(f"**Geolocation maturity: {geolocation_maturity}.** {validation_note or ''}".rstrip())
    lines += [
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
        # Named, not blanked: a dash would read as "the solver did not get
        # this far", when in fact it got there and found the number carries
        # no information.
        underdetermined = solution.get("fit_status") == FIT_UNDERDETERMINED
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
                n=(
                    "unidentifiable"
                    if underdetermined
                    else (f"{exponent:.2f}" if exponent is not None else "-")
                ),
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

    if common_mode:
        lines += [
            "",
            "## Per-stop common-mode offsets",
            "",
            "Anything that shifts a whole stop's levels together -- a re-seated antenna, local",
            "interference raising the noise floor, front-end compression -- corrupts a method built",
            "on comparing levels between places. Such an effect is separable because it is *common*:",
            "if every site heard at one stop sits the same distance from its predicted level while",
            "the rest of the campaign fits, the stop is what differs. Only differences between stops",
            "are identifiable, so these are centred on zero.",
            "",
            (
                f"- estimated at {common_mode.get('estimated', 0)} stop(s), "
                f"{common_mode.get('within_noise', 0)} within noise, "
                f"{common_mode.get('not_estimable', 0)} not estimable"
            ),
            f"- corrections applied: {common_mode.get('applied', 0)}",
            f"- largest offset: {common_mode.get('largest_offset_db', 0.0):.1f} dB",
            "",
        ]
        notable = [
            offset
            for offset in (common_mode.get("offsets") or {}).values()
            if offset.get("applied") or offset.get("status") == "not_estimable"
        ]
        if notable:
            lines += ["| Stop | Status | Offset | Sites | Why |", "|---|---|---:|---:|---|"]
            for offset in sorted(notable, key=lambda item: item["survey_run_id"]):
                lines.append(
                    "| `{run}` | {status} | {offset:+.1f} dB | {sites} | {reason} |".format(
                        run=offset["survey_run_id"],
                        status=offset["status"],
                        offset=offset["offset_db"],
                        sites=offset["site_count"],
                        reason=offset["reason"],
                    )
                )

    if plan:
        lines += [
            "",
            "## Where to go next",
            "",
            "A stop is worth making where its outcome is least predictable: a place where a site is",
            "certainly heard, or certainly not, teaches nothing about where it is. Value below is the",
            "binary entropy of the predicted detection probability under each site's current",
            "posterior, weighted so a site already pinned down stops pulling the plan, and damped",
            "near places already measured. It is a planning aid computed from current beliefs, not a",
            "prediction about the transmitters.",
            "",
        ]
        if plan.get("status") != "ok":
            lines.append(f"No suggestion: {plan.get('reason', plan.get('status'))}")
        else:
            lines += ["| Rank | Latitude | Longitude | Value | Helps most |", "|---:|---|---|---:|---|"]
            for rank, stop in enumerate(plan.get("top_stops", []), start=1):
                helps = ", ".join(item["site_key"] for item in stop.get("helps_most", []))
                lines.append(
                    f"| {rank} | {stop['latitude']:.5f} | {stop['longitude']:.5f} | "
                    f"{stop['value']:.2f} | {helps} |"
                )

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
