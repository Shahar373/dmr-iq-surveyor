"""`web serve --project`: which database this process is allowed to open.

The field app is the one entry point that opens a database without being
asked to. Before projects it did so on the first page load, creating
whatever `--database` named. With `--project` it must do the opposite: fail
at startup, before serving anything, and leave nothing behind -- because a
database the app manufactures is a database with no history in it, and an
operator would only find that out after driving somewhere.

Every no-create test below asserts the filesystem afterwards, not the exit
code alone.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app
from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.inventory.store import connect_database
from dmr_iq_surveyor.project.binding import active_binding, clear_binding
from dmr_iq_surveyor.project.claim import write_claim
from dmr_iq_surveyor.project.manifest import (
    ProjectDefaults,
    ProjectError,
    render_project_manifest,
)
from dmr_iq_surveyor.web.service import FieldSettings

runner = CliRunner()
ANALYZER = "p25_site_geolocation"
PROJECT_ID = "p25_central_il"

BAND_YAML = """
name: web_band
label: "web test band"
start_frequency_hz: 867910000
stop_frequency_hz: 868090000
raster_spacings_hz: [12500, 6250]
"""

OTHER_BAND_YAML = """
name: other_band
label: "a different band"
start_frequency_hz: 866000000
stop_frequency_hz: 866500000
raster_spacings_hz: [12500, 6250]
"""

SITE_YAML = """
site_id: mobile
label: "Mobile receiver"
latitude: 32.05
longitude: 34.79
antenna: "whip"
receiver: "SDRplay RSP1A"
gain_mode: manual
gain: 40.0
lna_state: 2
"""


@pytest.fixture(autouse=True)
def _unbound() -> None:
    """A binding is process-wide by design, so it must not leak between tests."""
    clear_binding()
    yield
    clear_binding()


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bands = tmp_path / "config" / "bands"
    sites = tmp_path / "config" / "sites"
    bands.mkdir(parents=True)
    sites.mkdir(parents=True)
    (bands / "web_band.yaml").write_text(BAND_YAML, encoding="utf-8")
    (bands / "other_band.yaml").write_text(OTHER_BAND_YAML, encoding="utf-8")
    (sites / "mobile.yaml").write_text(SITE_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@dataclass
class Started:
    """What `serve_forever` was handed, and what was in force when it was.

    The binding is recorded *inside* the call rather than after it, because
    after it there must not be one: it is scoped to the serving, not to the
    process. Only a stand-in for the server can see the difference.
    """

    settings: list[Any] = field(default_factory=list)
    binding: list[Any] = field(default_factory=list)


@pytest.fixture()
def started(monkeypatch: pytest.MonkeyPatch) -> Started:
    """Stands in for the server. It is never really started: what is under
    test is everything that happens before it, and a test that binds a socket
    tests the socket."""
    record = Started()

    def _serve_forever(settings: Any, **kwargs: Any) -> None:
        record.settings.append(settings)
        record.binding.append(active_binding())

    monkeypatch.setattr("dmr_iq_surveyor.cli_web.serve_forever", _serve_forever)
    return record


def _claim(database: Path, project_id: str) -> None:
    connection = connect_geo_database(database)
    try:
        write_claim(connection, project_id=project_id, analyzer=ANALYZER)
    finally:
        connection.close()


def _project(
    workspace: Path,
    *,
    database: Path | None = None,
    extra: tuple[str, ...] = (),
) -> Path:
    """A created project: manifest, new claimed database, defaults."""
    manifest = workspace / "projects" / "p25" / "project.yaml"
    result = runner.invoke(
        app,
        [
            "project", "init", "--create",
            "--project-id", PROJECT_ID,
            "--label", "P25 central Israel",
            "--database", str(database or (workspace / "db.sqlite3")),
            "--manifest", str(manifest),
            "--band", "web_band",
            "--site", "mobile",
            *extra,
        ],
    )
    assert result.exit_code == 0, result.output
    clear_binding()
    return manifest


def _manifest_pointing_at(workspace: Path, database: Path, **defaults: str) -> Path:
    """A project manifest naming a database, with nothing created for it.

    `project init --create` refuses to touch a path that already exists,
    which is right but makes it useless for the cases below -- each of which
    is precisely a manifest whose database is missing, empty, foreign or not
    a database at all.
    """
    manifest = workspace / "projects" / "p25" / "project.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        render_project_manifest(
            project_id=PROJECT_ID,
            label="P25 central Israel",
            analyzer=ANALYZER,
            database=database,
            defaults=ProjectDefaults(
                **{"band": "web_band", "site": "mobile", **defaults}
            ),
        ),
        encoding="utf-8",
    )
    return manifest


def _serve(manifest: Path | None, *extra: str) -> Any:
    args = ["web", "serve", "--host", "127.0.0.1", "--port", "0", "--token", "s3cret"]
    if manifest is not None:
        args += ["--project", str(manifest)]
    return runner.invoke(app, [*args, *extra])


# -- the project reaches the running app -------------------------------------


def test_project_reaches_the_field_settings(workspace: Path, started: Started) -> None:
    manifest = _project(workspace)

    result = _serve(manifest)

    assert result.exit_code == 0, result.output
    assert started.settings, "the server was never started"
    settings = started.settings[0]
    assert settings.project_id == PROJECT_ID
    assert settings.project_root == manifest.parent
    assert settings.database_path == (workspace / "db.sqlite3").resolve()
    assert settings.band == "web_band"
    assert settings.site_profile == "mobile"


def test_the_binding_is_held_while_serving_and_dropped_afterwards(
    workspace: Path, started: Started
) -> None:
    """The binding is the enforcement; the settings only record it. It is
    scoped to the serving, so it must exist during it and not survive it."""
    manifest = _project(workspace)

    assert _serve(manifest).exit_code == 0

    held = started.binding[0]
    assert held is not None
    assert held.project_id == PROJECT_ID
    assert held.analyzer == ANALYZER
    assert held.database == (workspace / "db.sqlite3").resolve()
    assert active_binding() is None, "a binding outlived the server it was made for"


def test_without_a_project_nothing_is_bound_and_no_database_is_opened(
    workspace: Path, started: Started
) -> None:
    """Every invocation that predates projects, unchanged."""
    database = workspace / "untouched" / "db.sqlite3"

    result = _serve(
        None, "--band", "web_band", "--site", "mobile", "--database", str(database)
    )

    assert result.exit_code == 0, result.output
    assert active_binding() is None
    # `web serve` has never opened the database at startup; the first
    # /api/state does. That is unchanged here.
    assert not database.exists()


# -- precedence and conflicts ------------------------------------------------


def test_manifest_defaults_apply_when_no_flag_was_typed(
    workspace: Path, started: Started
) -> None:
    manifest = _project(workspace, extra=("--output", str(workspace / "from_manifest")))

    assert _serve(manifest).exit_code == 0

    assert started.settings[0].output_root == workspace / "from_manifest"
    assert started.settings[0].recordings_dir == workspace / "from_manifest" / "recordings"


def test_an_explicit_output_wins_over_the_manifest_silently(
    workspace: Path, started: Started
) -> None:
    manifest = _project(workspace, extra=("--output", str(workspace / "from_manifest")))

    result = _serve(manifest, "--output", str(workspace / "typed"))

    assert result.exit_code == 0, result.output
    assert started.settings[0].output_root == workspace / "typed"


def test_a_contradicting_band_is_refused_naming_both_sides(
    workspace: Path, started: Started
) -> None:
    """Levels recorded under different bands are not comparable, so this is
    the one conflict that is an error rather than an override."""
    manifest = _project(workspace)

    result = _serve(manifest, "--band", "other_band")

    assert result.exit_code == 1
    assert "other_band" in result.output and "web_band" in result.output
    assert not started.settings, "the server must not start on a band conflict"


def test_passing_the_manifests_own_band_explicitly_is_not_a_conflict(
    workspace: Path, started: Started
) -> None:
    """An identical value is agreement, not disagreement -- and a plain
    default must not be mistaken for a typed flag either way."""
    manifest = _project(workspace)

    result = _serve(manifest, "--band", "web_band")

    assert result.exit_code == 0, result.output
    assert started.settings[0].band == "web_band"


# -- no creation on a project-aware path -------------------------------------


def test_a_missing_database_fails_startup_and_creates_nothing(
    workspace: Path, started: Started
) -> None:
    missing = workspace / "nowhere" / "db.sqlite3"
    manifest = _manifest_pointing_at(workspace, missing)

    result = _serve(manifest)

    assert result.exit_code == 1
    assert not missing.exists()
    assert not missing.parent.exists(), "a directory was created for a database that is not there"
    assert not started.settings


def test_a_zero_byte_file_is_not_silently_schemad(
    workspace: Path, started: Started
) -> None:
    """The trap this whole guard exists for: an empty file opens cleanly and
    comes back a full 18-table database."""
    empty = workspace / "empty.sqlite3"
    empty.write_bytes(b"")
    manifest = _manifest_pointing_at(workspace, empty)

    result = _serve(manifest)

    assert result.exit_code == 1
    assert empty.read_bytes() == b""
    assert not started.settings


def test_a_file_that_is_not_a_database_is_refused_unchanged(
    workspace: Path, started: Started
) -> None:
    notes = workspace / "notes.txt"
    notes.write_text("these are not a database\n", encoding="utf-8")
    manifest = _manifest_pointing_at(workspace, notes)

    result = _serve(manifest)

    assert result.exit_code == 1
    assert notes.read_text(encoding="utf-8") == "these are not a database\n"
    assert not started.settings


def test_an_unclaimed_database_is_refused_and_nothing_is_written(
    workspace: Path, started: Started
) -> None:
    """A real database, full of real runs, that no project has taken on."""
    unclaimed = workspace / "unclaimed.sqlite3"
    connect_geo_database(unclaimed).close()
    manifest = _manifest_pointing_at(workspace, unclaimed)
    before = unclaimed.read_bytes()

    result = _serve(manifest)

    assert result.exit_code == 1
    assert "adopt" in result.output
    assert unclaimed.read_bytes() == before
    assert not started.settings


def test_a_database_claimed_by_another_project_is_refused(
    workspace: Path, started: Started
) -> None:
    foreign = workspace / "foreign.sqlite3"
    connection = connect_geo_database(foreign)
    try:
        write_claim(connection, project_id="vor_north", analyzer=ANALYZER)
    finally:
        connection.close()
    manifest = _manifest_pointing_at(workspace, foreign)

    result = _serve(manifest)

    assert result.exit_code == 1
    assert "vor_north" in result.output
    assert not started.settings


def test_a_directory_where_the_database_should_be_is_refused(
    workspace: Path, started: Started
) -> None:
    directory = workspace / "db_dir"
    directory.mkdir()
    manifest = _manifest_pointing_at(workspace, directory)

    result = _serve(manifest)

    assert result.exit_code == 1
    assert not started.settings


# -- an explicit --database inside a project ---------------------------------


def test_an_explicit_database_of_the_same_project_is_accepted(
    workspace: Path, started: Started
) -> None:
    """Allowed on purpose: a second database of the same project is a
    legitimate thing to serve. It is not exempt from the guard."""
    manifest = _project(workspace)
    second = workspace / "second.sqlite3"
    connection = connect_geo_database(second)
    try:
        write_claim(connection, project_id=PROJECT_ID, analyzer=ANALYZER)
    finally:
        connection.close()

    result = _serve(manifest, "--database", str(second))

    assert result.exit_code == 0, result.output
    assert started.settings[0].database_path == second.resolve()
    assert started.binding[0].database == second.resolve()


def test_an_explicit_database_of_another_project_is_refused(
    workspace: Path, started: Started
) -> None:
    manifest = _project(workspace)
    other = workspace / "other.sqlite3"
    connection = connect_geo_database(other)
    try:
        write_claim(connection, project_id="atis_lod", analyzer=ANALYZER)
    finally:
        connection.close()

    result = _serve(manifest, "--database", str(other))

    assert result.exit_code == 1
    assert "atis_lod" in result.output
    assert not started.settings


def test_an_explicit_database_with_the_right_project_but_another_analyzer_is_refused(
    workspace: Path, started: Started
) -> None:
    """The guard compares both. A database read by a different analyzer holds
    different things under the same table names."""
    manifest = _project(workspace)
    wrong = workspace / "wrong_analyzer.sqlite3"
    connection = connect_geo_database(wrong)
    try:
        write_claim(connection, project_id=PROJECT_ID, analyzer="vor_bearing")
    finally:
        connection.close()

    result = _serve(manifest, "--database", str(wrong))

    assert result.exit_code == 1
    assert "vor_bearing" in result.output
    assert not started.settings


# -- campaigns ---------------------------------------------------------------


def _campaign(manifest: Path, campaign_id: str, body: str) -> Path:
    directory = manifest.parent / "campaigns"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{campaign_id}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_campaign_without_a_manifest_is_refused_never_invented(
    workspace: Path, started: Started
) -> None:
    manifest = _project(workspace)

    result = _serve(manifest, "--campaign", "day1")

    assert result.exit_code == 1
    assert "campaign" in result.output.lower()
    assert not started.settings


def test_a_campaign_manifest_belonging_to_another_project_is_refused(
    workspace: Path, started: Started
) -> None:
    manifest = _project(workspace)
    _campaign(
        manifest,
        "day1",
        "schema_version: 1\ncampaign_id: day1\nproject_id: vor_north\nlabel: Day 1\n",
    )

    result = _serve(manifest, "--campaign", "day1")

    assert result.exit_code == 1
    assert "vor_north" in result.output
    assert not started.settings


def test_a_campaign_whose_id_disagrees_with_its_filename_is_refused(
    workspace: Path, started: Started
) -> None:
    manifest = _project(workspace)
    _campaign(
        manifest,
        "day1",
        f"schema_version: 1\ncampaign_id: day2\nproject_id: {PROJECT_ID}\nlabel: Day 2\n",
    )

    result = _serve(manifest, "--campaign", "day1")

    assert result.exit_code == 1
    assert not started.settings


def test_a_project_without_a_campaign_leaves_stops_unassigned(
    workspace: Path, started: Started
) -> None:
    manifest = _project(workspace)

    result = _serve(manifest)

    assert result.exit_code == 0, result.output
    assert started.settings[0].campaign_id is None
    assert "unassigned" in result.output


def test_a_campaign_pins_the_capture_settings_it_declares(
    workspace: Path, started: Started
) -> None:
    manifest = _project(workspace)
    _campaign(
        manifest,
        "day1",
        f"schema_version: 1\ncampaign_id: day1\nproject_id: {PROJECT_ID}\n"
        "label: Day 1\ndefaults:\n  capture:\n    center_frequency_hz: 866500000\n"
        "    sample_rate_hz: 2000000\n    duration_seconds: 60\n",
    )

    result = _serve(manifest, "--campaign", "day1")

    assert result.exit_code == 0, result.output
    assert started.settings[0].campaign_id == "day1"
    assert started.settings[0].center_frequency_hz == 866_500_000.0
    assert started.settings[0].sample_rate_hz == 2_000_000.0
    assert started.settings[0].duration_seconds == 60.0


def test_a_typed_capture_flag_wins_over_the_campaign(
    workspace: Path, started: Started
) -> None:
    manifest = _project(workspace)
    _campaign(
        manifest,
        "day1",
        f"schema_version: 1\ncampaign_id: day1\nproject_id: {PROJECT_ID}\n"
        "label: Day 1\ndefaults:\n  capture:\n    sample_rate_hz: 2000000\n",
    )

    result = _serve(manifest, "--campaign", "day1", "--sample-rate", "3000000")

    assert result.exit_code == 0, result.output
    assert started.settings[0].sample_rate_hz == 3_000_000.0


def test_a_campaign_band_beats_the_project_band(
    workspace: Path, started: Started
) -> None:
    manifest = _project(workspace)
    _campaign(
        manifest,
        "day1",
        f"schema_version: 1\ncampaign_id: day1\nproject_id: {PROJECT_ID}\n"
        "label: Day 1\ndefaults:\n  band: other_band\n",
    )

    result = _serve(manifest, "--campaign", "day1")

    assert result.exit_code == 0, result.output
    assert started.settings[0].band == "other_band"


# -- the state the app hands the phone ---------------------------------------


def test_api_state_settings_still_serialise_with_the_new_fields() -> None:
    """`to_public_dict` stringifies a fixed list of Path keys; a new one that
    is not in it makes /api/state fail to serialise."""
    with_project = FieldSettings(
        project_id=PROJECT_ID, project_root=Path("/etc/dmr-field/projects/p25")
    ).to_public_dict()

    assert with_project["project_id"] == PROJECT_ID
    assert with_project["project_root"] == "/etc/dmr-field/projects/p25"
    assert json.loads(json.dumps(with_project))["project_root"].endswith("p25")

    without = FieldSettings().to_public_dict()
    assert without["project_id"] is None
    # Not the string "None": a client reading this decides whether to show a
    # project at all, and "None" is truthy.
    assert without["project_root"] is None
    json.dumps(without)


def test_the_guard_covers_opens_the_startup_check_never_sees(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding is not a one-off startup check. While serving, any open of
    another database -- a survey, a solve, /api/state -- is refused too.

    Asserted from inside the stand-in for the server, because that is the only
    place the binding is in force: after `web serve` returns there is
    deliberately no binding left to test.
    """
    manifest = _project(workspace)
    elsewhere = workspace / "elsewhere.sqlite3"
    checked: list[str] = []

    def _while_serving(settings: Any, **kwargs: Any) -> None:
        with pytest.raises(ProjectError):
            connect_database(elsewhere)
        assert not elsewhere.exists()
        # And the bound one still opens.
        opened = connect_database(workspace / "db.sqlite3")
        try:
            assert isinstance(opened, sqlite3.Connection)
        finally:
            opened.close()
        checked.append("ok")

    monkeypatch.setattr("dmr_iq_surveyor.cli_web.serve_forever", _while_serving)

    result = _serve(manifest)

    assert result.exit_code == 0, result.output
    assert checked == ["ok"], "the assertions inside the server never ran"


# -- the binding does not outlive the serving --------------------------------


def test_no_binding_survives_a_profile_that_will_not_resolve(
    workspace: Path, started: Started
) -> None:
    """The manifest resolves and the process binds; the band it names does
    not exist, and the failure is three steps later."""
    manifest = _manifest_pointing_at(
        workspace, workspace / "db.sqlite3", band="no_such_band"
    )
    _claim(workspace / "db.sqlite3", PROJECT_ID)

    result = _serve(manifest)

    assert result.exit_code == 1
    assert "Profile could not be resolved" in result.output
    assert not started.settings
    assert active_binding() is None


def test_no_binding_survives_a_token_that_will_not_resolve(
    workspace: Path, started: Started
) -> None:
    manifest = _project(workspace)

    result = _serve(manifest, "--token-file", str(workspace / "absent.token"))

    assert result.exit_code == 1
    assert not started.settings
    assert active_binding() is None


def test_no_binding_survives_a_tls_setup_that_fails(
    workspace: Path, started: Started
) -> None:
    manifest = _project(workspace)

    result = _serve(manifest, "--tls-cert", str(workspace / "cert.pem"))

    assert result.exit_code == 1
    assert not started.settings
    assert active_binding() is None


def test_no_binding_survives_a_bad_campaign_id(
    workspace: Path, started: Started
) -> None:
    """`--campaign` is validated after the binding is made, so this is a real
    exit path through the scope and not a hypothetical one."""
    manifest = _project(workspace)
    _campaign(
        manifest,
        "day1",
        f"schema_version: 1\ncampaign_id: day1\nproject_id: {PROJECT_ID}\nlabel: Day 1\n",
    )

    result = _serve(manifest, "--campaign", "day1", "--band", "other_band")

    assert result.exit_code == 1
    assert active_binding() is None


def test_no_binding_survives_an_exception_from_the_server(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash is the path a `finally` exists for."""
    manifest = _project(workspace)

    def _explode(settings: Any, **kwargs: Any) -> None:
        raise RuntimeError("the socket went away")

    monkeypatch.setattr("dmr_iq_surveyor.cli_web.serve_forever", _explode)

    result = _serve(manifest)

    assert isinstance(result.exception, RuntimeError)
    assert active_binding() is None


def test_no_binding_survives_a_refused_database(
    workspace: Path, started: Started
) -> None:
    """The binding is made before the verifying open, so the refusal itself
    has to leave through the scope."""
    unclaimed = workspace / "unclaimed.sqlite3"
    connect_geo_database(unclaimed).close()
    manifest = _manifest_pointing_at(workspace, unclaimed)

    result = _serve(manifest)

    assert result.exit_code == 1
    assert active_binding() is None


def test_a_second_serve_in_the_same_process_can_bind_another_project(
    workspace: Path, started: Started
) -> None:
    """What a leaked binding would break: `bind_project` refuses to rebind, so
    one leftover binding would make every later project unservable."""
    first = _project(workspace)
    assert _serve(first).exit_code == 0

    second_db = workspace / "second.sqlite3"
    connect_geo_database(second_db).close()
    _claim(second_db, "vor_north")
    second = workspace / "projects" / "vor" / "project.yaml"
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_text(
        render_project_manifest(
            project_id="vor_north",
            label="A second project",
            analyzer=ANALYZER,
            database=second_db,
            defaults=ProjectDefaults(band="web_band", site="mobile"),
        ),
        encoding="utf-8",
    )

    result = _serve(second)

    assert result.exit_code == 0, result.output
    assert started.binding[1].project_id == "vor_north"
    assert active_binding() is None
