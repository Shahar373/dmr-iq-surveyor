"""Reading an earlier round without being able to write into it.

PR3 used one campaign id for two jobs: the id stamped onto new stops, and the
filter on every read the field app made. Naming a campaign therefore hid every
run recorded before campaigns existed -- 51 of them on the real Pi -- behind an
empty map and the words "No stops recorded yet."

Nothing had been deleted. `campaign_id = 'g4'` is simply never true for a row
holding `NULL`, and there was no way to ask for those rows at all: `None`
already meant "the whole database". These tests pin the separation that fixes
it, and the safety rails that come with it -- what may be *seen* is now wider
than what may be *changed*, and every test below that widens one checks that
it did not widen the other.

One fixture serves all of them: three unassigned runs, two in the campaign
being recorded, two in an earlier campaign, and a solve of its own for each
of the three groups so that solutions, plans and measurements exist on every
side of every boundary.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixtures.device_probe import StubProbeRunner
from fixtures.geo_scenario import (
    Transmitter,
    build_database,
    fast_solve_settings,
    seed_run,
)

from dmr_iq_surveyor.geo.pipeline import materialise_measurements, solve_all_sites
from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.survey.scope import (
    UNASSIGNED_ONLY,
    WHOLE_DATABASE,
    CampaignScope,
    CampaignScopeError,
)
from dmr_iq_surveyor.web.server import create_server
from dmr_iq_surveyor.web.service import FieldService, FieldSettings
from dmr_iq_surveyor.web.viewscope import (
    ALL_VIEW,
    CURRENT_VIEW,
    LEGACY_VIEW,
    ReadOnlyViewError,
    ViewScope,
    ViewScopeError,
    parse_view_scope,
)

CAMPAIGN = "2026-09-10_g4_acceptance"
OTHER = "2026-08_pilot"

SITE = Transmitter(867_762_500.0, 32.050, 34.800, reference_level_db=25.0)

# Deliberately spread out, so a solve of any one group is a real solve with a
# geometry of its own rather than three stops in a car park.
LEGACY_STOPS = [(32.045, 34.795), (32.056, 34.806), (32.041, 34.809)]
CURRENT_STOPS = [(32.020, 34.760), (32.085, 34.770)]
OTHER_STOPS = [(31.950, 34.700), (32.200, 34.950)]

LEGACY_RUNS = {f"legacy_{index}" for index in range(len(LEGACY_STOPS))}
CURRENT_RUNS = {f"current_{index}" for index in range(len(CURRENT_STOPS))}
OTHER_RUNS = {f"other_{index}" for index in range(len(OTHER_STOPS))}


@pytest.fixture(autouse=True)
def _no_real_sdr(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here probes, opens or depends on real hardware."""
    monkeypatch.setattr(
        "dmr_iq_surveyor.web.service.default_probe_runner", StubProbeRunner
    )


def _seed(connection: object, prefix: str, stops: list[tuple[float, float]],
          campaign_id: str | None) -> None:
    for index, (latitude, longitude) in enumerate(stops):
        seed_run(
            connection,
            run_id=f"{prefix}_{index}",
            latitude=latitude,
            longitude=longitude,
            transmitters=[SITE],
            site_id=f"{prefix}_stop_{index}",
            campaign_id=campaign_id,
            capture_start_utc=f"2026-08-01T{8 + index:02d}:00:00+00:00",
        )


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    """Three rounds in one file, each with measurements, a solve and a plan.

    The unassigned group is solved *unscoped*, which is what actually
    happened historically: those solves ran before campaigns existed, so they
    carry `campaign_id IS NULL` because they read the whole file.
    """
    path = tmp_path / "field.sqlite3"
    connection = build_database(path)
    try:
        _seed(connection, "legacy", LEGACY_STOPS, None)
    finally:
        connection.close()

    # The historical state, reproduced in order: solve first, with nothing but
    # the unassigned runs in the file, then add the later rounds.
    materialise_measurements(database_path=path)
    solve_all_sites(
        database_path=path, output_root=tmp_path / "out", settings=fast_solve_settings()
    )

    connection = connect_geo_database(path)
    try:
        _seed(connection, "current", CURRENT_STOPS, CAMPAIGN)
        _seed(connection, "other", OTHER_STOPS, OTHER)
    finally:
        connection.close()
    for campaign in (CAMPAIGN, OTHER):
        scope = CampaignScope(campaign)
        materialise_measurements(database_path=path, scope=scope)
        solve_all_sites(
            database_path=path,
            output_root=tmp_path / "out",
            settings=fast_solve_settings(),
            scope=scope,
        )
    return path


@pytest.fixture()
def service(database: Path, tmp_path: Path) -> FieldService:
    return FieldService(
        FieldSettings(
            database_path=database,
            output_root=tmp_path / "svc",
            recordings_dir=tmp_path / "rec",
            campaign_id=CAMPAIGN,
            project_id="p25_central_il",
        )
    )


class Client:
    """Just enough HTTP to drive the real server over a real socket."""

    def __init__(self, base: str) -> None:
        self.base = base

    def get(self, path: str, scope: str | None = None) -> tuple[int, dict]:
        return self._call(path, scope, None, "GET")

    def post(self, path: str, body: dict, scope: str | None = None) -> tuple[int, dict]:
        return self._call(path, scope, body, "POST")

    def _call(
        self, path: str, scope: str | None, body: dict | None, method: str
    ) -> tuple[int, dict]:
        url = self.base + path
        if scope is not None:
            url += ("&" if "?" in path else "?") + urllib.parse.urlencode({"scope": scope})
        request = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
        )
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode()
                return response.status, json.loads(text) if text else {}
        except urllib.error.HTTPError as error:
            text = error.read().decode()
            return error.code, json.loads(text) if text else {}


@pytest.fixture()
def client(database: Path, tmp_path: Path) -> Iterator[Client]:
    settings = FieldSettings(
        database_path=database,
        output_root=tmp_path / "http",
        recordings_dir=tmp_path / "httprec",
        campaign_id=CAMPAIGN,
        project_id="p25_central_il",
        allow_capture=False,
    )
    server = create_server(settings, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield Client(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()


def _run_ids(rows: list[dict]) -> set[str]:
    return {row["survey_run_id"] for row in rows}


def _row_snapshot(path: Path) -> dict[str, list[tuple]]:
    """Every row of every table a mutation could touch, for an exact diff."""
    connection = sqlite3.connect(path)
    try:
        tables = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            table: sorted(connection.execute(f"SELECT * FROM {table}").fetchall())
            for table in tables
        }
    finally:
        connection.close()


# -- 1-3. each view shows its own rounds and nobody else's -------------------


def test_current_shows_only_the_campaign_being_recorded(service: FieldService) -> None:
    assert _run_ids(service.stops(CURRENT_VIEW)) == CURRENT_RUNS
    assert _run_ids(service.survey_runs(view=CURRENT_VIEW)) == CURRENT_RUNS


def test_legacy_shows_only_the_runs_that_carry_no_campaign(service: FieldService) -> None:
    """The regression itself. Before the split there was no way to ask for
    these at all once a campaign was named."""
    assert _run_ids(service.stops(LEGACY_VIEW)) == LEGACY_RUNS
    assert _run_ids(service.survey_runs(view=LEGACY_VIEW)) == LEGACY_RUNS


def test_legacy_uses_is_null_rather_than_equals_null(service: FieldService) -> None:
    """`= NULL` is never true for any row, so a scope written that way would
    report an empty database rather than the rows it was asked for."""
    predicate, parameters = UNASSIGNED_ONLY.where("r")
    assert predicate == "r.campaign_id IS NULL"
    assert parameters == ()
    assert service.stops(LEGACY_VIEW), "IS NULL must actually match the legacy rows"


def test_another_campaign_leaks_into_neither_current_nor_legacy(
    service: FieldService,
) -> None:
    current = _run_ids(service.stops(CURRENT_VIEW))
    legacy = _run_ids(service.stops(LEGACY_VIEW))
    assert not (current & OTHER_RUNS)
    assert not (legacy & OTHER_RUNS)
    assert _run_ids(service.stops(ViewScope("campaign", OTHER))) == OTHER_RUNS


def test_the_solutions_and_plan_a_view_reads_are_its_own(service: FieldService) -> None:
    """Not just the stops: a legacy view that showed this campaign's answers
    over last month's stops would be worse than showing nothing."""
    current = service.plan(CURRENT_VIEW)
    legacy = service.plan(LEGACY_VIEW)
    assert current["status"] != "none"
    assert legacy["status"] != "none"
    assert current["solve_batch_id"] != legacy["solve_batch_id"]

    def solved(view: ViewScope) -> set[str]:
        return {
            site["site_key"]
            for site in service.sites_overview(view)
            if site.get("solved_at")
        }

    assert solved(CURRENT_VIEW) and solved(LEGACY_VIEW)

    # Each answer names the round that drew it. The legacy solve was run
    # unscoped, before campaigns existed, so it carries `None` -- which means
    # "read the whole file", not "solved the unassigned runs", and the reader
    # has to be able to tell those apart.
    campaigns = {
        site["site_key"]: site["solution_campaign_id"]
        for site in service.sites_overview(CURRENT_VIEW)
        if site.get("solved_at")
    }
    assert set(campaigns.values()) == {CAMPAIGN}
    legacy_campaigns = {
        site["solution_campaign_id"]
        for site in service.sites_overview(LEGACY_VIEW)
        if site.get("solved_at")
    }
    assert legacy_campaigns == {None}


# -- 4. `all` is an overview, never a mixed analysis ------------------------


def test_all_shows_every_round_grouped_rather_than_blended(
    service: FieldService,
) -> None:
    stops = service.stops(ALL_VIEW)
    assert _run_ids(stops) == LEGACY_RUNS | CURRENT_RUNS | OTHER_RUNS
    # Every row says which round it belongs to, which is what makes grouping
    # possible at all -- an overview that could not label its own rows would
    # read as one round's work.
    by_campaign: dict[str | None, set[str]] = {}
    for stop in stops:
        by_campaign.setdefault(stop["campaign_id"], set()).add(stop["survey_run_id"])
    assert by_campaign == {None: LEGACY_RUNS, CAMPAIGN: CURRENT_RUNS, OTHER: OTHER_RUNS}


def test_all_never_runs_a_solve_across_campaigns(service: FieldService) -> None:
    """A joint solve is the thing `all` must not do. Two rounds at different
    gain are two scales on one axis; the fit absorbs the offset into the
    reference level and puts the mode somewhere neither round saw."""
    with pytest.raises(ReadOnlyViewError):
        service.start_solve({"rebuild_measurements": True}, ALL_VIEW)


def test_a_solve_scoped_to_the_unassigned_runs_refuses_to_store_itself(
    database: Path, tmp_path: Path
) -> None:
    """`geo_solutions.campaign_id IS NULL` already means "read the whole
    file". A solve of only the unassigned runs has no honest value to stamp
    there, so it refuses rather than mislabelling a stored conclusion."""
    with pytest.raises(CampaignScopeError):
        UNASSIGNED_ONLY.stored_campaign_id()
    with pytest.raises(CampaignScopeError):
        solve_all_sites(
            database_path=database,
            output_root=tmp_path / "never",
            settings=fast_solve_settings(),
            scope=UNASSIGNED_ONLY,
        )


# -- 5. capture always lands in the capture campaign -------------------------


def test_a_capture_is_written_to_the_capture_campaign_whatever_the_view(
    service: FieldService,
) -> None:
    """The write target is one value and a view cannot reach it."""
    assert service.settings.capture_campaign_id == CAMPAIGN
    for view in (CURRENT_VIEW, LEGACY_VIEW, ALL_VIEW):
        assert service._scope().campaign_id == CAMPAIGN, view
    # And the read scope really is the other thing, for the same service.
    assert service._read_scope(CURRENT_VIEW) == CampaignScope(CAMPAIGN)
    assert service._read_scope(LEGACY_VIEW) == UNASSIGNED_ONLY
    assert service._read_scope(ALL_VIEW) == WHOLE_DATABASE


def test_capture_is_refused_from_a_historical_view_rather_than_retargeted(
    service: FieldService,
) -> None:
    """Refused, not quietly written into the campaign being recorded: an
    operator who taps Record while reading last month's map has not asked for
    a stop today, they have made a mistake, and silently doing the safe thing
    would teach them the view is meaningless."""
    for view in (LEGACY_VIEW, ALL_VIEW, ViewScope("campaign", OTHER)):
        with pytest.raises(ReadOnlyViewError):
            service.start_capture({"label": "nope"}, view)


# -- 6. a mutation from a historical view changes nothing --------------------


@pytest.mark.parametrize("view", [LEGACY_VIEW, ALL_VIEW, ViewScope("campaign", OTHER)])
def test_no_mutation_from_a_historical_view_changes_a_single_row(
    service: FieldService, database: Path, view: ViewScope
) -> None:
    before = _row_snapshot(database)
    run_id = sorted(LEGACY_RUNS)[0]
    for call in (
        lambda: service.set_stop_excluded(run_id, excluded=True, view=view),
        lambda: service.set_stop_excluded(run_id, excluded=False, view=view),
        lambda: service.delete_stop(run_id, view),
        lambda: service.start_solve({"rebuild_measurements": True}, view),
        lambda: service.start_capture({"label": "x"}, view),
        lambda: service.purge(view),
    ):
        with pytest.raises(ReadOnlyViewError):
            call()
    assert _row_snapshot(database) == before


def test_deleting_a_stop_that_is_in_the_campaign_is_still_refused_from_all(
    client: Client, database: Path
) -> None:
    """The case the campaign narrowing never covered, and the reason the view
    check has to be the outer one.

    Under `all` the stop being looked at may well be in the capture campaign,
    so `narrow()` lets it through -- and it did: against the code this
    replaces, this exact request returned 200 and the run was gone. An
    operator reading last month's map has no reason to expect a tap to
    destroy this morning's evidence.
    """
    run_id = sorted(CURRENT_RUNS)[0]
    before = _row_snapshot(database)
    status, payload = client.post(f"/api/stops/{run_id}/delete", {}, "all")
    assert status == 409, payload
    assert _row_snapshot(database) == before
    assert run_id in _run_ids(client.get("/api/stops", "current")[1]["stops"])


def test_a_hold_is_a_write_and_is_refused_like_one(service: FieldService) -> None:
    """A pull-over hold is not a pause. It routes through the same close path
    a drive bin does and writes a `survey_runs` row, its observations and its
    levels, under the campaign the drive is recording into -- so it is guarded
    like every other write, not waved through as drive continuity."""
    for view in (LEGACY_VIEW, ALL_VIEW):
        with pytest.raises(ReadOnlyViewError):
            service.request_live_hold({"seconds": 60}, view)
    # Live position fixes genuinely write nothing and stay allowed, so a drive
    # already under way is not broken by a glance at history.
    assert service.push_live_position(
        {"latitude": 32.05, "longitude": 34.79, "accuracy_m": 8.0}
    )["accepted"]


def test_marking_a_position_is_refused_from_a_historical_view(
    service: FieldService, tmp_path: Path
) -> None:
    """It writes no database row, but it is the coordinate the next recording
    is filed under, and the page disables the controls -- so the server has to
    agree rather than leave a second way in."""
    marked = {"latitude": 32.0, "longitude": 34.8, "accuracy_m": 5.0, "source": "manual"}
    service.set_position(marked, CURRENT_VIEW)
    before = service.get_position()
    for view in (LEGACY_VIEW, ALL_VIEW):
        with pytest.raises(ReadOnlyViewError):
            service.set_position({**marked, "latitude": 31.0}, view)
    assert service.get_position()["latitude"] == before["latitude"]


def test_every_write_route_over_http_is_refused_from_a_historical_view(
    client: Client, database: Path
) -> None:
    """The whole POST surface, enumerated, so a route added later that writes
    evidence has to be added here or to the documented exception list."""
    before = _row_snapshot(database)
    for path, body in (
        ("/api/position", {"latitude": 32.0, "longitude": 34.8, "source": "manual"}),
        ("/api/capture", {"label": "x"}),
        ("/api/analyse", {"recording": "/nonexistent.wav"}),
        ("/api/solve", {}),
        ("/api/live/start", {}),
        ("/api/live/solve", {}),
        ("/api/live/hold", {"seconds": 60}),
        ("/api/recordings/purge", {}),
    ):
        for scope in ("legacy", "all"):
            status, payload = client.post(path, body, scope)
            assert status == 409, (path, scope, status, payload)
            assert "read-only" in payload["error"]
    assert _row_snapshot(database) == before


# -- a historical view says what it is actually showing ----------------------


def test_a_plan_from_an_unscoped_solve_is_labelled_as_one(
    service: FieldService, database: Path, tmp_path: Path
) -> None:
    """`geo_plans.campaign_id IS NULL` means "this solve read the whole
    database", not "this solve read the unassigned runs". The legacy view is
    the one place both readings meet, so the answer has to say which it is --
    otherwise a plan drawn across every round reads as the legacy stops' own.
    """
    assert service.plan(LEGACY_VIEW)["unscoped_solve"] is True
    assert service.plan(CURRENT_VIEW)["unscoped_solve"] is False
    assert service.plan(CURRENT_VIEW)["campaign_id"] == CAMPAIGN

    # And it stays true of a solve run later, over a file that by then holds
    # every round -- the case a fixture built in historical order would miss.
    solve_all_sites(
        database_path=database,
        output_root=tmp_path / "late",
        settings=fast_solve_settings(),
    )
    late = service.plan(LEGACY_VIEW)
    assert late["unscoped_solve"] is True
    assert late["campaign_id"] is None
    # The campaign's own plan is untouched by it.
    assert service.plan(CURRENT_VIEW)["campaign_id"] == CAMPAIGN


def test_a_site_solved_without_a_campaign_says_so(service: FieldService) -> None:
    legacy = [
        site for site in service.sites_overview(LEGACY_VIEW) if site.get("solved_at")
    ]
    assert legacy and all(site["solution_campaign_id"] is None for site in legacy)


def test_the_capture_campaign_still_guards_what_the_current_view_may_touch(
    service: FieldService,
) -> None:
    """Widening what is visible must not widen what is destroyable. From the
    current view -- the writable one -- another round's stop is still refused
    by the campaign narrowing, exactly as it was before view scopes existed."""
    with pytest.raises(CampaignScopeError):
        service.delete_stop(sorted(LEGACY_RUNS)[0], CURRENT_VIEW)
    with pytest.raises(CampaignScopeError):
        service.delete_stop(sorted(OTHER_RUNS)[0], CURRENT_VIEW)


# -- 7. every data source moves together -------------------------------------


def test_switching_scope_moves_every_data_source_at_once(client: Client) -> None:
    """A map still showing one scope's measurements under another scope's
    stop list is the confusion this release exists to remove."""
    seen: dict[str, set[str]] = {}
    for scope, expected in (
        ("current", CURRENT_RUNS),
        ("legacy", LEGACY_RUNS),
        (f"campaign:{OTHER}", OTHER_RUNS),
    ):
        status, state = client.get("/api/state", scope)
        assert status == 200
        assert _run_ids(state["stops"]) == expected
        assert _run_ids(state["runs"]) == expected

        status, stops = client.get("/api/stops", scope)
        assert status == 200 and _run_ids(stops["stops"]) == expected

        status, collection = client.get("/api/geojson", scope)
        assert status == 200
        measured = {
            feature["properties"]["survey_run_id"]
            for feature in collection["features"]
            if feature["properties"].get("kind") == "measurement"
        }
        assert measured <= expected
        seen[scope] = measured

        status, plan = client.get("/api/plan", scope)
        assert status == 200
        status, body = client.get("/api/export?format=geojson", scope)
        assert status == 200

    assert seen["current"] and seen["legacy"]
    assert seen["current"] != seen["legacy"]


def test_api_state_says_where_the_work_goes_and_what_is_on_screen(
    client: Client,
) -> None:
    status, state = client.get("/api/state")
    assert status == 200
    scope = state["scope"]
    assert scope["project_id"] == "p25_central_il"
    assert scope["capture_campaign_id"] == CAMPAIGN
    assert scope["view"] == "current"
    assert scope["read_only"] is False
    assert scope["has_legacy"] is True
    assert scope["counts"]["stops"] == len(CURRENT_RUNS)
    census = {entry["campaign_id"]: entry["runs"] for entry in scope["campaigns"]}
    assert census == {
        None: len(LEGACY_RUNS),
        CAMPAIGN: len(CURRENT_RUNS),
        OTHER: len(OTHER_RUNS),
    }
    # Additive: the field an existing client already reads is untouched.
    assert state["settings"]["campaign_id"] == CAMPAIGN

    status, state = client.get("/api/state", "legacy")
    assert state["scope"]["read_only"] is True
    assert state["scope"]["view"] == "legacy"
    assert state["scope"]["capture_campaign_id"] == CAMPAIGN
    assert state["scope"]["counts"]["stops"] == len(LEGACY_RUNS)


def test_a_mutation_over_http_from_a_historical_view_is_refused_with_409(
    client: Client, database: Path
) -> None:
    before = _row_snapshot(database)
    run_id = sorted(LEGACY_RUNS)[0]
    for path, body in (
        (f"/api/stops/{run_id}/exclude", {"reason": "no"}),
        (f"/api/stops/{run_id}/include", {}),
        (f"/api/stops/{run_id}/delete", {}),
        ("/api/solve", {"rebuild_measurements": True}),
        ("/api/recordings/purge", {}),
    ):
        for scope in ("legacy", "all"):
            status, payload = client.post(path, body, scope)
            assert status == 409, (path, scope, status, payload)
            assert "read-only" in payload["error"]
    assert _row_snapshot(database) == before


# -- 8. an old deployment, with no project and no campaign -------------------


def test_a_deployment_with_no_campaign_still_reads_the_whole_database(
    database: Path, tmp_path: Path
) -> None:
    """The invocation that predates campaigns entirely. `current` there is
    the whole file, exactly as it always was -- and it now has a Legacy view
    that is narrower than its default rather than wider."""
    service = FieldService(
        FieldSettings(
            database_path=database,
            output_root=tmp_path / "old",
            recordings_dir=tmp_path / "oldrec",
        )
    )
    everything = LEGACY_RUNS | CURRENT_RUNS | OTHER_RUNS
    assert _run_ids(service.stops(CURRENT_VIEW)) == everything
    assert service._read_scope(CURRENT_VIEW).where("r") == ("", ())
    assert _run_ids(service.stops(LEGACY_VIEW)) == LEGACY_RUNS
    # Nothing is read-only here: with no campaign named, the default view is
    # still the one being recorded into.
    assert CURRENT_VIEW.is_read_only is False
    assert service.state(CURRENT_VIEW)["scope"]["capture_campaign_id"] is None


def test_a_request_that_names_no_scope_behaves_exactly_as_before(
    client: Client,
) -> None:
    """Every client that predates this parameter, and every hand-typed URL."""
    bare = client.get("/api/state")[1]
    explicit = client.get("/api/state", "current")[1]
    assert _run_ids(bare["stops"]) == _run_ids(explicit["stops"]) == CURRENT_RUNS
    assert bare["scope"]["view"] == "current"


# -- 9. an unusable scope is refused in words --------------------------------


@pytest.mark.parametrize("raw", ["yesterday", "campaign:", "campaign:not valid!!", "none"])
def test_an_unusable_scope_is_refused_rather_than_guessed(
    client: Client, raw: str
) -> None:
    status, payload = client.get("/api/state", raw)
    assert status == 400
    assert raw in payload["error"] or "campaign" in payload["error"]
    with pytest.raises(ViewScopeError):
        parse_view_scope(raw)


def test_naming_the_campaign_being_recorded_is_the_current_view(
    service: FieldService,
) -> None:
    """Not a read-only copy of it: spelling out your own round must not take
    Record away from you."""
    view = service.resolve_view(f"campaign:{CAMPAIGN}")
    assert view == CURRENT_VIEW
    assert view.is_read_only is False


def test_a_campaign_id_in_a_scope_is_normalised_the_way_one_is_written(
    service: FieldService,
) -> None:
    assert service.resolve_view("campaign:  2026-08_PILOT  ") == ViewScope(
        "campaign", OTHER
    )


def test_a_scope_cannot_be_a_campaign_and_the_unassigned_at_once() -> None:
    with pytest.raises(CampaignScopeError):
        CampaignScope("day1", unassigned_only=True)


# -- 10. nothing here touches secrets, tokens or authentication --------------


def test_the_scope_parameter_changes_nothing_about_authentication(
    database: Path, tmp_path: Path
) -> None:
    """A new query parameter must not become a second way in. Every scoped
    request is authorised exactly as the unscoped one is, and the token is
    still absent from the payload."""
    settings = FieldSettings(
        database_path=database,
        output_root=tmp_path / "tok",
        recordings_dir=tmp_path / "tokrec",
        campaign_id=CAMPAIGN,
        token="s3cret",
        allow_capture=False,
    )
    server = create_server(settings, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for scope in ("legacy", "all", "current"):
            request = urllib.request.Request(f"{base}/api/state?scope={scope}")
            with pytest.raises(urllib.error.HTTPError) as unauthorised:
                urllib.request.urlopen(request, timeout=30)
            assert unauthorised.value.code == 401

            request.add_header("X-Auth-Token", "s3cret")
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode())
            assert "token" not in payload["settings"]
            assert "token" not in json.dumps(payload["scope"])
    finally:
        server.shutdown()
        server.server_close()
