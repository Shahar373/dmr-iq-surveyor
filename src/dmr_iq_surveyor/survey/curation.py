"""Assigning historical runs to a campaign, deliberately and reversibly-looking.

A run recorded before campaigns existed carries `campaign_id IS NULL`. That is
not a gap to be filled in automatically: nobody declared which round those
stops belonged to, and a backfill that guessed from a filename, a site, a date
or a coordinate would put a claim on rows that never faced the question. So
membership is only ever changed by an operator naming the runs and saying why.

What makes this more than an `UPDATE` is what hangs off it. A campaign's
reference gain and its noise-floor median are computed from the runs that are
in it, so moving one run changes the yardstick every *other* run in that
campaign was measured against. `geo_measurements` follows membership through a
join, but the campaign-relative verdicts frozen into `quality_flags_json` do
not, and `geo_solutions`/`geo_plans` do not follow at all -- they carry the
campaign they were solved under as a stored stamp. Left alone, the newest
stored solve for the target keeps being served as that campaign's current
answer although it never saw the run that just joined.

So one transaction carries all of it: the membership change, the rebuilt
measurements for every run whose yardstick moved, the audit rows, and a mark
saying the stored conclusions are superseded. The solve itself cannot join
that transaction -- it is minutes of grid search and would hold a write lock
across the field app -- so instead it is the thing that *clears* the mark. A
failure, a crash or an interrupt anywhere after the commit therefore leaves
the mark standing, and a superseded conclusion is never presented as current.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.survey.provenance import (
    SOURCE_DECLARED_SITE_ROW,
    SOURCE_NOT_RECORDED,
    normalise_campaign_id,
    receiver_settings,
)
from dmr_iq_surveyor.survey.scope import UNASSIGNED_ONLY, CampaignScope


class CurationError(ValueError):
    """Raised when an assignment is refused, before anything is written."""


@dataclass(frozen=True, slots=True)
class RunCandidate:
    """One run the operator is proposing to move, as the dry run shows it."""

    survey_run_id: str
    campaign_id: str | None
    capture_start_utc: str | None
    capture_time_source: str
    source_basename: str
    site_id: str | None
    gps_source: str
    gps_latitude: float | None
    gps_longitude: float | None
    gain: float | None
    gain_source: str

    @property
    def has_position(self) -> bool:
        return self.gps_latitude is not None and self.gps_longitude is not None


@dataclass(frozen=True, slots=True)
class SupersededAnalysis:
    """A campaign whose stored conclusions predate a membership change."""

    campaign_id: str
    superseded_at: str
    reason: str


@dataclass(slots=True)
class AssignmentPlan:
    """What an assignment would do, computed without writing anything."""

    project_id: str
    campaign_id: str
    database: str
    selected: list[RunCandidate] = field(default_factory=list)
    already_in_target: list[RunCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    target_run_count: int = 0
    unassigned_run_count: int = 0
    stored_solution_count: int = 0
    stored_plan_count: int = 0

    @property
    def moving(self) -> list[RunCandidate]:
        """The runs an assignment would actually change."""
        return self.selected


def _row_to_candidate(row: Any) -> RunCandidate:
    reading = receiver_settings(row).if_gain_reduction
    value = reading.value
    return RunCandidate(
        survey_run_id=str(row["survey_run_id"]),
        campaign_id=row["campaign_id"],
        capture_start_utc=row["capture_start_utc"],
        capture_time_source=str(row["capture_time_source"]),
        source_basename=str(row["source_basename"]),
        site_id=row["site_id"],
        gps_source=str(row["gps_source"]),
        gps_latitude=row["gps_latitude"],
        gps_longitude=row["gps_longitude"],
        gain=float(value) if isinstance(value, (int, float)) else None,
        gain_source=reading.source,
    )


_RUN_QUERY = """
    SELECT r.survey_run_id, r.campaign_id, r.capture_start_utc, r.capture_time_source,
           r.source_basename, r.site_id, r.gps_source, r.gps_latitude, r.gps_longitude,
           r.hardware_json, s.gain, s.lna_state
    FROM survey_runs r
    LEFT JOIN sites s ON s.site_id = r.site_id
"""


def fetch_runs(connection: sqlite3.Connection, run_ids: list[str]) -> dict[str, RunCandidate]:
    """The named runs, keyed by id. Missing ids are simply absent."""
    if not run_ids:
        return {}
    found: dict[str, RunCandidate] = {}
    for run_id in run_ids:
        row = connection.execute(
            _RUN_QUERY + " WHERE r.survey_run_id = ?", (run_id,)
        ).fetchone()
        if row is not None:
            found[str(row["survey_run_id"])] = _row_to_candidate(row)
    return found


def select_by_time_range(
    connection: sqlite3.Connection, *, start_utc: str, end_utc: str
) -> list[RunCandidate]:
    """Unassigned runs whose capture time falls in [start, end).

    Half-open on purpose, and documented as such wherever the flags are
    described: two adjacent ranges then partition a day without the stop on
    the boundary belonging to both. The bound is compared against
    `capture_start_utc` and nothing else -- never `imported_at`, which says
    when a file was filed rather than when the RF was recorded, and would
    sweep in a stop taken months earlier.

    A run whose capture time is NULL is not returned. `capture_time_source`
    is `unknown` for those: the time is not late or approximate, it is
    absent, and a range cannot say whether an absent time is inside it.
    Callers report them rather than letting them fall out quietly.
    """
    rows = connection.execute(
        _RUN_QUERY
        + " WHERE r.campaign_id IS NULL AND r.capture_start_utc IS NOT NULL"
        " AND r.capture_start_utc >= ? AND r.capture_start_utc < ?"
        " ORDER BY r.capture_start_utc ASC",
        (start_utc, end_utc),
    ).fetchall()
    return [_row_to_candidate(row) for row in rows]


def undated_unassigned_runs(connection: sqlite3.Connection) -> list[RunCandidate]:
    """Unassigned runs a time range can never select, because they have no time."""
    rows = connection.execute(
        _RUN_QUERY
        + " WHERE r.campaign_id IS NULL AND r.capture_start_utc IS NULL"
        " ORDER BY r.survey_run_id ASC"
    ).fetchall()
    return [_row_to_candidate(row) for row in rows]


def campaign_run_count(connection: sqlite3.Connection, scope: CampaignScope) -> int:
    predicate, parameters = scope.where("survey_runs")
    query = "SELECT COUNT(*) FROM survey_runs"
    if predicate:
        query += f" WHERE {predicate}"
    return int(connection.execute(query, parameters).fetchone()[0])


def stored_analysis_counts(
    connection: sqlite3.Connection, campaign_id: str
) -> tuple[int, int]:
    """How many solutions and plans already carry this campaign's stamp."""
    solutions = int(
        connection.execute(
            "SELECT COUNT(*) FROM geo_solutions WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()[0]
    )
    plans = int(
        connection.execute(
            "SELECT COUNT(*) FROM geo_plans WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()[0]
    )
    return solutions, plans


def superseded_analysis(
    connection: sqlite3.Connection, campaign_id: str
) -> SupersededAnalysis | None:
    """The mark, if this campaign's stored conclusions are out of date.

    Returns `None` when the table does not exist, so a database opened by an
    older build -- or a caller reading before the migration has run -- reads
    as "nothing superseded" rather than raising.
    """
    try:
        row = connection.execute(
            "SELECT campaign_id, superseded_at, reason FROM campaign_analysis_state "
            "WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return SupersededAnalysis(
        campaign_id=str(row["campaign_id"]),
        superseded_at=str(row["superseded_at"]),
        reason=str(row["reason"]),
    )


def mark_analysis_superseded(
    connection: sqlite3.Connection,
    *,
    campaign_id: str,
    reason: str,
    assignment_id: int | None = None,
) -> None:
    """Say that this campaign's stored conclusions predate its membership.

    Written in the same transaction as the membership change, so there is no
    instant in which the two disagree.
    """
    connection.execute(
        "INSERT OR REPLACE INTO campaign_analysis_state("
        "campaign_id, superseded_at, reason, superseded_by_assignment_id) "
        "VALUES (?, ?, ?, ?)",
        (campaign_id, datetime.now(UTC).isoformat(), reason, assignment_id),
    )


def clear_analysis_superseded(connection: sqlite3.Connection, campaign_id: str) -> None:
    """Drop the mark, because a fresh solve has just replaced the conclusions.

    Tolerant of a database whose migration has not run: there is then no mark
    to clear, and a solve must not fail over that.
    """
    try:
        connection.execute(
            "DELETE FROM campaign_analysis_state WHERE campaign_id = ?", (campaign_id,)
        )
    except sqlite3.OperationalError:
        return


def _gain_warnings(candidates: list[RunCandidate], *, target_label: str) -> list[str]:
    """Everything about the moved runs that stays visible rather than inferred."""
    warnings: list[str] = []
    gains = {c.gain for c in candidates if c.gain is not None}
    if len(gains) > 1:
        rendered = ", ".join(f"{gain:g}" for gain in sorted(gains))
        warnings.append(
            f"mixed gain across the selected runs ({rendered}). Levels recorded at "
            f"different gain are not on one scale, so {target_label}'s reference gain "
            "will be the mode and the rest will be flagged. Nothing is corrected here"
        )
    missing = [c.survey_run_id for c in candidates if c.gain is None]
    if missing:
        warnings.append(
            f"{len(missing)} run(s) have no recorded gain at all "
            f"({', '.join(sorted(missing)[:5])}"
            f"{', ...' if len(missing) > 5 else ''}). They stay 'not recorded'; "
            "no value is inferred for them"
        )
    legacy = [c.survey_run_id for c in candidates if c.gain_source == SOURCE_DECLARED_SITE_ROW]
    if legacy:
        warnings.append(
            f"{len(legacy)} run(s) rest on the mutable `sites` row for their gain "
            "rather than on their own provenance, so that number describes the "
            "profile as it stands now, not as it stood for the run"
        )
    unrecorded = [c.survey_run_id for c in candidates if c.gain_source == SOURCE_NOT_RECORDED]
    if unrecorded and not missing:
        warnings.append(f"{len(unrecorded)} run(s) carry no hardware provenance")
    undated = [c.survey_run_id for c in candidates if c.capture_start_utc is None]
    if undated:
        warnings.append(
            f"{len(undated)} selected run(s) have no capture time "
            f"({', '.join(sorted(undated)[:5])}"
            f"{', ...' if len(undated) > 5 else ''}). They order by import time in "
            "every report that sorts by when the RF was recorded"
        )
    positionless = [c.survey_run_id for c in candidates if not c.has_position]
    if positionless:
        warnings.append(
            f"{len(positionless)} selected run(s) have no position, so they "
            "contribute no measurement the solver can read as distance"
        )
    return warnings


def build_plan(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    campaign_id: str,
    database: str,
    selected: list[RunCandidate],
) -> AssignmentPlan:
    """What the assignment would do. Reads only; writes nothing, ever."""
    already = [c for c in selected if c.campaign_id == campaign_id]
    moving = [c for c in selected if c.campaign_id != campaign_id]
    solutions, plans = stored_analysis_counts(connection, campaign_id)
    plan = AssignmentPlan(
        project_id=project_id,
        campaign_id=campaign_id,
        database=database,
        selected=moving,
        already_in_target=already,
        target_run_count=campaign_run_count(connection, CampaignScope(campaign_id)),
        unassigned_run_count=campaign_run_count(connection, UNASSIGNED_ONLY),
        stored_solution_count=solutions,
        stored_plan_count=plans,
    )
    plan.warnings = _gain_warnings(moving, target_label=campaign_id)
    return plan


def validate_selection(
    connection: sqlite3.Connection,
    *,
    campaign_id: str,
    requested_ids: list[str] | None,
    found: dict[str, RunCandidate],
) -> None:
    """Refuse every selection that must not become a write.

    Called before the transaction opens and again inside it, because the
    answer can change between the two and the one that matters is the one
    taken under the write lock.
    """
    if requested_ids is not None:
        unknown = [run_id for run_id in requested_ids if run_id not in found]
        if unknown:
            raise CurationError(
                f"{len(unknown)} run id(s) are not in this database: "
                f"{', '.join(sorted(unknown))}. A run named and not found is a typo "
                "or the wrong database, and either way nothing here is what was meant"
            )
    reassigned = [
        candidate
        for candidate in found.values()
        if candidate.campaign_id is not None and candidate.campaign_id != campaign_id
    ]
    if reassigned:
        listed = ", ".join(
            f"{candidate.survey_run_id} (in {candidate.campaign_id!r})"
            for candidate in sorted(reassigned, key=lambda c: c.survey_run_id)
        )
        raise CurationError(
            f"{len(reassigned)} run(s) already belong to another campaign: {listed}. "
            "This command assigns runs that declare no campaign; it does not move a "
            "run between rounds. Moving one would change two campaigns' conclusions "
            "at once, and the round it is leaving may already have been reported on"
        )


def apply_assignment(
    connection: sqlite3.Connection,
    *,
    campaign_id: str,
    run_ids: list[str],
    reason: str,
    measurement_settings: Any = None,
) -> dict[str, Any]:
    """Move the runs and rebuild what the move invalidated, all or nothing.

    The caller opens the connection and owns it; everything here happens
    inside one `BEGIN IMMEDIATE` this function takes and either commits or
    rolls back. Nothing in it is deferred to a second statement the operator
    could be interrupted before reaching.

    The rebuild covers two populations, not one. The target gains runs, so
    its reference gain and noise floor move and every run already in it has
    to be measured against the new yardstick. The unassigned population
    loses runs, so its reference moves too -- leaving those measurements
    alone would show the legacy view drift flags computed against a
    population that no longer exists. Neither rebuild touches a stored
    solution: `geo_measurements` is derived data keyed by run, and the
    historical whole-database solves stay exactly as they were.
    """
    from dmr_iq_surveyor.geo.pipeline import materialise_within

    # Close the implicit transaction the schema work left open, so the
    # explicit one below is the only one in flight -- the shape `write_claim`
    # established for a multi-statement write.
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Re-read under the write lock. Everything checked outside it was
        # checked against a database another process could have changed since.
        found = fetch_runs(connection, run_ids)
        validate_selection(
            connection, campaign_id=campaign_id, requested_ids=run_ids, found=found
        )
        moving = sorted(
            (c for c in found.values() if c.campaign_id != campaign_id),
            key=lambda c: c.survey_run_id,
        )
        no_ops = sorted(
            c.survey_run_id for c in found.values() if c.campaign_id == campaign_id
        )
        if not moving:
            # Nothing changes, so nothing is recorded: an audit row for a
            # move that did not happen is a false entry in the one place
            # that is supposed to say what did.
            connection.commit()
            return {
                "assigned": [],
                "no_ops": no_ops,
                "measurements": None,
                "legacy_measurements": None,
            }

        assigned_at = datetime.now(UTC).isoformat()
        for candidate in moving:
            connection.execute(
                "UPDATE survey_runs SET campaign_id = ? "
                "WHERE survey_run_id = ? AND campaign_id IS NULL",
                (campaign_id, candidate.survey_run_id),
            )
        # Trust the write, not the intent: if the guarded UPDATE matched
        # fewer rows than were selected, something changed underneath and
        # the whole assignment is abandoned rather than half applied.
        landed = {
            str(row["survey_run_id"])
            for row in connection.execute(
                "SELECT survey_run_id FROM survey_runs WHERE campaign_id = ?",
                (campaign_id,),
            )
        }
        missed = [c.survey_run_id for c in moving if c.survey_run_id not in landed]
        if missed:
            raise CurationError(
                f"{len(missed)} run(s) were not assigned: {', '.join(sorted(missed))}. "
                "Their campaign changed while this command was running; nothing has "
                "been written"
            )

        last_assignment_id: int | None = None
        for candidate in moving:
            cursor = connection.execute(
                "INSERT INTO campaign_assignments("
                "survey_run_id, previous_campaign_id, campaign_id, assigned_at, "
                "reason, tool_version) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    candidate.survey_run_id,
                    candidate.campaign_id,
                    campaign_id,
                    assigned_at,
                    reason,
                    __version__,
                ),
            )
            last_assignment_id = cursor.lastrowid

        # The stored conclusions are stamped with a campaign whose membership
        # has just changed. Marked here, inside the same transaction, so the
        # mark cannot be missing on a database whose runs have already moved.
        mark_analysis_superseded(
            connection,
            campaign_id=campaign_id,
            reason=(
                f"{len(moving)} run(s) were assigned to this campaign on "
                f"{assigned_at}; the stored solve read the membership as it was "
                "before that"
            ),
            assignment_id=last_assignment_id,
        )

        rebuilt = materialise_within(
            connection, settings=measurement_settings, scope=CampaignScope(campaign_id)
        )
        legacy = materialise_within(
            connection, settings=measurement_settings, scope=UNASSIGNED_ONLY
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return {
        "assigned": [c.survey_run_id for c in moving],
        "no_ops": no_ops,
        "measurements": rebuilt,
        "legacy_measurements": legacy,
    }


__all__ = [
    "AssignmentPlan",
    "CurationError",
    "RunCandidate",
    "SupersededAnalysis",
    "apply_assignment",
    "build_plan",
    "campaign_run_count",
    "clear_analysis_superseded",
    "fetch_runs",
    "mark_analysis_superseded",
    "normalise_campaign_id",
    "select_by_time_range",
    "stored_analysis_counts",
    "superseded_analysis",
    "undated_unassigned_runs",
    "validate_selection",
]
