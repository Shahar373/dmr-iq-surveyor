"""The acceptance check PR4 owes: legacy 868 *analyses*, not just run lists.

Restoring the stop list was never the point. What went missing when
`FIELD_CAMPAIGN` was set was the transmitter work -- the modes, the credible
regions, the plan -- and a view that brings back six rows and an empty map has
not brought anything back.

The fixture is the database as the Pi holds it after the migration: 868 MHz
work recorded before campaigns existed and analysed by an UNSCOPED solve (which
is what those solves were), then the live campaign and a foreign one, each
solved within itself. The foreign round deliberately solves last and finds too
little, because that is the shape that exposed the bug these tests pin: an
overview that keeps one row per site, the most recently inserted, hands every
site to the round that found nothing and hides the ones that found something.

Two rules run through all of it:

* A stored `campaign_id IS NULL` is **not** "the unassigned runs' answer".
  There is no such thing -- `stored_campaign_id()` refuses to write one. It
  means the solve read whatever the whole file held at the time, and beside a
  campaign's own conclusions it has to say so.
* Reading is wide; writing is not. Every mutation from a historical view is
  refused, and the database file is checked byte for byte afterwards.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
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
from dmr_iq_surveyor.geo.store import connect_geo_database, latest_solutions_by_campaign
from dmr_iq_surveyor.survey.scope import (
    HISTORICAL_WHOLE_DATABASE_LABEL,
    CampaignScope,
    stored_analysis_label,
)
from dmr_iq_surveyor.web.server import create_server
from dmr_iq_surveyor.web.service import FieldService, FieldSettings
from dmr_iq_surveyor.web.viewscope import ALL_VIEW, CURRENT_VIEW, LEGACY_VIEW, ViewScope

CAMPAIGN = "2026-09-10_g4_acceptance"
FOREIGN = "2026-08_pilot"

# The two DIRECT control channels the reference snapshot actually carries. A
# transmitter on any other frequency is measured against no site at all, so
# nothing is detected and every solve returns `insufficient_evidence` -- which
# would make this whole file pass for the wrong reason.
SITE_30 = Transmitter(867_762_500.0, 32.050, 34.800, reference_level_db=30.0)
SITE_33 = Transmitter(866_712_500.0, 32.090, 34.830, reference_level_db=30.0)
TRANSMITTERS = [SITE_30, SITE_33]
SOLVED_SITE_KEYS = {"BEE00:37D:1:30", "BEE00:37D:1:33"}

# Spread widely enough that the levels constrain a position: a region with an
# area is the historical analysis this view has to bring back.
LEGACY_STOPS = [
    (32.030, 34.780), (32.070, 34.780), (32.070, 34.830),
    (32.030, 34.830), (32.050, 34.760), (32.100, 34.800),
]
CURRENT_STOPS = [(32.040, 34.790), (32.065, 34.815), (32.085, 34.845)]
# Two stops only, so this round genuinely cannot solve -- and it is written
# last, so it wins "most recently inserted" for every site.
FOREIGN_STOPS = [(32.020, 34.820), (32.095, 34.775)]

LEGACY_RUNS = {f"legacy868_{i}" for i in range(len(LEGACY_STOPS))}
CURRENT_RUNS = {f"current_{i}" for i in range(len(CURRENT_STOPS))}
FOREIGN_RUNS = {f"foreign_{i}" for i in range(len(FOREIGN_STOPS))}


@pytest.fixture(autouse=True)
def _no_real_sdr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "dmr_iq_surveyor.web.service.default_probe_runner", StubProbeRunner
    )


def _seed(connection: object, prefix: str, stops: list[tuple[float, float]],
          campaign_id: str | None) -> None:
    month = "07" if campaign_id is None else "09"
    for index, (latitude, longitude) in enumerate(stops):
        seed_run(
            connection,
            run_id=f"{prefix}_{index}",
            latitude=latitude,
            longitude=longitude,
            transmitters=TRANSMITTERS,
            site_id=f"{prefix}_stop_{index}",
            campaign_id=campaign_id,
            capture_start_utc=f"2026-{month}-01T{8 + index:02d}:00:00+00:00",
        )


@pytest.fixture(scope="module")
def migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The file after the migration, built in the order history built it.

    Module-scoped because three real solves are not cheap, and every test here
    reads it. Nothing in this file writes to it -- the mutation tests assert
    exactly that, byte for byte.
    """
    root = tmp_path_factory.mktemp("migrated")
    path = root / "field.sqlite3"

    connection = build_database(path)
    try:
        _seed(connection, "legacy868", LEGACY_STOPS, None)
    finally:
        connection.close()
    # Analysed before campaigns existed, so unscoped: the solve read the whole
    # file, and stored `campaign_id IS NULL` saying so.
    materialise_measurements(database_path=path)
    solve_all_sites(
        database_path=path, output_root=root / "out", settings=fast_solve_settings()
    )

    connection = connect_geo_database(path)
    try:
        _seed(connection, "current", CURRENT_STOPS, CAMPAIGN)
        _seed(connection, "foreign", FOREIGN_STOPS, FOREIGN)
    finally:
        connection.close()
    for campaign in (CAMPAIGN, FOREIGN):
        scope = CampaignScope(campaign)
        materialise_measurements(database_path=path, scope=scope)
        solve_all_sites(
            database_path=path, output_root=root / "out",
            settings=fast_solve_settings(), scope=scope,
        )
    return path


@pytest.fixture()
def service(migrated_database: Path, tmp_path: Path) -> FieldService:
    return FieldService(
        FieldSettings(
            database_path=migrated_database,
            output_root=tmp_path / "svc",
            recordings_dir=tmp_path / "rec",
            campaign_id=CAMPAIGN,
            project_id="p25_central_il",
            allow_capture=False,
        )
    )


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def get(self, path: str, scope: str | None = None) -> tuple[int, dict]:
        return self._call(path, scope, None, "GET")

    def post(self, path: str, body: dict, scope: str | None = None) -> tuple[int, dict]:
        return self._call(path, scope, body, "POST")

    def _call(self, path: str, scope: str | None, body: dict | None,
              method: str) -> tuple[int, dict]:
        url = self.base + path
        if scope is not None:
            url += ("&" if "?" in path else "?") + urllib.parse.urlencode({"scope": scope})
        request = urllib.request.Request(
            url, method=method,
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
def client(migrated_database: Path, tmp_path: Path) -> Iterator[Client]:
    settings = FieldSettings(
        database_path=migrated_database,
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


def _features(collection: dict, kind: str) -> list[dict]:
    return [
        feature["properties"]
        for feature in collection["features"]
        if feature["properties"].get("kind") == kind
    ]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- the fixture is worth what it claims ------------------------------------


def test_the_historical_round_really_did_produce_transmitter_analyses(
    migrated_database: Path,
) -> None:
    """Guards every assertion below. If the 868 work had solved to nothing,
    "legacy shows no analyses" and "legacy hides the analyses" would look the
    same, and this file would pass while proving nothing."""
    connection = sqlite3.connect(migrated_database)
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT s.site_key, g.detection_count, g.mode_latitude, g.area_km2_90
                FROM geo_solutions g JOIN p25_sites s ON s.p25_site_id = g.p25_site_id
                WHERE g.campaign_id IS NULL AND g.mode_latitude IS NOT NULL
                """
            )
        ]
    finally:
        connection.close()
    assert {row["site_key"] for row in rows} == SOLVED_SITE_KEYS
    assert all(row["detection_count"] >= 2 for row in rows)
    assert all(row["area_km2_90"] and row["area_km2_90"] > 0 for row in rows)


# -- 1-2. what each view returns, analyses included -------------------------


def test_legacy_returns_the_historical_transmitter_analyses_not_only_the_runs(
    service: FieldService,
) -> None:
    collection = service.geojson(LEGACY_VIEW)
    estimates = _features(collection, "estimate")
    regions = _features(collection, "credible_region")

    assert {estimate["site_key"] for estimate in estimates} == SOLVED_SITE_KEYS
    assert {region["site_key"] for region in regions} == SOLVED_SITE_KEYS
    assert regions, "the historical credible regions must come back, not just points"
    # And the runs, and their measurements, as before.
    assert {stop["survey_run_id"] for stop in service.stops(LEGACY_VIEW)} == LEGACY_RUNS
    assert {
        measurement["survey_run_id"]
        for measurement in _features(collection, "measurement")
    } == LEGACY_RUNS


def test_current_shows_only_the_active_campaigns_results(service: FieldService) -> None:
    collection = service.geojson(CURRENT_VIEW)
    labels = {
        estimate["analysis_label"] for estimate in _features(collection, "estimate")
    }
    assert labels == {f"Campaign {CAMPAIGN}"}
    assert HISTORICAL_WHOLE_DATABASE_LABEL not in labels
    assert service.plan(CURRENT_VIEW)["campaign_id"] == CAMPAIGN
    assert {
        site["solution_analysis_label"] for site in service.sites_overview(CURRENT_VIEW)
    } == {f"Campaign {CAMPAIGN}"}


# -- 3. the unattributable analysis is named, never passed off as the view's --


def test_a_historical_analysis_is_marked_and_never_shown_as_legacys_own(
    service: FieldService, client: Client
) -> None:
    """`campaign_id IS NULL` means the solve read the whole database. Under
    the legacy view that is the answer worth showing -- and the one thing it
    must not be allowed to look like is an answer drawn from these stops."""
    plan = service.plan(LEGACY_VIEW)
    assert plan["status"] != "none"
    assert plan["campaign_id"] is None
    assert plan["unscoped_solve"] is True
    assert plan["analysis_label"] == HISTORICAL_WHOLE_DATABASE_LABEL

    for site in service.sites_overview(LEGACY_VIEW):
        assert site["solution_campaign_id"] is None
        assert site["solution_analysis_label"] == HISTORICAL_WHOLE_DATABASE_LABEL

    collection = service.geojson(LEGACY_VIEW)
    for kind in ("estimate", "credible_region"):
        properties = _features(collection, kind)
        assert properties, kind
        assert {item["analysis_label"] for item in properties} == {
            HISTORICAL_WHOLE_DATABASE_LABEL
        }
        assert {item["campaign_id"] for item in properties} == {None}

    # And over HTTP, which is what the page actually reads.
    _, state = client.get("/api/state", "legacy")
    assert state["plan"]["analysis_label"] == HISTORICAL_WHOLE_DATABASE_LABEL


def test_the_label_is_one_phrase_everywhere_it_appears(service: FieldService) -> None:
    assert stored_analysis_label(None) == HISTORICAL_WHOLE_DATABASE_LABEL
    assert stored_analysis_label("day1") == "Campaign day1"
    collection = service.geojson(LEGACY_VIEW)
    everywhere = (
        {site["solution_analysis_label"] for site in service.sites_overview(LEGACY_VIEW)}
        | {item["analysis_label"] for item in _features(collection, "estimate")}
        | {item["analysis_label"] for item in _features(collection, "credible_region")}
        | {service.plan(LEGACY_VIEW)["analysis_label"]}
    )
    assert everywhere == {HISTORICAL_WHOLE_DATABASE_LABEL}


# -- 4. `all` is a grouped overview, and groups rather than picking a winner --


def test_all_keeps_every_rounds_analysis_instead_of_the_newest_one(
    service: FieldService,
) -> None:
    """The regression this fixture is built around.

    The foreign round solved last and found too little. Keeping one solution
    per site -- the most recently inserted -- therefore handed every site to
    it, and the historical 868 analyses and the live campaign's both vanished
    from the overview behind an `insufficient_evidence` verdict.
    """
    collection = service.geojson(ALL_VIEW)
    labels = {
        estimate["analysis_label"] for estimate in _features(collection, "estimate")
    }
    assert HISTORICAL_WHOLE_DATABASE_LABEL in labels
    assert f"Campaign {CAMPAIGN}" in labels
    assert len(_features(collection, "credible_region")) >= 4

    by_site = {site["site_key"]: site for site in service.sites_overview(ALL_VIEW)}
    for site_key in SOLVED_SITE_KEYS:
        rounds = {entry["analysis_label"] for entry in by_site[site_key]["solutions"]}
        assert rounds == {
            HISTORICAL_WHOLE_DATABASE_LABEL,
            f"Campaign {CAMPAIGN}",
            f"Campaign {FOREIGN}",
        }, site_key
        historical = next(
            entry
            for entry in by_site[site_key]["solutions"]
            if entry["campaign_id"] is None
        )
        assert historical["mode_latitude"] is not None
        assert historical["area_km2_90"] > 0


def test_only_all_groups_by_campaign(service: FieldService) -> None:
    """Grouping is what an overview needs and what a single round must not
    get: one campaign's view listing three rounds' answers would invent a
    comparison nobody asked for."""
    for view in (CURRENT_VIEW, LEGACY_VIEW, ViewScope("campaign", FOREIGN)):
        assert all(site["solutions"] == [] for site in service.sites_overview(view))
    assert any(site["solutions"] for site in service.sites_overview(ALL_VIEW))


def test_all_offers_no_shared_plan_and_says_why(service: FieldService) -> None:
    """A next-stop plan is one round's advice computed from one round's
    evidence. Across rounds there is none, and offering the newest one would
    be exactly the shared aggregation an overview must not do."""
    plan = service.plan(ALL_VIEW)
    assert plan["status"] == "none"
    assert plan["plan"] == {}
    assert plan["geojson"]["features"] == []
    assert "overview" in plan["reason"] and "one round" in plan["reason"]
    # Specifically: not the foreign round's plan, which is the newest row.
    assert plan["campaign_id"] is None
    assert plan["analysis_label"] is None
    assert not _features(service.geojson(ALL_VIEW), "plan_stop")


def test_grouping_reads_stored_rows_and_never_recombines_them(
    migrated_database: Path,
) -> None:
    """`latest_solutions_by_campaign` is a query, not an analysis: one stored
    row per (site, round), no averaging, no re-solve, no shared gain."""
    connection = connect_geo_database(migrated_database)
    try:
        rows = latest_solutions_by_campaign(connection)
        stored = {
            (row["p25_site_id"], row["campaign_id"]): row["geo_solution_id"]
            for row in connection.execute(
                "SELECT p25_site_id, campaign_id, MAX(geo_solution_id) AS geo_solution_id "
                "FROM geo_solutions GROUP BY p25_site_id, campaign_id"
            )
        }
    finally:
        connection.close()
    seen = defaultdict(list)
    for row in rows:
        seen[(row["p25_site_id"], row["campaign_id"])].append(row["geo_solution_id"])
    assert all(len(ids) == 1 for ids in seen.values()), "one row per site per round"
    assert {key: ids[0] for key, ids in seen.items()} == stored


# -- 5. every source moves together --------------------------------------


def test_switching_scope_moves_map_tables_and_summaries_together(
    client: Client,
) -> None:
    expected = {
        "current": (CURRENT_RUNS, f"Campaign {CAMPAIGN}"),
        "legacy": (LEGACY_RUNS, HISTORICAL_WHOLE_DATABASE_LABEL),
    }
    for scope, (runs, label) in expected.items():
        status, state = client.get("/api/state", scope)
        assert status == 200
        assert {stop["survey_run_id"] for stop in state["stops"]} == runs
        assert {run["survey_run_id"] for run in state["runs"]} == runs
        assert state["plan"]["analysis_label"] == label
        assert {
            site["solution_analysis_label"] for site in state["sites"]
        } == {label}

        status, collection = client.get("/api/geojson", scope)
        assert status == 200
        assert {
            item["survey_run_id"] for item in _features(collection, "measurement")
        } == runs
        assert {item["analysis_label"] for item in _features(collection, "estimate")} == {
            label
        }

        status, stops = client.get("/api/stops", scope)
        assert status == 200
        assert {stop["survey_run_id"] for stop in stops["stops"]} == runs

        status, _ = client.get("/api/export?format=geojson", scope)
        assert status == 200


# -- 6. reading is wide, writing is not ------------------------------------


def test_no_mutation_from_legacy_or_all_changes_one_byte_of_the_database(
    client: Client, migrated_database: Path
) -> None:
    before = _digest(migrated_database)
    run_id = sorted(LEGACY_RUNS)[0]
    attempts = [
        (f"/api/stops/{run_id}/exclude", {"reason": "no"}),
        (f"/api/stops/{run_id}/include", {}),
        (f"/api/stops/{run_id}/delete", {}),
        (f"/api/stops/{sorted(CURRENT_RUNS)[0]}/delete", {}),
        ("/api/solve", {"rebuild_measurements": True}),
        ("/api/capture", {"label": "x"}),
        ("/api/live/start", {}),
        ("/api/live/solve", {}),
        ("/api/live/hold", {"seconds": 60}),
        ("/api/recordings/purge", {}),
        ("/api/position", {"latitude": 32.0, "longitude": 34.8, "source": "manual"}),
    ]
    for path, body in attempts:
        for scope in ("legacy", "all"):
            status, payload = client.post(path, body, scope)
            assert status == 409, (path, scope, status, payload)
            assert "read-only" in payload["error"]
    assert _digest(migrated_database) == before

    # And the historical analyses are still all there afterwards.
    _, collection = client.get("/api/geojson", "legacy")
    assert {item["site_key"] for item in _features(collection, "estimate")} == (
        SOLVED_SITE_KEYS
    )
