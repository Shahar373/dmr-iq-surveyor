"""Canonical machine-readable JSON report for a Phase 6 survey run."""

from __future__ import annotations

from typing import Any

from dmr_iq_surveyor import __version__


def build_survey_report(
    *,
    run: dict[str, Any],
    observations: list[dict[str, Any]],
    failures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """`run` is the `survey_runs` row (as returned by `survey.store.get_run`);
    `observations` is `survey.store.get_run_observations` output."""
    return {
        "tool": "dmr-iq-surveyor",
        "tool_version": __version__,
        "report_kind": "survey_run",
        "run": run,
        "observation_count": len(observations),
        "observations": observations,
        "failures": failures or [],
    }


def build_comparison_report(
    *,
    baseline_run_id: str,
    target_run_id: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "tool": "dmr-iq-surveyor",
        "tool_version": __version__,
        "report_kind": "survey_comparison",
        "baseline_run_id": baseline_run_id,
        "target_run_id": target_run_id,
        "status_counts": counts,
        "rows": rows,
    }


__all__ = ["build_comparison_report", "build_survey_report"]
