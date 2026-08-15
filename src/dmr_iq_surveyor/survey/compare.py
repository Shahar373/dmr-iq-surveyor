"""Phase 6A protocol-agnostic run-vs-run comparison.

Works with no decoder installed: it only reads `survey_runs` and
`rf_observations`, joined on the shared `rf_frequencies` catalog (which
already tolerance-matches the same physical channel across runs at import
time). `dmr-surveyor survey compare RUN_A RUN_B` is exactly this function.

Comparability is checked before any delta is emitted. Two runs from
different sites, with different detection settings, wildly different
analyzed durations, or where a frequency falls outside one run's measured
usable passband are `NOT_COMPARABLE` -- never silently compared, and never
reported as `MISSING_THIS_RUN` (that status means "we could tell it wasn't
there", which is a different claim from "we couldn't tell either way").
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from dmr_iq_surveyor.survey.profiles import ComparisonTolerances
from dmr_iq_surveyor.survey.store import get_run, get_run_observations

STATUS_NEW = "NEW"
STATUS_MISSING_THIS_RUN = "MISSING_THIS_RUN"
STATUS_STABLE = "STABLE"
STATUS_SNR_CHANGE = "SNR_CHANGE"
STATUS_OCCUPANCY_CHANGE = "OCCUPANCY_CHANGE"
STATUS_PERSISTENCE_CHANGE = "PERSISTENCE_CHANGE"
STATUS_NOT_COMPARABLE = "NOT_COMPARABLE"


@dataclass(slots=True)
class ComparisonRow:
    baseline_run_id: str
    target_run_id: str
    rf_frequency_id: int | None
    nominal_frequency_hz: float | None
    status: str
    reason: str
    delta: dict[str, float]
    comparable: bool
    not_comparable_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_level_comparability(baseline: dict[str, Any], target: dict[str, Any]) -> str | None:
    """Return a reason string if the two runs are not comparable at all, or
    None if they are (per-frequency checks still apply afterwards)."""
    if baseline["site_id"] != target["site_id"]:
        return f"different sites ({baseline['site_id']!r} vs {target['site_id']!r})"
    baseline_range = (baseline["requested_start_hz"], baseline["requested_stop_hz"])
    target_range = (target["requested_start_hz"], target["requested_stop_hz"])
    if baseline_range[1] <= target_range[0] or target_range[1] <= baseline_range[0]:
        return "requested bands do not overlap"
    if baseline["occupancy_threshold_db"] != target["occupancy_threshold_db"]:
        return "different occupancy_threshold_db"
    if json.loads(baseline["detection_settings_json"]) != json.loads(
        target["detection_settings_json"]
    ):
        return "different detection settings"
    return None


def _analyzed_seconds_comparable(
    baseline: dict[str, Any], target: dict[str, Any], ratio_limit: float
) -> bool:
    values = sorted([float(baseline["analyzed_seconds"]), float(target["analyzed_seconds"])])
    if values[0] <= 0:
        return False
    return (values[1] / values[0]) <= ratio_limit


def _within_usable_passband(frequency_hz: float, run: dict[str, Any]) -> bool:
    low = run.get("usable_low_hz")
    high = run.get("usable_high_hz")
    if low is None or high is None:
        return False
    return low <= frequency_hz <= high


def compare_runs(
    connection: sqlite3.Connection,
    *,
    baseline_run_id: str,
    target_run_id: str,
    tolerances: ComparisonTolerances,
) -> list[ComparisonRow]:
    baseline = get_run(connection, baseline_run_id)
    target = get_run(connection, target_run_id)
    if baseline is None:
        raise ValueError(f"Unknown survey run: {baseline_run_id}")
    if target is None:
        raise ValueError(f"Unknown survey run: {target_run_id}")

    run_reason = _run_level_comparability(baseline, target)
    if run_reason is None and not _analyzed_seconds_comparable(
        baseline, target, tolerances.analyzed_seconds_ratio_limit
    ):
        run_reason = "analyzed_seconds ratio exceeds comparison tolerance"

    if run_reason is not None:
        return [
            ComparisonRow(
                baseline_run_id=baseline_run_id,
                target_run_id=target_run_id,
                rf_frequency_id=None,
                nominal_frequency_hz=None,
                status=STATUS_NOT_COMPARABLE,
                reason=run_reason,
                delta={},
                comparable=False,
                not_comparable_reason=run_reason,
            )
        ]

    baseline_obs = {row["rf_frequency_id"]: row for row in get_run_observations(connection, baseline_run_id)}
    target_obs = {row["rf_frequency_id"]: row for row in get_run_observations(connection, target_run_id)}
    all_ids = sorted(set(baseline_obs) | set(target_obs))

    rows: list[ComparisonRow] = []
    for rf_frequency_id in all_ids:
        base_row = baseline_obs.get(rf_frequency_id)
        target_row = target_obs.get(rf_frequency_id)
        nominal_hz = (base_row or target_row)["nominal_frequency_hz"]

        in_baseline_passband = _within_usable_passband(nominal_hz, baseline)
        in_target_passband = _within_usable_passband(nominal_hz, target)
        if not (in_baseline_passband and in_target_passband):
            reason = "outside the usable passband measured for at least one run"
            rows.append(
                ComparisonRow(
                    baseline_run_id=baseline_run_id,
                    target_run_id=target_run_id,
                    rf_frequency_id=rf_frequency_id,
                    nominal_frequency_hz=nominal_hz,
                    status=STATUS_NOT_COMPARABLE,
                    reason=reason,
                    delta={},
                    comparable=False,
                    not_comparable_reason=reason,
                )
            )
            continue

        if base_row is None:
            rows.append(
                ComparisonRow(
                    baseline_run_id=baseline_run_id,
                    target_run_id=target_run_id,
                    rf_frequency_id=rf_frequency_id,
                    nominal_frequency_hz=nominal_hz,
                    status=STATUS_NEW,
                    reason="observed in target run, not in baseline run",
                    delta={},
                    comparable=True,
                    not_comparable_reason=None,
                )
            )
            continue
        if target_row is None:
            rows.append(
                ComparisonRow(
                    baseline_run_id=baseline_run_id,
                    target_run_id=target_run_id,
                    rf_frequency_id=rf_frequency_id,
                    nominal_frequency_hz=nominal_hz,
                    status=STATUS_MISSING_THIS_RUN,
                    reason="observed in baseline run, not in target run this capture",
                    delta={},
                    comparable=True,
                    not_comparable_reason=None,
                )
            )
            continue

        snr_delta = float(target_row["snr_db"]) - float(base_row["snr_db"])
        occupancy_delta = float(target_row["occupancy_pct"]) - float(base_row["occupancy_pct"])
        persistence_delta = float(target_row["persistence"]) - float(base_row["persistence"])
        delta = {
            "snr_db": snr_delta,
            "occupancy_pct": occupancy_delta,
            "persistence": persistence_delta,
        }
        if abs(snr_delta) > tolerances.snr_delta_db:
            status, reason = STATUS_SNR_CHANGE, f"SNR changed by {snr_delta:+.1f} dB"
        elif abs(occupancy_delta) > tolerances.occupancy_delta_pct:
            status, reason = (
                STATUS_OCCUPANCY_CHANGE,
                f"occupancy changed by {occupancy_delta:+.1f} points",
            )
        elif abs(persistence_delta) > tolerances.persistence_delta:
            status, reason = (
                STATUS_PERSISTENCE_CHANGE,
                f"persistence changed by {persistence_delta:+.2f}",
            )
        else:
            status, reason = STATUS_STABLE, "within comparison tolerances"
        rows.append(
            ComparisonRow(
                baseline_run_id=baseline_run_id,
                target_run_id=target_run_id,
                rf_frequency_id=rf_frequency_id,
                nominal_frequency_hz=nominal_hz,
                status=status,
                reason=reason,
                delta=delta,
                comparable=True,
                not_comparable_reason=None,
            )
        )
    rows.sort(key=lambda row: (row.nominal_frequency_hz is None, row.nominal_frequency_hz or 0.0))
    return rows


def store_comparison(
    connection: sqlite3.Connection, rows: list[ComparisonRow]
) -> None:
    if not rows:
        return
    baseline_run_id = rows[0].baseline_run_id
    target_run_id = rows[0].target_run_id
    connection.execute(
        "DELETE FROM run_comparisons WHERE baseline_run_id = ? AND target_run_id = ?",
        (baseline_run_id, target_run_id),
    )
    now = datetime.now(UTC).isoformat()
    for row in rows:
        connection.execute(
            """
            INSERT INTO run_comparisons(
                baseline_run_id, target_run_id, rf_frequency_id, status, reason,
                delta_json, comparable, not_comparable_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.baseline_run_id,
                row.target_run_id,
                row.rf_frequency_id,
                row.status,
                row.reason,
                json.dumps(row.delta, sort_keys=True),
                int(row.comparable),
                row.not_comparable_reason,
                now,
            ),
        )
    connection.commit()


__all__ = [
    "STATUS_MISSING_THIS_RUN",
    "STATUS_NEW",
    "STATUS_NOT_COMPARABLE",
    "STATUS_OCCUPANCY_CHANGE",
    "STATUS_PERSISTENCE_CHANGE",
    "STATUS_SNR_CHANGE",
    "STATUS_STABLE",
    "ComparisonRow",
    "compare_runs",
    "store_comparison",
]
