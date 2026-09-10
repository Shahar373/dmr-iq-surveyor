"""One campaign as an analysis boundary, not a label.

PR1 gave a run a `campaign_id`; it decided what a run *said about itself* and
nothing else. Every analysis still read the whole database, so two collection
rounds recorded into one file were separate only in the digest's collection
section -- their levels shared a reference gain, their noise floors shared a
median, and their measurements shared a solve.

This module is the single place that turns an id into a filter. Two rules
shape it, and every caller inherits both:

  * **No campaign means the whole database.** `CampaignScope(None)` produces no
    predicate at all, so a command run without `--campaign` executes the same
    SQL it always did. That is what keeps every existing invocation, every
    stored report and every test from before this change true.
  * **A campaign never includes the unassigned.** A run written before
    campaigns existed carries `campaign_id IS NULL`. It was not taken under
    the round being asked about -- nobody declared that it was -- so it is
    excluded rather than swept in. `campaign_id = ?` does this in SQL already,
    since `NULL = 'day1'` is NULL and never true; the rule is written down
    here because it is a decision, not an accident of three-valued logic.

The scope is deliberately not a table column. `survey_runs.campaign_id` is the
one place the fact lives, and everything else reaches it by join, so a run
reassigned in one place is reassigned everywhere.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from dmr_iq_surveyor.survey.provenance import ProvenanceError, normalise_campaign_id


class CampaignScopeError(ValueError):
    """Raised when a request names runs that are not in the campaign asked for."""


@dataclass(frozen=True, slots=True)
class CampaignScope:
    """Which runs an analysis may see. `None` is every run in the database."""

    campaign_id: str | None = None

    @property
    def is_whole_database(self) -> bool:
        return self.campaign_id is None

    @property
    def label(self) -> str:
        return self.campaign_id if self.campaign_id is not None else "whole database"

    def where(self, alias: str = "r") -> tuple[str, tuple[Any, ...]]:
        """A predicate for a query that already reaches `survey_runs`.

        Returns `("", ())` when unscoped, so a caller can concatenate it
        unconditionally and get exactly the query it had before.
        """
        if self.campaign_id is None:
            return "", ()
        return f"{alias}.campaign_id = ?", (self.campaign_id,)

    def clause(self, alias: str = "r", *, keyword: str = "WHERE") -> tuple[str, tuple[Any, ...]]:
        """`where()` with its leading keyword, or `("", ())` when unscoped."""
        predicate, parameters = self.where(alias)
        if not predicate:
            return "", ()
        return f" {keyword} {predicate}", parameters

    def run_ids(self, connection: sqlite3.Connection) -> list[str] | None:
        """Every run in this campaign, oldest first, or `None` for all runs.

        `None` rather than "every id in the database" on purpose: callers use
        it to mean "no filter", and materialising the whole table just to hand
        it back would turn an unscoped rebuild into a list that goes stale the
        moment another run is imported.
        """
        if self.campaign_id is None:
            return None
        return [
            str(row["survey_run_id"])
            for row in connection.execute(
                "SELECT survey_run_id FROM survey_runs WHERE campaign_id = ? "
                "ORDER BY COALESCE(capture_start_utc, imported_at) ASC",
                (self.campaign_id,),
            )
        ]

    def narrow(
        self, connection: sqlite3.Connection, run_ids: Sequence[str] | None
    ) -> list[str] | None:
        """Check an explicit run list against this campaign.

        A run the caller named that is not in the campaign is an error, not a
        silent drop: naming a run and having it quietly ignored is how an
        operator ends up believing a rebuild covered a stop it never touched.
        """
        if run_ids is None:
            return None
        wanted = list(run_ids)
        if self.campaign_id is None:
            return wanted
        allowed = set(self.run_ids(connection) or ())
        outside = [run_id for run_id in wanted if run_id not in allowed]
        if outside:
            raise CampaignScopeError(
                f"{len(outside)} run(s) are not in campaign {self.campaign_id!r}: "
                f"{', '.join(sorted(outside))}. A run is either in the campaign being "
                "analysed or it is not; it is refused rather than quietly dropped"
            )
        return wanted


WHOLE_DATABASE = CampaignScope(None)


def resolve_scope(campaign: str | None) -> CampaignScope:
    """Validate a campaign id and turn it into a scope.

    The id goes through `normalise_campaign_id`, the same validator that
    writes one, so `--campaign Day1` selects the runs stored as `day1` rather
    than silently matching nothing.
    """
    try:
        resolved = normalise_campaign_id(campaign)
    except ProvenanceError as exc:  # pragma: no cover - re-raised verbatim
        raise CampaignScopeError(str(exc)) from exc
    return CampaignScope(resolved)


__all__ = [
    "WHOLE_DATABASE",
    "CampaignScope",
    "CampaignScopeError",
    "resolve_scope",
]
