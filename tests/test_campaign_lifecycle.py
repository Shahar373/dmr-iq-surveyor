"""A campaign is open until it is closed, and closed is not hidden.

`status: open | closed` is the round's own lifecycle. It is deliberately not
`active`: which campaign a deployment is *recording into* is a property of
that deployment -- it lives in the environment file the Pi reads -- while
open/closed is a property of the round itself, read the same way by the
laptop doing analysis and by the car.

The rules these pin:

  * a manifest with no `status` is open, so nothing written before this
    field existed has to be rewritten, and nothing is closed that nobody
    closed;
  * `closed` refuses new acquisition at every door that leads to one, and
    refuses it *early* -- before an SDR is opened, a directory made or a
    database touched;
  * `closed` takes nothing away from reading: the manifest still loads, the
    campaign still lists, and every analysis path is untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app
from dmr_iq_surveyor.project.binding import clear_binding
from dmr_iq_surveyor.project.manifest import (
    CAMPAIGN_STATUS_CLOSED,
    CAMPAIGN_STATUS_OPEN,
    CampaignDefaults,
    ProjectError,
    load_campaign_manifest,
    render_campaign_manifest,
    require_open_campaign,
    resolve_project,
    set_campaign_status_text,
)

runner = CliRunner()

PROJECT_YAML = """
schema_version: 1
project_id: p25_central_il
label: "P25 central Israel"
analyzer: p25_site_geolocation
database: db.sqlite3
defaults:
  band: web_band
  site: mobile
"""

CAMPAIGN_YAML = """
schema_version: 1
campaign_id: 2026-09_day1
project_id: p25_central_il
label: "Day 1"
"""


@pytest.fixture(autouse=True)
def _unbound() -> None:
    clear_binding()
    yield
    clear_binding()


def _project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "project.yaml"
    manifest.write_text(PROJECT_YAML, encoding="utf-8")
    return manifest


def _campaign(root: Path, *, campaign_id: str = "2026-09_day1", status: str | None = None) -> Path:
    directory = root / "campaigns"
    directory.mkdir(parents=True, exist_ok=True)
    body = CAMPAIGN_YAML.replace("2026-09_day1", campaign_id)
    if status is not None:
        body += f"status: {status}\n"
    path = directory / f"{campaign_id}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# -- the manifest field ---------------------------------------------------


def test_a_manifest_without_a_status_is_open(tmp_path: Path) -> None:
    """Every campaign declared before this field existed. Reading an absent
    status any other way would close rounds nobody closed."""
    _project(tmp_path)
    path = _campaign(tmp_path)

    campaign = load_campaign_manifest(path)

    assert campaign.status == CAMPAIGN_STATUS_OPEN
    assert not campaign.is_closed


@pytest.mark.parametrize("declared", [CAMPAIGN_STATUS_OPEN, CAMPAIGN_STATUS_CLOSED])
def test_both_statuses_are_accepted_and_read_back(tmp_path: Path, declared: str) -> None:
    _project(tmp_path)
    path = _campaign(tmp_path, status=declared)

    campaign = load_campaign_manifest(path)

    assert campaign.status == declared
    assert campaign.is_closed == (declared == CAMPAIGN_STATUS_CLOSED)


@pytest.mark.parametrize("declared", ["Closed", "CLOSED", "finished", "done", "active", '""'])
def test_any_other_status_is_refused_by_name(tmp_path: Path, declared: str) -> None:
    """Validated, never repaired, like every other id in this module. A
    status meant to say `closed` and silently read as something else is the
    one mistake this field exists to prevent -- and `active` in particular is
    refused because it is the word for a different question."""
    _project(tmp_path)
    path = _campaign(tmp_path, status=declared)

    with pytest.raises(ProjectError) as raised:
        load_campaign_manifest(path)

    assert "status" in str(raised.value)
    assert "'open', 'closed'" in str(raised.value)


def test_a_status_that_is_not_a_string_is_refused(tmp_path: Path) -> None:
    _project(tmp_path)
    path = _campaign(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + "status: 3\n", encoding="utf-8")

    with pytest.raises(ProjectError):
        load_campaign_manifest(path)


def test_an_open_manifest_renders_exactly_as_it_did_before_the_field_existed() -> None:
    """So a manifest written by an older build still compares equal, and
    `project campaign new` keeps reporting it unchanged rather than as a
    conflict it refuses to overwrite."""
    text = render_campaign_manifest(
        campaign_id="day1", project_id="p", label="Day 1", defaults=CampaignDefaults()
    )

    assert "status" not in text


def test_a_closed_manifest_says_so_in_the_file() -> None:
    text = render_campaign_manifest(
        campaign_id="day1", project_id="p", label="Day 1", status=CAMPAIGN_STATUS_CLOSED
    )

    assert "status: closed" in text


# -- closing edits the file rather than rewriting it ----------------------


ANNOTATED = """# Coastal road, morning. Gain pinned after the 06:40 re-seat.
schema_version: 1
campaign_id: day1
project_id: p25_central_il
label: "Day 1"
defaults:
  band: central_800_narrow  # do not change mid-round
"""


def test_closing_preserves_every_other_byte_including_comments() -> None:
    """A manifest is a file an operator may have annotated. Closing a round
    is not an occasion to drop their notes or reorder their keys."""
    closed = set_campaign_status_text(ANNOTATED, CAMPAIGN_STATUS_CLOSED)

    assert "# Coastal road, morning. Gain pinned after the 06:40 re-seat." in closed
    assert "# do not change mid-round" in closed
    assert "status: closed" in closed
    for line in ANNOTATED.splitlines():
        assert line in closed.splitlines()


def test_closing_twice_leaves_one_status_line() -> None:
    once = set_campaign_status_text(ANNOTATED, CAMPAIGN_STATUS_CLOSED)
    twice = set_campaign_status_text(once, CAMPAIGN_STATUS_CLOSED)

    assert once == twice
    assert twice.count("status:") == 1


def test_the_status_line_lands_above_the_settings_block() -> None:
    closed = set_campaign_status_text(ANNOTATED, CAMPAIGN_STATUS_CLOSED).splitlines()

    assert closed.index("status: closed") < closed.index("defaults:")


def test_an_indented_status_is_not_mistaken_for_the_top_level_one() -> None:
    """Only a line starting at column zero is a top-level key."""
    nested = "schema_version: 1\ndefaults:\n  status: whatever\n"

    closed = set_campaign_status_text(nested, CAMPAIGN_STATUS_CLOSED)

    assert "  status: whatever" in closed
    assert "status: closed" in closed.splitlines()


def test_a_closed_manifest_still_loads_and_still_validates(tmp_path: Path) -> None:
    """Closed is a lifecycle, not a quarantine. Everything that reads a
    campaign keeps reading this one."""
    _project(tmp_path)
    path = _campaign(tmp_path)
    path.write_text(
        set_campaign_status_text(path.read_text(encoding="utf-8"), CAMPAIGN_STATUS_CLOSED),
        encoding="utf-8",
    )

    campaign = load_campaign_manifest(path, expect_project_id="p25_central_il")

    assert campaign.is_closed
    assert campaign.label == "Day 1"


# -- the shared refusal ---------------------------------------------------


def test_require_open_campaign_returns_an_open_round(tmp_path: Path) -> None:
    manifest = _project(tmp_path)
    _campaign(tmp_path)

    campaign = require_open_campaign(resolve_project(manifest), "2026-09_day1")

    assert campaign.campaign_id == "2026-09_day1"


def test_require_open_campaign_refuses_a_closed_round_and_says_reading_still_works(
    tmp_path: Path,
) -> None:
    manifest = _project(tmp_path)
    _campaign(tmp_path, status=CAMPAIGN_STATUS_CLOSED)

    with pytest.raises(ProjectError) as raised:
        require_open_campaign(resolve_project(manifest), "2026-09_day1")

    message = str(raised.value)
    assert "closed" in message
    assert "readable and analysable" in message


# -- the doors into a recording -------------------------------------------


BAND_YAML = """
name: web_band
label: "web test band"
start_frequency_hz: 867910000
stop_frequency_hz: 868090000
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


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bands = tmp_path / "config" / "bands"
    sites = tmp_path / "config" / "sites"
    bands.mkdir(parents=True)
    sites.mkdir(parents=True)
    (bands / "web_band.yaml").write_text(BAND_YAML, encoding="utf-8")
    (sites / "mobile.yaml").write_text(SITE_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture()
def served(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Records whether the server was ever started. It must not be."""
    started: list[Any] = []
    monkeypatch.setattr(
        "dmr_iq_surveyor.cli_web.serve_forever",
        lambda settings, **kwargs: started.append(settings),
    )
    return started


def _serve_project(workspace: Path) -> Path:
    """A project whose database exists and carries its claim."""
    manifest = workspace / "projects" / "p25" / "project.yaml"
    result = runner.invoke(
        app,
        [
            "project", "init", "--create", "--write",
            "--project-id", "p25_central_il",
            "--label", "P25 central Israel",
            "--database", str(workspace / "db.sqlite3"),
            "--manifest", str(manifest),
            "--band", "web_band",
            "--site", "mobile",
        ],
    )
    assert result.exit_code == 0, result.output
    clear_binding()
    return manifest


def test_web_serve_refuses_a_closed_campaign_before_anything_is_opened(
    workspace: Path, served: list[Any]
) -> None:
    """The door that matters most: the field app opens an SDR, makes its
    recordings directory and touches the database on the way up. A closed
    campaign discovered after any of that has already cost something."""
    manifest = _serve_project(workspace)
    _campaign(manifest.parent, status=CAMPAIGN_STATUS_CLOSED)
    output = workspace / "field-output"

    result = runner.invoke(
        app,
        [
            "web", "serve", "--host", "127.0.0.1", "--port", "0", "--token", "s3cret",
            "--project", str(manifest), "--campaign", "2026-09_day1",
            "--output", str(output),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "closed" in result.output
    assert not served, "the server was started against a closed campaign"
    assert not output.exists(), "a directory was created for a refused campaign"


def test_web_serve_still_starts_on_an_open_campaign(
    workspace: Path, served: list[Any]
) -> None:
    """The refusal has to be the closed status and nothing else."""
    manifest = _serve_project(workspace)
    _campaign(manifest.parent)

    result = runner.invoke(
        app,
        [
            "web", "serve", "--host", "127.0.0.1", "--port", "0", "--token", "s3cret",
            "--project", str(manifest), "--campaign", "2026-09_day1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert served, "the server was never started"
    assert served[0].campaign_id == "2026-09_day1"


def test_survey_capture_refuses_a_closed_campaign_before_probing_the_radio(
    workspace: Path,
) -> None:
    """The refusal is recognisable by which error arrives: SoapySDR is not
    installed in CI, so a check that ran after the probe would fail with the
    device's message instead of this one."""
    manifest = _serve_project(workspace)
    _campaign(manifest.parent, status=CAMPAIGN_STATUS_CLOSED)

    result = runner.invoke(
        app,
        [
            "survey", "capture", str(workspace / "out"),
            "--band", "web_band", "--site", "mobile",
            "--center-frequency", "868200000", "--sample-rate", "768000",
            "--campaign", "2026-09_day1", "--project", str(manifest),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "Campaign refused" in result.output
    assert "closed" in result.output
    assert "SoapySDR" not in result.output
    assert not (workspace / "out").exists()


def test_live_stop_refuses_a_closed_campaign_before_opening_the_device(
    workspace: Path,
) -> None:
    manifest = _serve_project(workspace)
    _campaign(manifest.parent, status=CAMPAIGN_STATUS_CLOSED)

    result = runner.invoke(
        app,
        [
            "live", "stop", "--band", "web_band", "--site", "mobile",
            "--latitude", "32.05", "--longitude", "34.79",
            "--campaign", "2026-09_day1", "--project", str(manifest),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "Campaign refused" in result.output


@pytest.mark.parametrize(
    "command",
    [
        ["survey", "capture", "OUTPUT", "--center-frequency", "868200000",
         "--sample-rate", "768000"],
        ["live", "stop", "--latitude", "32.05", "--longitude", "34.79"],
    ],
)
def test_a_project_without_a_campaign_says_there_is_nothing_to_check(
    workspace: Path, command: list[str]
) -> None:
    """`--project` alone checks nothing, and silently checking nothing is how
    an operator ends up believing a guard ran."""
    manifest = _serve_project(workspace)
    arguments = [str(workspace / "out") if part == "OUTPUT" else part for part in command]

    result = runner.invoke(
        app, [*arguments, "--band", "web_band", "--site", "mobile", "--project", str(manifest)]
    )

    assert result.exit_code == 1, result.output
    assert "nothing for it to check" in result.output


def test_an_open_campaign_is_not_refused_by_the_cli_check(workspace: Path) -> None:
    """Past the check, `live stop` goes on to the radio -- which is absent
    here. The point is that it got that far."""
    manifest = _serve_project(workspace)
    _campaign(manifest.parent)

    result = runner.invoke(
        app,
        [
            "live", "stop", "--band", "web_band", "--site", "mobile",
            "--latitude", "32.05", "--longitude", "34.79",
            "--campaign", "2026-09_day1", "--project", str(manifest),
        ],
    )

    assert "Campaign refused" not in result.output


def test_a_closed_campaign_is_still_listed_by_project_show(workspace: Path) -> None:
    """Closed rounds stay visible. A campaign that vanished from the listing
    would look deleted, and nothing here deletes anything."""
    manifest = _serve_project(workspace)
    _campaign(manifest.parent, campaign_id="day1", status=CAMPAIGN_STATUS_CLOSED)
    _campaign(manifest.parent, campaign_id="day2")

    result = runner.invoke(app, ["project", "show", "--project", str(manifest)])

    assert result.exit_code == 0, result.output
    assert "day1" in result.output
    assert "closed" in result.output
    assert "day2" in result.output
    assert "open" in result.output
