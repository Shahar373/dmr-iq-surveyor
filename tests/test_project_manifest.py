"""Project and Campaign manifests: what they accept, and what they refuse.

Nothing here opens a database. That separation is the point: a manifest has to
be renderable and checkable before anything is written, so adoption can show an
operator exactly what it would do before it does any of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dmr_iq_surveyor.project.manifest import (
    MANIFEST_SCHEMA_VERSION,
    ORIGIN_CAMPAIGN,
    ORIGIN_DEFAULT,
    ORIGIN_FLAG,
    ORIGIN_PROJECT,
    ProjectError,
    Resolution,
    load_campaign_manifest,
    load_project_manifest,
    normalise_project_id,
    resolve_campaign,
    resolve_project,
    resolve_setting,
)

PROJECT_YAML = """
schema_version: 1
project_id: p25_central_il
label: "P25 central Israel"
analyzer: p25_site_geolocation
database: inventory/dmr_inventory.sqlite3
defaults:
  band: central_800_narrow
  site: /etc/dmr-field/sites/field.yaml
  output: /var/lib/dmr-field
"""

CAMPAIGN_YAML = """
schema_version: 1
campaign_id: 2026-09_day1
project_id: p25_central_il
label: "Day 1, coastal road"
defaults:
  band: central_800_narrow
  capture:
    center_frequency_hz: 867406250
    duration_seconds: 90
"""


def _project(root: Path, body: str = PROJECT_YAML) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "project.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _campaign(root: Path, name: str = "2026-09_day1", body: str = CAMPAIGN_YAML) -> Path:
    directory = root / "campaigns"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# -- ids ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [("p25", "p25"), ("  P25_Central ", "p25_central"), ("a.b-c_d", "a.b-c_d"), (None, None), ("", None)],
)
def test_project_id_normalisation(given, expected) -> None:
    assert normalise_project_id(given) == expected


@pytest.mark.parametrize("given", ["p 25", "p/25", "-lead", "_lead", "a" * 65, "פרויקט"])
def test_project_id_rejects_rather_than_mangles(given) -> None:
    with pytest.raises(ProjectError):
        normalise_project_id(given)


def test_project_id_normalisation_is_stable() -> None:
    once = normalise_project_id("  P25_Central ")
    assert normalise_project_id(once) == once


# -- the project manifest ----------------------------------------------------


def test_a_valid_project_manifest_parses(tmp_path: Path) -> None:
    manifest = load_project_manifest(_project(tmp_path / "p"))

    assert manifest.schema_version == MANIFEST_SCHEMA_VERSION
    assert manifest.project_id == "p25_central_il"
    assert manifest.analyzer == "p25_site_geolocation"
    assert manifest.defaults.band == "central_800_narrow"
    assert manifest.root == (tmp_path / "p").resolve()
    assert manifest.campaign_path("day1") == (tmp_path / "p" / "campaigns" / "day1.yaml").resolve()


def test_a_relative_database_is_relative_to_the_manifest_not_the_shell(tmp_path: Path) -> None:
    """A project that is read from another directory must still name the same
    file, and a project that moves must keep working."""
    manifest = load_project_manifest(_project(tmp_path / "p"))

    assert manifest.database == (tmp_path / "p" / "inventory" / "dmr_inventory.sqlite3").resolve()


def test_an_absolute_database_is_taken_as_given(tmp_path: Path) -> None:
    body = PROJECT_YAML.replace(
        "database: inventory/dmr_inventory.sqlite3", "database: /var/lib/dmr-field/x.sqlite3"
    )
    manifest = load_project_manifest(_project(tmp_path / "p", body))

    assert manifest.database == Path("/var/lib/dmr-field/x.sqlite3")


def test_an_unknown_key_is_an_error_not_a_silent_no_op(tmp_path: Path) -> None:
    """A misspelled default is a default that would never apply."""
    body = PROJECT_YAML + "bands: central_800\n"
    with pytest.raises(ProjectError, match="Unknown keys"):
        load_project_manifest(_project(tmp_path / "p", body))


def test_an_unknown_default_key_is_an_error(tmp_path: Path) -> None:
    body = PROJECT_YAML + "  gain: 40\n"
    with pytest.raises(ProjectError, match="Unknown keys"):
        load_project_manifest(_project(tmp_path / "p", body))


def test_an_unsupported_analyzer_is_refused_rather_than_treated_as_p25(tmp_path: Path) -> None:
    """A project pointed at a reader that does not exist would produce results
    meaning something other than they say."""
    body = PROJECT_YAML.replace("p25_site_geolocation", "vor_bearing")
    with pytest.raises(ProjectError, match="not supported"):
        load_project_manifest(_project(tmp_path / "p", body))


@pytest.mark.parametrize("version", ["schema_version: 2", "schema_version: 0"])
def test_a_foreign_schema_version_is_refused(tmp_path: Path, version: str) -> None:
    body = PROJECT_YAML.replace("schema_version: 1", version)
    with pytest.raises(ProjectError, match="schema_version"):
        load_project_manifest(_project(tmp_path / "p", body))


@pytest.mark.parametrize("dropped", ["project_id", "label", "analyzer", "database"])
def test_a_missing_required_key_is_named(tmp_path: Path, dropped: str) -> None:
    body = "\n".join(line for line in PROJECT_YAML.splitlines() if not line.startswith(f"{dropped}:"))
    with pytest.raises(ProjectError, match=dropped):
        load_project_manifest(_project(tmp_path / "p", body))


@pytest.mark.parametrize(("body", "match"), [("", "empty"), ("- a\n- b\n", "mapping"), ("a: [1,\n", "YAML")])
def test_a_file_that_is_not_a_manifest_says_why(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(ProjectError, match=match):
        load_project_manifest(_project(tmp_path / "p", body))


def test_a_missing_manifest_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_project_manifest(tmp_path / "nowhere.yaml")


# -- the campaign manifest ---------------------------------------------------


def test_a_valid_campaign_manifest_parses(tmp_path: Path) -> None:
    manifest = load_campaign_manifest(_campaign(tmp_path / "p"))

    assert manifest.campaign_id == "2026-09_day1"
    assert manifest.project_id == "p25_central_il"
    assert manifest.defaults.capture["duration_seconds"] == 90


def test_a_campaign_file_renamed_without_its_contents_is_refused(tmp_path: Path) -> None:
    """Otherwise a campaign could be found under one name and record another."""
    path = _campaign(tmp_path / "p", name="day2")
    with pytest.raises(ProjectError, match="filename"):
        load_campaign_manifest(path)


def test_a_campaign_belonging_to_another_project_is_refused(tmp_path: Path) -> None:
    path = _campaign(tmp_path / "p")
    with pytest.raises(ProjectError, match="belongs to project"):
        load_campaign_manifest(path, expect_project_id="vor_north")


def test_a_campaign_that_is_not_the_one_asked_for_is_refused(tmp_path: Path) -> None:
    path = _campaign(tmp_path / "p")
    with pytest.raises(ProjectError, match="was asked for"):
        load_campaign_manifest(path, expect_campaign_id="2026-09_day2")


def test_a_campaign_id_that_is_not_a_slug_is_refused(tmp_path: Path) -> None:
    body = CAMPAIGN_YAML.replace("2026-09_day1", "Day One")
    path = tmp_path / "p" / "campaigns" / "x.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ProjectError, match="invalid campaign id"):
        load_campaign_manifest(path)


def test_a_capture_block_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    body = CAMPAIGN_YAML.replace("  capture:\n", "  capture: 90\n").replace(
        "    center_frequency_hz: 867406250\n    duration_seconds: 90\n", ""
    )
    with pytest.raises(ProjectError, match="capture"):
        load_campaign_manifest(_campaign(tmp_path / "p", body=body))


# -- resolution --------------------------------------------------------------


def test_a_project_resolves_by_path_by_directory_and_by_name(tmp_path: Path) -> None:
    _project(tmp_path / "projects" / "p25")

    by_path = resolve_project(tmp_path / "projects" / "p25" / "project.yaml")
    by_dir = resolve_project(tmp_path / "projects" / "p25")
    by_name = resolve_project("p25", base_dir=tmp_path)

    assert by_path.project_id == by_dir.project_id == by_name.project_id == "p25_central_il"


def test_an_unresolvable_project_names_what_it_tried(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match="Tried"):
        resolve_project("nope", base_dir=tmp_path)


def test_a_campaign_resolves_under_the_project_root(tmp_path: Path) -> None:
    _project(tmp_path / "p")
    _campaign(tmp_path / "p")
    project = resolve_project(tmp_path / "p")

    assert resolve_campaign(project, "  2026-09_Day1 ").campaign_id == "2026-09_day1"


def test_a_campaign_is_never_invented(tmp_path: Path) -> None:
    """A run tagged with a round nobody declared has no settings pinned to it."""
    _project(tmp_path / "p")
    project = resolve_project(tmp_path / "p")

    with pytest.raises(ProjectError, match="no campaign manifest at"):
        resolve_campaign(project, "day9")


# -- precedence and conflicts ------------------------------------------------


def test_precedence_runs_flag_then_campaign_then_project_then_default() -> None:
    assert resolve_setting(
        "output", flag="/f", flag_explicit=True, campaign="/c", project="/p", default="/d"
    ) == Resolution("/f", ORIGIN_FLAG)
    assert resolve_setting(
        "output", flag="/d", flag_explicit=False, campaign="/c", project="/p", default="/d"
    ).origin == ORIGIN_CAMPAIGN
    assert resolve_setting(
        "output", flag="/d", flag_explicit=False, campaign=None, project="/p", default="/d"
    ).origin == ORIGIN_PROJECT
    assert resolve_setting(
        "output", flag="/d", flag_explicit=False, campaign=None, project=None, default="/d"
    ).origin == ORIGIN_DEFAULT


def test_a_flag_left_at_its_default_does_not_beat_a_manifest() -> None:
    """Several flags have a non-None default, so the value alone cannot say
    whether the operator typed it. Only the source can."""
    resolved = resolve_setting(
        "band",
        flag="central_800_narrow",
        flag_explicit=False,
        project="central_800",
        default="central_800_narrow",
    )
    assert resolved.value == "central_800"
    assert resolved.origin == ORIGIN_PROJECT


def test_an_explicit_band_that_contradicts_the_manifest_is_refused() -> None:
    """Levels recorded under different bands are not comparable, so this is an
    error rather than an override."""
    with pytest.raises(ProjectError, match="not comparable"):
        resolve_setting(
            "band", flag="central_800", flag_explicit=True, project="central_800_narrow"
        )


def test_an_explicit_band_that_agrees_with_the_manifest_is_not_a_conflict() -> None:
    resolved = resolve_setting(
        "band", flag="central_800_narrow", flag_explicit=True, project="central_800_narrow"
    )
    assert resolved.origin == ORIGIN_FLAG


def test_an_explicit_output_overrides_the_manifest_silently() -> None:
    """A path changes where bytes land, not what they mean."""
    resolved = resolve_setting("output", flag="/tmp/x", flag_explicit=True, project="/var/lib/y")
    assert resolved.value == "/tmp/x"
    assert resolved.origin == ORIGIN_FLAG


# -- the example that ships with the repository ------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config" / "projects" / "example"


def test_the_shipped_example_manifests_are_valid() -> None:
    """It is documentation an operator is told to copy. A stale example is a
    worse starting point than none, and only loading it can say."""
    project = load_project_manifest(EXAMPLE / "project.yaml")
    assert project.project_id == "example_p25"
    assert project.analyzer == "p25_site_geolocation"
    # Relative in the file, resolved against the manifest, so copying the
    # directory somewhere else moves the database path with it.
    assert project.database.is_absolute()

    campaign = resolve_campaign(project, "2026-09_day1")
    assert campaign.project_id == project.project_id
    assert campaign.defaults.capture["sample_rate_hz"] == 5_000_000.0


def test_a_project_resolves_by_name_under_config_projects(tmp_path: Path) -> None:
    """`config/projects/<name>/` alongside `config/bands` and `config/sites`,
    so an operator who knows how a band profile resolves knows this too."""
    _project(tmp_path / "config" / "projects" / "p25")

    resolved = resolve_project("p25", base_dir=tmp_path)

    assert resolved.project_id == "p25_central_il"


def test_config_projects_is_searched_before_a_bare_projects_directory(
    tmp_path: Path,
) -> None:
    _project(tmp_path / "config" / "projects" / "p25")
    _project(tmp_path / "projects" / "p25", body=PROJECT_YAML.replace("p25_central_il", "other_id"))

    assert resolve_project("p25", base_dir=tmp_path).project_id == "p25_central_il"


# -- a campaign is found by its filename, so the filename is checked ---------


def test_a_campaign_filename_that_is_not_a_slug_is_refused(tmp_path: Path) -> None:
    """It used to be skipped: an unparseable filename meant the comparison
    never ran, so the one name that can never be found by `resolve_campaign`
    was the one name that loaded without complaint."""
    _project(tmp_path / "p")
    directory = tmp_path / "p" / "campaigns"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "Day One.yaml"
    path.write_text(
        "schema_version: 1\ncampaign_id: 2026-09_day1\nproject_id: p25_central_il\n"
        "label: Day 1\n",
        encoding="utf-8",
    )

    with pytest.raises(ProjectError, match="not a valid campaign id"):
        load_campaign_manifest(path)


def test_a_campaign_filename_that_only_differs_in_case_is_accepted(tmp_path: Path) -> None:
    """`2026-09_Day1.yaml` normalises to the same slug, so it agrees."""
    _project(tmp_path / "p")
    directory = tmp_path / "p" / "campaigns"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "2026-09_Day1.yaml"
    path.write_text(
        "schema_version: 1\ncampaign_id: 2026-09_day1\nproject_id: p25_central_il\n"
        "label: Day 1\n",
        encoding="utf-8",
    )

    assert load_campaign_manifest(path).campaign_id == "2026-09_day1"


def test_a_campaign_filename_with_no_stem_is_refused(tmp_path: Path) -> None:
    _project(tmp_path / "p")
    directory = tmp_path / "p" / "campaigns"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".yaml"
    path.write_text(
        "schema_version: 1\ncampaign_id: 2026-09_day1\nproject_id: p25_central_il\n"
        "label: Day 1\n",
        encoding="utf-8",
    )

    with pytest.raises(ProjectError):
        load_campaign_manifest(path)


def test_resolve_setting_uses_the_callers_notion_of_equivalence() -> None:
    """`resolve_setting` compares values as written unless told how to compare
    them properly; a key with more than one spelling needs the latter."""
    resolved = resolve_setting(
        "band",
        flag="central_800_narrow",
        flag_explicit=True,
        project="/config/bands/central_800_narrow.yaml",
        equivalent=lambda left, right: str(right).endswith(f"/{left}.yaml"),
    )
    assert resolved.origin == ORIGIN_FLAG

    with pytest.raises(ProjectError, match="not comparable"):
        resolve_setting(
            "band",
            flag="central_800",
            flag_explicit=True,
            project="/config/bands/central_800_narrow.yaml",
            equivalent=lambda left, right: str(right).endswith(f"/{left}.yaml"),
        )
