"""Two collection rounds in one database, and the boundary between them.

The failure this file exists to prevent is quiet: a second round recorded
into the same file drags the first round's evidence into its reference gain,
its noise floor, its solve and its plan, and nothing in the output says so.
Every test below seeds ONE database holding two campaigns whose runs
deliberately disagree, then asserts that a scoped analysis sees only its own.

`day1` and `day2` are driven from different places on purpose, so a solution
built from both is arithmetically distinguishable from one built from either.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fixtures.geo_scenario import (
    Transmitter,
    build_database,
    fast_solve_settings,
    seed_run,
)

from dmr_iq_surveyor.geo.pipeline import (
    build_map_geojson,
    materialise_measurements,
    site_overview,
    solve_all_sites,
)
from dmr_iq_surveyor.geo.store import connect_geo_database, latest_plan, solution_history
from dmr_iq_surveyor.survey.provenance import (
    SOURCE_APPLIED,
    SOURCE_DECLARED_SITE_ROW,
    hardware_provenance,
    requested_bucket,
)
from dmr_iq_surveyor.survey.scope import (
    WHOLE_DATABASE,
    CampaignScope,
    CampaignScopeError,
    resolve_scope,
)

# The site-30 control channel from the shared fixture's registry, so its
# measurements attribute to a real site rather than an unknown frequency.
TRANSMITTER = Transmitter(
    867_762_500.0, 32.050, 34.800, reference_level_db=25.0, path_loss_exponent=3.4
)

# Two rounds along different stretches, both within range of the transmitter
# so each has real evidence of its own -- the point is that the evidence is
# different, not that one round has none.
DAY1_STOPS = [(32.070, 34.770), (32.075, 34.775), (32.080, 34.790), (32.085, 34.795)]
DAY2_STOPS = [(32.030, 34.820), (32.035, 34.825), (32.040, 34.830)]


def _applied(gain: float) -> dict[str, Any]:
    """A run whose radio reported its gain back."""
    return hardware_provenance(
        applied={"gains": {"IFGR": gain, "RFGR": 2.0}},
        requested=requested_bucket(if_gain_reduction_db=gain, lna_state=2),
    )


def _two_campaigns(tmp_path: Path, *, day2_gain: float = 20.0) -> Path:
    """One database, two rounds, recorded at deliberately different gains.

    `day1` is four stops at 40 dB IFGR; `day2` is three stops at another
    gain entirely. Unscoped, the modal reference gain is `day1`'s and every
    `day2` stop is flagged as drifted -- which is exactly right for a single
    campaign and exactly wrong once these are two.
    """
    path = tmp_path / "two.sqlite3"
    connection = build_database(path)
    try:
        for index, (latitude, longitude) in enumerate(DAY1_STOPS):
            seed_run(
                connection,
                run_id=f"day1_{index}",
                latitude=latitude,
                longitude=longitude,
                transmitters=[TRANSMITTER],
                site_id=f"day1_stop_{index}",
                gain=40.0,
                campaign_id="day1",
                hardware=_applied(40.0),
            )
        for index, (latitude, longitude) in enumerate(DAY2_STOPS):
            seed_run(
                connection,
                run_id=f"day2_{index}",
                latitude=latitude,
                longitude=longitude,
                transmitters=[TRANSMITTER],
                site_id=f"day2_stop_{index}",
                gain=day2_gain,
                campaign_id="day2",
                hardware=_applied(day2_gain),
            )
    finally:
        connection.close()
    return path


def _legacy_run(path: Path, run_id: str = "legacy") -> None:
    """One stop from before campaigns existed: `campaign_id IS NULL`."""
    connection = connect_geo_database(path)
    try:
        seed_run(
            connection,
            run_id=run_id,
            latitude=32.060,
            longitude=34.760,
            transmitters=[TRANSMITTER],
            site_id="legacy_stop",
            gain=40.0,
            campaign_id=None,
        )
    finally:
        connection.close()


def _run_ids(path: Path, campaign: str | None) -> set[str]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        if campaign is None:
            rows = connection.execute(
                "SELECT survey_run_id FROM survey_runs WHERE campaign_id IS NULL"
            )
        else:
            rows = connection.execute(
                "SELECT survey_run_id FROM survey_runs WHERE campaign_id = ?", (campaign,)
            )
        return {str(row["survey_run_id"]) for row in rows}
    finally:
        connection.close()


# -- the scope itself --------------------------------------------------------


def test_no_campaign_produces_no_predicate_at_all() -> None:
    """The backward-compatibility contract, at its source: an unscoped query
    is byte-for-byte the query it was before this existed."""
    assert WHOLE_DATABASE.where("r") == ("", ())
    assert WHOLE_DATABASE.is_whole_database
    assert WHOLE_DATABASE.run_ids.__doc__  # documented as "no filter", not "every id"


def test_a_campaign_id_is_validated_the_way_one_is_written() -> None:
    """`--campaign Day1` has to select the runs stored as `day1`, or it
    selects nothing and reports an empty round as if it were the truth."""
    assert resolve_scope("  Day1 ").campaign_id == "day1"
    assert resolve_scope(None).is_whole_database
    with pytest.raises(CampaignScopeError):
        resolve_scope("Day One")


def test_naming_a_run_outside_the_campaign_is_refused_not_dropped(tmp_path: Path) -> None:
    """A rebuild that silently skipped a stop the operator named would report
    success over work it never did."""
    path = _two_campaigns(tmp_path)
    connection = connect_geo_database(path)
    try:
        scope = CampaignScope("day1")
        assert scope.narrow(connection, ["day1_0"]) == ["day1_0"]
        with pytest.raises(CampaignScopeError, match="not in campaign 'day1'"):
            scope.narrow(connection, ["day1_0", "day2_0"])
    finally:
        connection.close()


# -- the derived values ------------------------------------------------------


def test_each_campaign_gets_its_own_reference_gain(tmp_path: Path) -> None:
    """The whole point. Unscoped, one round's gain is the reference and the
    other round is flagged as drifted against it; scoped, each round is its
    own reference and neither is flagged."""
    path = _two_campaigns(tmp_path, day2_gain=20.0)

    whole = materialise_measurements(database_path=path)
    assert whole["reference_gain"] == 40.0
    assert sorted(whole["gain_drift_runs"]) == sorted(_run_ids(path, "day2"))

    first = materialise_measurements(database_path=path, scope=CampaignScope("day1"))
    assert first["reference_gain"] == 40.0
    assert first["gain_drift_runs"] == []
    assert set(run["survey_run_id"] for run in first["runs"]) == _run_ids(path, "day1")

    second = materialise_measurements(database_path=path, scope=CampaignScope("day2"))
    assert second["reference_gain"] == 20.0
    assert second["gain_drift_runs"] == []
    assert set(run["survey_run_id"] for run in second["runs"]) == _run_ids(path, "day2")


def test_each_campaign_gets_its_own_noise_floor(tmp_path: Path) -> None:
    path = _two_campaigns(tmp_path)

    first = materialise_measurements(database_path=path, scope=CampaignScope("day1"))
    second = materialise_measurements(database_path=path, scope=CampaignScope("day2"))

    assert set(first["noise_floor_by_run"]) == _run_ids(path, "day1")
    assert set(second["noise_floor_by_run"]) == _run_ids(path, "day2")
    assert first["reference_noise_floor_dbfs_per_hz"] is not None
    assert second["reference_noise_floor_dbfs_per_hz"] is not None


def test_an_unassigned_run_is_never_swept_into_a_campaign(tmp_path: Path) -> None:
    """A run written before campaigns existed was not taken under this round.
    Nobody declared that it was, so it is excluded rather than included by
    default -- which is the direction that cannot be undone by inspection."""
    path = _two_campaigns(tmp_path)
    _legacy_run(path)

    scoped = materialise_measurements(database_path=path, scope=CampaignScope("day1"))
    assert "legacy" not in {run["survey_run_id"] for run in scoped["runs"]}
    assert "legacy" not in scoped["noise_floor_by_run"]

    whole = materialise_measurements(database_path=path)
    assert "legacy" in {run["survey_run_id"] for run in whole["runs"]}


def test_the_campaign_is_named_in_the_measurement_report(tmp_path: Path) -> None:
    path = _two_campaigns(tmp_path)

    assert materialise_measurements(database_path=path)["campaign_id"] is None
    scoped = materialise_measurements(database_path=path, scope=CampaignScope("day2"))
    assert scoped["campaign_id"] == "day2"


# -- the solve, the plan, the exports ---------------------------------------


def test_a_scoped_solve_reads_only_its_own_measurements(tmp_path: Path) -> None:
    path = _two_campaigns(tmp_path)
    materialise_measurements(database_path=path)

    report = solve_all_sites(
        database_path=path, settings=fast_solve_settings(), scope=CampaignScope("day1")
    )

    assert report["campaign_id"] == "day1"
    inputs: set[str] = set()
    for solution in report["solutions"]:
        inputs.update(solution.get("input_run_ids", []))
    assert inputs, "the scoped solve found no evidence at all"
    assert inputs <= _run_ids(path, "day1")


def test_two_campaigns_solved_separately_do_not_pool_evidence(tmp_path: Path) -> None:
    """The end-to-end no-mixing proof: each round's solve counts only its own
    stops, and the whole-database solve counts strictly more than either."""
    path = _two_campaigns(tmp_path)
    materialise_measurements(database_path=path)

    first = solve_all_sites(
        database_path=path,
        settings=fast_solve_settings(),
        solve_batch_id="b_day1",
        scope=CampaignScope("day1"),
    )
    second = solve_all_sites(
        database_path=path,
        settings=fast_solve_settings(),
        solve_batch_id="b_day2",
        scope=CampaignScope("day2"),
    )
    whole = solve_all_sites(
        database_path=path, settings=fast_solve_settings(), solve_batch_id="b_all"
    )

    def counted(report: dict[str, Any]) -> int:
        return sum(int(row["detection_count"]) for row in report["solutions"])

    assert counted(first) > 0
    assert counted(second) > 0
    assert counted(whole) == counted(first) + counted(second)
    assert first["measurement_summary"]["total"] < whole["measurement_summary"]["total"]


def test_a_solution_records_the_campaign_it_was_solved_for(tmp_path: Path) -> None:
    path = _two_campaigns(tmp_path)
    materialise_measurements(database_path=path)
    solve_all_sites(
        database_path=path,
        settings=fast_solve_settings(),
        solve_batch_id="scoped",
        scope=CampaignScope("day1"),
    )
    solve_all_sites(
        database_path=path, settings=fast_solve_settings(), solve_batch_id="unscoped"
    )

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        stamped = {
            str(row["solve_batch_id"]): row["campaign_id"]
            for row in connection.execute(
                "SELECT DISTINCT solve_batch_id, campaign_id FROM geo_solutions"
            )
        }
    finally:
        connection.close()
    assert stamped["scoped"] == "day1"
    # An unscoped solve read every run in the file. Labelling it afterwards
    # would claim a boundary it never applied.
    assert stamped["unscoped"] is None


def test_history_shows_only_this_campaigns_solves(tmp_path: Path) -> None:
    path = _two_campaigns(tmp_path)
    materialise_measurements(database_path=path)
    for batch, scope in (
        ("b_day1", CampaignScope("day1")),
        ("b_day2", CampaignScope("day2")),
        ("b_all", WHOLE_DATABASE),
    ):
        solve_all_sites(
            database_path=path,
            settings=fast_solve_settings(),
            solve_batch_id=batch,
            scope=scope,
        )

    connection = connect_geo_database(path)
    try:
        site_id = int(
            connection.execute("SELECT p25_site_id FROM p25_sites LIMIT 1").fetchone()[0]
        )
        scoped = solution_history(connection, site_id, scope=CampaignScope("day1"))
        every = solution_history(connection, site_id)
    finally:
        connection.close()

    assert {entry["solve_batch_id"] for entry in scoped} == {"b_day1"}
    assert {entry["solve_batch_id"] for entry in every} == {"b_day1", "b_day2", "b_all"}


def test_the_plan_offered_for_a_campaign_is_that_campaigns_own(tmp_path: Path) -> None:
    """A plan from an unscoped solve was computed from every run in the file.
    Offering it as one campaign's next stop would be the mixing this whole
    boundary exists to stop."""
    path = _two_campaigns(tmp_path)
    materialise_measurements(database_path=path)
    solve_all_sites(
        database_path=path, settings=fast_solve_settings(), solve_batch_id="b_all"
    )

    connection = connect_geo_database(path)
    try:
        assert latest_plan(connection) is not None
        assert latest_plan(connection, scope=CampaignScope("day1")) is None
    finally:
        connection.close()

    solve_all_sites(
        database_path=path,
        settings=fast_solve_settings(),
        solve_batch_id="b_day1",
        scope=CampaignScope("day1"),
    )
    connection = connect_geo_database(path)
    try:
        plan = latest_plan(connection, scope=CampaignScope("day1"))
        assert plan is not None
        assert plan["solve_batch_id"] == "b_day1"
    finally:
        connection.close()


def test_the_overview_counts_only_this_campaigns_evidence(tmp_path: Path) -> None:
    path = _two_campaigns(tmp_path)
    materialise_measurements(database_path=path)

    whole = {row["site_key"]: row for row in site_overview(database_path=path)}
    first = {
        row["site_key"]: row
        for row in site_overview(database_path=path, scope=CampaignScope("day1"))
    }
    second = {
        row["site_key"]: row
        for row in site_overview(database_path=path, scope=CampaignScope("day2"))
    }

    # The registry is campaign-independent: a site exists whether or not this
    # round drove past it, so every site is still listed either way.
    assert set(whole) == set(first) == set(second)
    for key in whole:
        assert (
            first[key]["measurement_total"] + second[key]["measurement_total"]
            == whole[key]["measurement_total"]
        )


def test_the_geojson_export_carries_only_this_campaigns_measurements(
    tmp_path: Path,
) -> None:
    path = _two_campaigns(tmp_path)
    materialise_measurements(database_path=path)

    collection = build_map_geojson(database_path=path, scope=CampaignScope("day2"))
    runs = {
        feature["properties"]["survey_run_id"]
        for feature in collection["features"]
        if feature["properties"].get("kind") == "measurement"
    }
    assert runs
    assert runs <= _run_ids(path, "day2")


def test_a_stale_exclusion_in_another_campaign_is_not_rebuilt(tmp_path: Path) -> None:
    """The back door: the solve feeds stale runs straight into a rebuild, so
    an unscoped stale set would pull another round's runs into a scoped
    solve without ever naming them."""
    from dmr_iq_surveyor.geo.pipeline import runs_with_stale_exclusions
    from dmr_iq_surveyor.geo.store import exclude_run

    path = _two_campaigns(tmp_path)
    materialise_measurements(database_path=path)
    connection = connect_geo_database(path)
    try:
        exclude_run(connection, "day2_0", "operator judgement")
        assert "day2_0" in runs_with_stale_exclusions(connection)
        assert runs_with_stale_exclusions(connection, scope=CampaignScope("day1")) == []
        assert runs_with_stale_exclusions(connection, scope=CampaignScope("day2")) == [
            "day2_0"
        ]
    finally:
        connection.close()


# -- backward compatibility --------------------------------------------------


def test_without_a_campaign_everything_still_reads_the_whole_database(
    tmp_path: Path,
) -> None:
    """Every existing invocation, every stored report and every test from
    before this change stays true."""
    path = _two_campaigns(tmp_path)
    _legacy_run(path)

    result = materialise_measurements(database_path=path)
    every = _run_ids(path, "day1") | _run_ids(path, "day2") | {"legacy"}
    assert {run["survey_run_id"] for run in result["runs"]} == every

    report = solve_all_sites(database_path=path, settings=fast_solve_settings())
    assert report["campaign_id"] is None
    inputs: set[str] = set()
    for solution in report["solutions"]:
        inputs.update(solution.get("input_run_ids", []))
    assert inputs & _run_ids(path, "day1")
    assert inputs & _run_ids(path, "day2")


def test_a_database_written_before_these_columns_upgrades_in_place(
    tmp_path: Path,
) -> None:
    """The additive-migration contract: an existing file gains the two
    columns on next open, NULL everywhere, and behaves exactly as it did."""
    path = tmp_path / "old.sqlite3"
    connection = connect_geo_database(path)
    try:
        connection.execute("ALTER TABLE geo_solutions DROP COLUMN campaign_id")
        connection.execute("ALTER TABLE geo_plans DROP COLUMN campaign_id")
        connection.commit()
    finally:
        connection.close()

    connection = connect_geo_database(path)
    try:
        solutions = {row[1] for row in connection.execute("PRAGMA table_info(geo_solutions)")}
        plans = {row[1] for row in connection.execute("PRAGMA table_info(geo_plans)")}
    finally:
        connection.close()
    assert "campaign_id" in solutions
    assert "campaign_id" in plans


# -- the gain resolver no longer trusts the mutable site row ------------------


def test_a_runs_own_gain_beats_the_site_row_that_was_rewritten_after_it(
    tmp_path: Path,
) -> None:
    """`sites` holds one row per profile and `upsert_site` rewrites it on
    every run, so before this the drift check attributed the LAST stop's gain
    to every earlier stop that shared the profile. A run carrying its own
    provenance now answers from that, and never falls through."""
    from dmr_iq_surveyor.geo.pipeline import _campaign_gain_readings

    path = tmp_path / "shared.sqlite3"
    connection = build_database(path)
    try:
        # Both stops share ONE site profile, and the second rewrites its gain.
        seed_run(
            connection,
            run_id="first",
            latitude=32.07,
            longitude=34.77,
            transmitters=[TRANSMITTER],
            site_id="shared",
            gain=40.0,
            campaign_id="day1",
            hardware=_applied(40.0),
        )
        seed_run(
            connection,
            run_id="second",
            latitude=32.08,
            longitude=34.78,
            transmitters=[TRANSMITTER],
            site_id="shared",
            gain=20.0,
            campaign_id="day1",
            hardware=_applied(20.0),
        )
        readings = _campaign_gain_readings(connection, ["first", "second"])
        stored = connection.execute(
            "SELECT gain FROM sites WHERE site_id = 'shared'"
        ).fetchone()[0]
    finally:
        connection.close()

    # The mutable row now says 20; the first run still says 40, from itself.
    assert stored == 20.0
    assert readings["first"].value == 40.0
    assert readings["first"].source == SOURCE_APPLIED
    assert readings["second"].value == 20.0


def test_a_run_with_no_provenance_still_falls_back_and_says_so(
    tmp_path: Path,
) -> None:
    """The legacy tier survives -- a row written before runs carried their own
    declaration has nothing else -- but it is labelled apart, so a reference
    gain resting on it is never mistaken for measured fact."""
    from dmr_iq_surveyor.geo.pipeline import _campaign_gain_readings, _campaign_gain_sources

    path = tmp_path / "legacy.sqlite3"
    connection = build_database(path)
    try:
        seed_run(
            connection,
            run_id="old",
            latitude=32.07,
            longitude=34.77,
            transmitters=[TRANSMITTER],
            site_id="old_stop",
            gain=40.0,
            campaign_id=None,
            hardware=None,
        )
        readings = _campaign_gain_readings(connection, ["old"])
    finally:
        connection.close()

    assert readings["old"].value == 40.0
    assert readings["old"].source == SOURCE_DECLARED_SITE_ROW
    assert _campaign_gain_sources(readings) == {SOURCE_DECLARED_SITE_ROW: 1}


def test_the_measurement_report_says_where_its_reference_gain_came_from(
    tmp_path: Path,
) -> None:
    path = _two_campaigns(tmp_path)

    report = materialise_measurements(database_path=path, scope=CampaignScope("day1"))

    assert report["reference_gain_sources"] == {SOURCE_APPLIED: len(DAY1_STOPS)}
    # Serialisable: this goes into the run report on disk.
    json.dumps(report["reference_gain_sources"])


# -- the two bugs a self-review caught --------------------------------------


def test_supersede_normalises_the_campaign_before_comparing(tmp_path: Path) -> None:
    """`LiveSettings` holds what the operator typed -- `Day1` -- while the
    store writes `day1`. Comparing the two directly found no match and
    silently superseded nothing, leaving both passes of a re-driven road
    counting: the double evidence supersede exists to prevent.

    The settings are deliberately NOT rewritten (a CLI-wiring test asserts
    the raw value reaches the session); the normalisation belongs at the
    comparison.
    """
    from dmr_iq_surveyor.geo.store import run_exclusion
    from dmr_iq_surveyor.live.session import _supersede_earlier_bins

    path = tmp_path / "drive.sqlite3"
    connection = build_database(path)
    try:
        for run_id in ("live_a_b_1", "live_a_b_2"):
            seed_run(
                connection,
                run_id=run_id,
                latitude=32.07,
                longitude=34.77,
                transmitters=[TRANSMITTER],
                site_id="mobile",
                campaign_id="day1",
            )
        # The un-normalised form an operator types, against runs stored
        # normalised.
        superseded = _supersede_earlier_bins(
            connection, "live_a_b", "live_a_b_2", "  Day1 "
        )
        assert superseded == 1
        assert run_exclusion(connection, "live_a_b_1") is not None
    finally:
        connection.close()


def test_the_field_app_solves_within_the_campaign_it_records_under(
    tmp_path: Path,
) -> None:
    """The field app records every stop under one campaign and solves after
    each one. If those two disagreed it would solve across every round in the
    file and stamp the result with no campaign -- and the scoped readers,
    which refuse an unstamped solve precisely because it was computed from
    everything, would report the Pi as having solved nothing."""
    from dmr_iq_surveyor.web.service import FieldService, FieldSettings

    path = _two_campaigns(tmp_path)
    materialise_measurements(database_path=path)

    service = FieldService(
        FieldSettings(database_path=path, campaign_id="day1"), probe_runner=lambda **_: None
    )
    assert service._scope().campaign_id == "day1"

    unassigned = FieldService(
        FieldSettings(database_path=path), probe_runner=lambda **_: None
    )
    assert unassigned._scope().is_whole_database


def test_a_gain_that_is_not_a_number_reads_as_unknown_not_a_crash(
    tmp_path: Path,
) -> None:
    """`sites.gain` was a REAL column, so a gain was a float by construction.
    A provenance blob is hand-editable and the validator permits any scalar,
    so one malformed blob must not take down a rebuild of every other run."""
    from dmr_iq_surveyor.geo.pipeline import _as_gain

    assert _as_gain(40.0) == 40.0
    assert _as_gain("40") == 40.0
    assert _as_gain(None) is None
    assert _as_gain("forty") is None
    assert _as_gain(True) is None
    assert _as_gain(float("nan")) is None
    assert _as_gain(float("inf")) is None


def test_two_scoped_solves_in_the_same_second_do_not_collide(tmp_path: Path) -> None:
    """`geo solve --campaign day1 && geo solve --campaign day2` is a natural
    pair to run back to back, and `geo_plans.solve_batch_id` is the primary
    key -- a bare timestamp would let the second replace the first's plan."""
    path = _two_campaigns(tmp_path)
    materialise_measurements(database_path=path)

    first = solve_all_sites(
        database_path=path, settings=fast_solve_settings(), scope=CampaignScope("day1")
    )
    second = solve_all_sites(
        database_path=path, settings=fast_solve_settings(), scope=CampaignScope("day2")
    )

    assert first["solve_batch_id"] != second["solve_batch_id"]
    assert first["solve_batch_id"].endswith("day1")

    connection = connect_geo_database(path)
    try:
        assert latest_plan(connection, scope=CampaignScope("day1")) is not None
        assert latest_plan(connection, scope=CampaignScope("day2")) is not None
    finally:
        connection.close()
