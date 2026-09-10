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
    baseline_campaign_id: str | None = None,
    target_campaign_id: str | None = None,
) -> dict[str, Any]:
    """The comparison as a report, with a campaign warning where one applies.

    `campaign_differs` does NOT block the comparison. Two rounds of the same
    place are exactly what an operator wants to compare -- that is the point
    of running a second round -- and refusing would remove the only tool for
    it. But the two rounds may have been recorded under different receiver
    settings, on different days, with a reference gain established
    separately, so a level difference between them can be the campaigns
    rather than the RF. That is worth saying every time and deciding never.

    Both ids are reported whether they differ or not, so a reader never has
    to infer from the absence of a warning that both runs were assigned at
    all -- `None` means unassigned, which is what every run recorded before
    campaigns existed is.
    """
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    differs = baseline_campaign_id != target_campaign_id
    warnings: list[str] = []
    if differs:
        warnings.append(
            f"campaign_differs: baseline is {_campaign_label(baseline_campaign_id)} and "
            f"target is {_campaign_label(target_campaign_id)}. Levels recorded under "
            "different collection rounds may differ because the rounds differ, not "
            "because the RF did; each round establishes its own reference gain and "
            "noise floor."
        )
    return {
        "tool": "dmr-iq-surveyor",
        "tool_version": __version__,
        "report_kind": "survey_comparison",
        "baseline_run_id": baseline_run_id,
        "target_run_id": target_run_id,
        "baseline_campaign_id": baseline_campaign_id,
        "target_campaign_id": target_campaign_id,
        "campaign_differs": differs,
        "warnings": warnings,
        "status_counts": counts,
        "rows": rows,
    }


def _campaign_label(campaign_id: str | None) -> str:
    return repr(campaign_id) if campaign_id is not None else "unassigned"


__all__ = ["build_comparison_report", "build_survey_report"]
