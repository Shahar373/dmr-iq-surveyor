"""The `project` commands, and the promise that looking is not claiming.

Adoption is the one that has to be right: it takes on a database that has
already been validated in the field, so its default has to be to change
nothing and say what it would do. These tests assert the filesystem and the
database bytes afterwards, not just the exit code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app
from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.project.binding import clear_binding
from dmr_iq_surveyor.project.claim import read_claim, write_claim

runner = CliRunner()
ANALYZER = "p25_site_geolocation"


@pytest.fixture(autouse=True)
def _unbound() -> None:
    clear_binding()
    yield
    clear_binding()


def _seeded(path: Path, *, project_id: str | None = None) -> Path:
    """A database with one historical survey run, as a field one would have."""
    connection = connect_geo_database(path)
    try:
        connection.execute(
            "INSERT INTO survey_runs(survey_run_id, band_profile, source_path, source_basename,"
            " center_frequency_hz, sample_rate_hz, capture_time_source, requested_start_hz,"
            " requested_stop_hz, coverage_status, duration_seconds, analyzed_seconds,"
            " segment_count, occupancy_threshold_db, detection_settings_json, tool_version,"
            " imported_at, status)"
            " VALUES ('legacy', 'central_800', '/tmp/a.wav', 'a.wav', 868e6, 5e6, 'auxi',"
            " 866e6, 870e6, 'complete', 90.0, 30.0, 30, 8.0, '{}', '0.10.0',"
            " '2026-08-01T00:00:00+00:00', 'ok')"
        )
        connection.commit()
        if project_id is not None:
            write_claim(connection, project_id=project_id, analyzer=ANALYZER)
    finally:
        connection.close()
    return path


def _init(tmp_path: Path, *args: str) -> object:
    return runner.invoke(
        app,
        [
            "project", "init",
            "--project-id", "p25_central_il",
            "--label", "P25 central Israel",
            "--database", str(tmp_path / "db.sqlite3"),
            "--manifest", str(tmp_path / "p" / "project.yaml"),
            *args,
        ],
    )


def _claim_of(path: Path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return read_claim(connection)
    finally:
        connection.close()


# -- validate ----------------------------------------------------------------


def test_validate_accepts_a_project_and_a_campaign(tmp_path: Path) -> None:
    assert _init(tmp_path, "--create").exit_code == 0
    manifest = tmp_path / "p" / "project.yaml"
    assert runner.invoke(app, ["project", "validate", str(manifest)]).exit_code == 0

    made = runner.invoke(
        app,
        ["project", "campaign", "new", "--project", str(manifest), "--campaign-id", "day1"],
    )
    assert made.exit_code == 0, made.output
    campaign = tmp_path / "p" / "campaigns" / "day1.yaml"
    result = runner.invoke(app, ["project", "validate", str(campaign)])
    assert result.exit_code == 0
    assert "Campaign manifest is valid" in result.output


def test_validate_refuses_a_file_that_is_not_a_manifest(tmp_path: Path) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_text("hello: [1,\n", encoding="utf-8")
    assert runner.invoke(app, ["project", "validate", str(junk)]).exit_code == 1


# -- create ------------------------------------------------------------------


def test_create_makes_a_database_a_claim_and_a_manifest(tmp_path: Path) -> None:
    result = _init(tmp_path, "--create")

    assert result.exit_code == 0, result.output
    database = tmp_path / "db.sqlite3"
    assert database.is_file()
    assert (tmp_path / "p" / "project.yaml").is_file()
    claim = _claim_of(database)
    assert claim is not None
    assert (claim.project_id, claim.analyzer) == ("p25_central_il", ANALYZER)


def test_create_refuses_a_database_that_is_already_there(tmp_path: Path) -> None:
    """Creation must never adopt something that already exists."""
    _seeded(tmp_path / "db.sqlite3")
    result = _init(tmp_path, "--create")

    assert result.exit_code == 1
    assert "use --adopt" in result.output


def test_exactly_one_of_create_or_adopt_is_required(tmp_path: Path) -> None:
    assert _init(tmp_path).exit_code == 1
    assert _init(tmp_path, "--create", "--adopt").exit_code == 1


# -- adopt: the dry run ------------------------------------------------------


def test_adoption_reports_and_changes_nothing_by_default(tmp_path: Path) -> None:
    """The whole point. A database that has been validated in the field must
    not be altered by an operator looking at it."""
    database = _seeded(tmp_path / "db.sqlite3")
    before_bytes = database.read_bytes()
    before_mtime = database.stat().st_mtime_ns

    result = _init(tmp_path, "--adopt")

    assert result.exit_code == 0, result.output
    assert "Nothing was written" in result.output
    assert "rows in survey_runs" in result.output
    assert database.read_bytes() == before_bytes
    assert database.stat().st_mtime_ns == before_mtime
    assert not (tmp_path / "p" / "project.yaml").exists()
    assert _claim_of(database) is None


def test_adoption_says_what_it_would_assign(tmp_path: Path) -> None:
    _seeded(tmp_path / "db.sqlite3")
    result = _init(tmp_path, "--adopt")

    assert "assigns this whole database to the project" in result.output
    assert "campaign_id` stays NULL" in result.output or "stays NULL" in result.output


@pytest.mark.parametrize("kind", ["missing", "empty", "not_sqlite"])
def test_adoption_refuses_anything_that_is_not_a_database(tmp_path: Path, kind: str) -> None:
    database = tmp_path / "db.sqlite3"
    if kind == "empty":
        database.touch()
    elif kind == "not_sqlite":
        database.write_text("nope", encoding="utf-8")

    result = _init(tmp_path, "--adopt")

    assert result.exit_code == 1
    if kind == "missing":
        assert not database.exists()


# -- adopt: the write --------------------------------------------------------


def test_adoption_with_write_claims_the_database_and_writes_the_manifest(tmp_path: Path) -> None:
    database = _seeded(tmp_path / "db.sqlite3")

    result = _init(tmp_path, "--adopt", "--write")

    assert result.exit_code == 0, result.output
    claim = _claim_of(database)
    assert claim is not None
    assert claim.project_id == "p25_central_il"
    assert (tmp_path / "p" / "project.yaml").is_file()
    assert list((tmp_path / "p").glob("*.tmp")) == [], "no temporary may survive"


def test_adoption_is_idempotent(tmp_path: Path) -> None:
    _seeded(tmp_path / "db.sqlite3")
    first = _init(tmp_path, "--adopt", "--write")
    claim_after_first = _claim_of(tmp_path / "db.sqlite3")
    second = _init(tmp_path, "--adopt", "--write")

    assert first.exit_code == 0 and second.exit_code == 0, second.output
    # Re-running must not rewrite history, timestamp included.
    assert _claim_of(tmp_path / "db.sqlite3") == claim_after_first


def test_adoption_refuses_a_database_already_claimed_by_another_project(tmp_path: Path) -> None:
    _seeded(tmp_path / "db.sqlite3", project_id="vor_north")

    result = _init(tmp_path, "--adopt", "--write")

    assert result.exit_code == 1
    assert "already claimed" in result.output
    assert _claim_of(tmp_path / "db.sqlite3").project_id == "vor_north"


def test_a_manifest_that_differs_is_never_overwritten(tmp_path: Path) -> None:
    _seeded(tmp_path / "db.sqlite3")
    manifest = tmp_path / "p" / "project.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("hand written, do not lose\n", encoding="utf-8")

    result = _init(tmp_path, "--adopt", "--write")

    assert result.exit_code == 1
    assert "will not be overwritten" in result.output
    assert manifest.read_text(encoding="utf-8") == "hand written, do not lose\n"
    # Checked before the claim, so a refused manifest leaves no claimed database.
    assert _claim_of(tmp_path / "db.sqlite3") is None


def test_a_claim_written_without_its_manifest_is_completed_by_re_running(tmp_path: Path) -> None:
    """The recoverable half of a partial adoption: the claim is written first,
    so an interrupted run leaves a claimed database and no manifest, and the
    next run writes only what is missing."""
    database = _seeded(tmp_path / "db.sqlite3")
    connection = connect_geo_database(database)
    try:
        write_claim(connection, project_id="p25_central_il", analyzer=ANALYZER)
    finally:
        connection.close()
    assert not (tmp_path / "p" / "project.yaml").exists()

    result = _init(tmp_path, "--adopt", "--write")

    assert result.exit_code == 0, result.output
    assert (tmp_path / "p" / "project.yaml").is_file()


def test_adoption_assigns_the_database_but_no_run_to_a_campaign(tmp_path: Path) -> None:
    database = _seeded(tmp_path / "db.sqlite3")
    _init(tmp_path, "--adopt", "--write")

    connection = sqlite3.connect(database)
    try:
        assigned = connection.execute(
            "SELECT count(*) FROM survey_runs WHERE campaign_id IS NOT NULL"
        ).fetchone()[0]
        total = connection.execute("SELECT count(*) FROM survey_runs").fetchone()[0]
    finally:
        connection.close()
    assert total == 1
    assert assigned == 0, "adoption must never assign a historical run to a campaign"


# -- show --------------------------------------------------------------------


def test_show_reports_unclaimed_claimed_and_foreign(tmp_path: Path) -> None:
    _seeded(tmp_path / "db.sqlite3")
    _init(tmp_path, "--adopt")  # dry run: manifest not written yet
    manifest = tmp_path / "p" / "project.yaml"
    _init(tmp_path, "--adopt", "--write")

    claimed = runner.invoke(app, ["project", "show", "--project", str(manifest)])
    assert claimed.exit_code == 0, claimed.output
    assert "claimed by this project" in claimed.output
    assert "rows in" in claimed.output or "survey_runs" in claimed.output

    other = tmp_path / "other"
    other.mkdir()
    _seeded(other / "db.sqlite3", project_id="vor_north")
    foreign_manifest = other / "project.yaml"
    foreign_manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            str(tmp_path / "db.sqlite3"), str(other / "db.sqlite3")
        ),
        encoding="utf-8",
    )
    foreign = runner.invoke(app, ["project", "show", "--project", str(foreign_manifest)])
    assert foreign.exit_code == 0
    assert "claimed by project 'vor_north'" in foreign.output


def test_show_does_not_create_a_database_that_the_manifest_names(tmp_path: Path) -> None:
    """Looking is never claiming, and never creating either."""
    _init(tmp_path, "--create")
    manifest = tmp_path / "p" / "project.yaml"
    (tmp_path / "db.sqlite3").unlink()

    result = runner.invoke(app, ["project", "show", "--project", str(manifest)])

    assert result.exit_code == 0
    assert "no database at" in result.output
    assert not (tmp_path / "db.sqlite3").exists()


# -- campaigns ---------------------------------------------------------------


def test_campaign_new_writes_once_and_refuses_to_clobber(tmp_path: Path) -> None:
    _init(tmp_path, "--create")
    manifest = tmp_path / "p" / "project.yaml"
    args = ["project", "campaign", "new", "--project", str(manifest), "--campaign-id", "  Day1 "]

    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    written = tmp_path / "p" / "campaigns" / "day1.yaml"
    assert written.is_file()

    again = runner.invoke(app, args)
    assert again.exit_code == 0
    assert "Unchanged" in again.output

    written.write_text("edited by hand\n", encoding="utf-8")
    third = runner.invoke(app, args)
    assert third.exit_code == 1
    assert written.read_text(encoding="utf-8") == "edited by hand\n"


# -- adoption verifies what it is adopting -----------------------------------


def _foreign_database(path: Path) -> Path:
    """A perfectly valid SQLite file that is somebody else's.

    The shape of the real risk: a browser cache, a package index, a phone
    backup. Being valid SQLite is not a reason to write a row into it.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE bookmarks (id INTEGER PRIMARY KEY, url TEXT)")
        connection.execute("INSERT INTO bookmarks(url) VALUES ('https://example.invalid')")
        connection.commit()
    finally:
        connection.close()
    return path


def _fingerprint(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), path.stat().st_size


def test_adoption_refuses_a_foreign_sqlite_file(tmp_path: Path) -> None:
    foreign = _foreign_database(tmp_path / "db.sqlite3")
    before = _fingerprint(foreign)

    result = _init(tmp_path, "--adopt")

    assert result.exit_code == 1
    assert "not a dmr-iq-surveyor one" in result.output.replace("\n", "")
    assert _fingerprint(foreign) == before, "a refused file was modified"
    assert not (tmp_path / "p" / "project.yaml").exists()


def test_a_foreign_file_is_untouched_even_with_write(tmp_path: Path) -> None:
    """`--write` is not a way past the check; the check comes first."""
    foreign = _foreign_database(tmp_path / "db.sqlite3")
    before = _fingerprint(foreign)

    result = _init(tmp_path, "--adopt", "--write")

    assert result.exit_code == 1
    assert _fingerprint(foreign) == before
    assert _claim_of(foreign) is None
    assert not (tmp_path / "p" / "project.yaml").exists()
    assert not list(tmp_path.glob("**/*.tmp"))


def test_adoption_refuses_a_database_with_the_names_but_not_the_columns(
    tmp_path: Path,
) -> None:
    """Table names alone are cheap to collide with; the columns are the
    signature."""
    impostor = tmp_path / "db.sqlite3"
    connection = sqlite3.connect(impostor)
    try:
        for table in ("runs", "attempts", "events", "sessions", "channels"):
            connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    before = _fingerprint(impostor)

    result = _init(tmp_path, "--adopt", "--write")

    assert result.exit_code == 1
    assert "not its columns" in result.output.replace("\n", "")
    assert _fingerprint(impostor) == before


def test_adoption_refuses_a_corrupt_database(tmp_path: Path) -> None:
    """Claiming a file that cannot be trusted to read back what it holds is
    worse than refusing it."""
    database = _seeded(tmp_path / "db.sqlite3")
    raw = bytearray(database.read_bytes())
    # Scribble over the middle of the file, past the header, so it still
    # sniffs as SQLite and still opens.
    for offset in range(4096, min(len(raw), 20480)):
        raw[offset] = 0x5A
    database.write_bytes(bytes(raw))
    before = _fingerprint(database)

    result = _init(tmp_path, "--adopt", "--write")

    assert result.exit_code == 1
    # A clean `typer.Exit`, not a traceback out of the report: `CliRunner`
    # surfaces the former as SystemExit.
    assert isinstance(result.exception, SystemExit)
    assert "integrity check" in result.output.replace("\n", "")
    assert _fingerprint(database) == before
    assert not (tmp_path / "p" / "project.yaml").exists()


def test_adoption_still_accepts_a_real_database(tmp_path: Path) -> None:
    """The check must not refuse what adoption exists for."""
    database = _seeded(tmp_path / "db.sqlite3")

    result = _init(tmp_path, "--adopt", "--write")

    assert result.exit_code == 0, result.output
    assert _claim_of(database).project_id == "p25_central_il"


def test_adoption_accepts_a_pre_phase6_database(tmp_path: Path) -> None:
    """A database written before the survey tables existed is still this
    project's. The signature is the Phase 5 layer for exactly this reason;
    the later tables arrive by additive migration once it is opened."""
    from dmr_iq_surveyor.inventory.store import SCHEMA

    old = tmp_path / "db.sqlite3"
    connection = sqlite3.connect(old)
    try:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO runs(run_id, source_dir, imported_at)"
            " VALUES ('legacy', '/tmp/old', '2025-01-01T00:00:00+00:00')"
        )
        connection.commit()
    finally:
        connection.close()

    result = _init(tmp_path, "--adopt", "--write")

    assert result.exit_code == 0, result.output
    assert _claim_of(old).project_id == "p25_central_il"


def test_the_dry_run_reports_integrity_and_schema(tmp_path: Path) -> None:
    _seeded(tmp_path / "db.sqlite3")

    result = _init(tmp_path, "--adopt")

    assert result.exit_code == 0, result.output
    flat = result.output.replace("\n", "")
    assert "integrity" in flat and "ok" in flat
    assert "dmr-iq-surveyor" in flat
