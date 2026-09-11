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

Those two rules leave a third thing unsayable, and PR3 made that a problem
rather than a curiosity: **"only the runs that carry no campaign"**. `None`
was already taken -- it means the whole database -- so once a deployment
named a campaign there was no scope at all that could name the runs recorded
before campaigns existed, and 51 rounds of real work became unreachable from
the field app without restarting it. `unassigned_only` is that third state.
It is a separate flag rather than a magic id because `campaign_id` is also
the value a scoped solve *stores* as its own boundary, and a stored `NULL`
already means "this solve read everything". Hence `stored_campaign_id()`:
the unassigned scope can be read from and can never be written back, because
there is no honest value for it to write.

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
    """Which runs an analysis may see.

    Three states, not two:

    * `CampaignScope(None)` -- every run in the database, and no predicate
      at all. What an unscoped command has always done.
    * `CampaignScope("day1")` -- that campaign's runs, and only those.
    * `CampaignScope(unassigned_only=True)` -- the runs that declare no
      campaign, and only those. `campaign_id` stays `None` here because
      that is genuinely what those rows hold; the flag is what separates
      "the unassigned ones" from "all of them".
    """

    campaign_id: str | None = None
    unassigned_only: bool = False

    def __post_init__(self) -> None:
        if self.campaign_id is not None and self.unassigned_only:
            raise CampaignScopeError(
                f"a scope cannot be both campaign {self.campaign_id!r} and the "
                "unassigned runs: a run that carries a campaign is by definition "
                "not unassigned"
            )

    @property
    def is_whole_database(self) -> bool:
        return self.campaign_id is None and not self.unassigned_only

    @property
    def label(self) -> str:
        if self.unassigned_only:
            return "unassigned runs"
        return self.campaign_id if self.campaign_id is not None else "whole database"

    def stored_campaign_id(self) -> str | None:
        """The boundary a solve computed under this scope may record as its own.

        `geo_solutions.campaign_id` and `geo_plans.campaign_id` answer "which
        round was this conclusion drawn from", and a stored `NULL` there
        already has a meaning: the solve read the whole file. The unassigned
        scope has no honest value to store -- `NULL` would claim it read
        everything, and any id would claim a campaign nobody declared -- so it
        refuses rather than mislabelling a result that outlives the session
        that produced it.
        """
        if self.unassigned_only:
            raise CampaignScopeError(
                "a solve cannot be stored against the unassigned runs: "
                "`campaign_id IS NULL` already means 'this solve read the whole "
                "database', so stamping one there would claim a boundary it never "
                "applied. Assign those runs to a campaign first, or solve unscoped"
            )
        return self.campaign_id

    def where(self, alias: str = "r") -> tuple[str, tuple[Any, ...]]:
        """A predicate for a query that already reaches `survey_runs`.

        Returns `("", ())` when unscoped, so a caller can concatenate it
        unconditionally and get exactly the query it had before.

        The unassigned scope is `IS NULL`, never `= NULL`: the latter is
        never true for any row, so a scope written that way would report an
        empty database rather than the rows it was asked for.
        """
        if self.unassigned_only:
            return f"{alias}.campaign_id IS NULL", ()
        if self.campaign_id is None:
            return "", ()
        return f"{alias}.campaign_id = ?", (self.campaign_id,)

    def run_ids(self, connection: sqlite3.Connection) -> list[str] | None:
        """Every run in this campaign, oldest first, or `None` for all runs.

        `None` rather than "every id in the database" on purpose: callers use
        it to mean "no filter", and materialising the whole table just to hand
        it back would turn an unscoped rebuild into a list that goes stale the
        moment another run is imported.
        """
        if self.is_whole_database:
            return None
        predicate, parameters = self.where("survey_runs")
        return [
            str(row["survey_run_id"])
            for row in connection.execute(
                f"SELECT survey_run_id FROM survey_runs WHERE {predicate} "
                "ORDER BY COALESCE(capture_start_utc, imported_at) ASC",
                parameters,
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
        if self.is_whole_database:
            return wanted
        allowed = set(self.run_ids(connection) or ())
        outside = [run_id for run_id in wanted if run_id not in allowed]
        if outside:
            raise CampaignScopeError(
                f"{len(outside)} run(s) are not in campaign {self.label!r}: "
                f"{', '.join(sorted(outside))}. A run is either in the campaign being "
                "analysed or it is not; it is refused rather than quietly dropped"
            )
        return wanted


WHOLE_DATABASE = CampaignScope(None)
UNASSIGNED_ONLY = CampaignScope(unassigned_only=True)

# What a stored `campaign_id IS NULL` on a solution or a plan actually says.
# Not "this belongs to the unassigned runs" -- there is no such solve, because
# `stored_campaign_id()` refuses to write one. It says the solve read whatever
# the whole file held at the time, which is what every analysis run before
# campaigns existed did. Shown beside a campaign's own conclusions it needs a
# name, or it reads as one of them.
HISTORICAL_WHOLE_DATABASE_LABEL = "Historical whole-database analysis"


def stored_analysis_label(campaign_id: str | None) -> str:
    """Name the boundary a stored solution or plan was computed under.

    One function so the phrase is identical everywhere it appears -- the API,
    the map popup, the site card -- rather than three near-misses an operator
    has to decide are the same thing.
    """
    if campaign_id is None:
        return HISTORICAL_WHOLE_DATABASE_LABEL
    return f"Campaign {campaign_id}"


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
    "HISTORICAL_WHOLE_DATABASE_LABEL",
    "UNASSIGNED_ONLY",
    "WHOLE_DATABASE",
    "CampaignScope",
    "CampaignScopeError",
    "resolve_scope",
    "stored_analysis_label",
]
