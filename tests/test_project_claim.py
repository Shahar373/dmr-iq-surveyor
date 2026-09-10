"""A database's claim, and the guard that reads it.

The measured fact behind all of this: `sqlite3.connect` does not read a
database, it manufactures one. Opening a mistyped path creates the directories,
the file, four schema layers and commits them. A project-aware caller therefore
has to decide what a file is *before* opening it, and these tests assert the
filesystem afterwards rather than trusting the exception alone.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dmr_iq_surveyor.geo.pipeline import site_overview
from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.inventory.store import connect_database
from dmr_iq_surveyor.project.binding import active_binding, bind_project, clear_binding
from dmr_iq_surveyor.project.claim import (
    CLAIM_TABLE,
    assert_claim,
    inspect_database,
    read_claim,
    require_existing_database,
    write_claim,
    write_manifest_atomically,
)
from dmr_iq_surveyor.project.manifest import ProjectError

ANALYZER = "p25_site_geolocation"


@pytest.fixture(autouse=True)
def _unbound() -> None:
    """The binding is process-wide, so a test that sets one must not leak it."""
    clear_binding()
    yield
    clear_binding()


def _claimed(path: Path, *, project_id: str = "p25", analyzer: str = ANALYZER) -> Path:
    connection = connect_geo_database(path)
    try:
        write_claim(connection, project_id=project_id, analyzer=analyzer)
    finally:
        connection.close()
    return path


# -- deciding what a path is, without opening it ------------------------------


def test_a_missing_path_is_named_and_nothing_is_created(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "typo.sqlite3"

    found = inspect_database(target)

    assert not found.usable
    assert "never creates one by opening it" in found.reason
    assert not target.exists()
    assert not target.parent.exists(), "inspection must not make directories either"


def test_an_empty_file_is_refused_rather_than_filled_in(tmp_path: Path) -> None:
    """An empty file and a mistyped path look identical, and opening one is
    what turns it into a database."""
    target = tmp_path / "empty.sqlite3"
    target.touch()

    found = inspect_database(target)

    assert not found.usable
    assert "empty file" in found.reason
    assert target.stat().st_size == 0


def test_a_file_that_is_not_a_database_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("this is not a database", encoding="utf-8")

    found = inspect_database(target)

    assert not found.usable
    assert "not a SQLite database" in found.reason
    assert target.read_text(encoding="utf-8") == "this is not a database"


def test_a_directory_is_refused(tmp_path: Path) -> None:
    found = inspect_database(tmp_path)

    assert not found.usable
    assert "is a directory" in found.reason


def test_a_real_database_is_recognised(tmp_path: Path) -> None:
    found = inspect_database(_claimed(tmp_path / "p25.sqlite3"))

    assert found.usable
    assert found.is_sqlite
    assert found.size_bytes > 0


def test_require_existing_database_creates_nothing(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b.sqlite3"

    with pytest.raises(ProjectError):
        require_existing_database(target)

    assert not target.exists()
    assert not target.parent.exists()


# -- the claim ----------------------------------------------------------------


def test_a_fresh_database_carries_no_claim(tmp_path: Path) -> None:
    connection = connect_geo_database(tmp_path / "fresh.sqlite3")
    try:
        assert read_claim(connection) is None
        with pytest.raises(ProjectError, match="no project claim"):
            assert_claim(connection, project_id="p25", analyzer=ANALYZER)
    finally:
        connection.close()


class _UnreadableClaimTable:
    """A connection stand-in where `project_meta` exists but cannot be read.

    Real corruption confined to exactly one table is not reliably
    reproducible without depending on SQLite's internal page layout, so this
    simulates the one distinction `read_claim` has to make: a table that is
    *there* but unreadable is not the same fact as a table that was never
    created. Every other query passes through to the real connection
    untouched.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        if CLAIM_TABLE in sql:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return self._connection.execute(sql, *args)


def test_a_claim_table_that_exists_but_cannot_be_read_is_refused_not_swallowed(
    tmp_path: Path,
) -> None:
    """Only a *missing* `project_meta` table is "no claim". Every caller here
    -- `assert_claim`, the resumability check in `project init --create`, the
    dry run in `project init --adopt` -- treats `None` as safe to proceed, so
    silently returning `None` for corruption would let the unsafe case through
    disguised as the safe one."""
    connection = connect_geo_database(tmp_path / "p25.sqlite3")
    try:
        with pytest.raises(ProjectError, match="could not be read"):
            read_claim(_UnreadableClaimTable(connection))
    finally:
        connection.close()


def test_an_absent_claim_table_is_still_read_as_no_claim(tmp_path: Path) -> None:
    """The narrowing must not over-correct: a database written before the
    `project_meta` table existed -- the ordinary, common case -- is still
    "no claim", not a refusal."""
    connection = sqlite3.connect(tmp_path / "no_table.sqlite3")
    try:
        assert read_claim(connection) is None
    finally:
        connection.close()


def test_a_claim_round_trips_and_is_idempotent(tmp_path: Path) -> None:
    connection = connect_geo_database(tmp_path / "p25.sqlite3")
    try:
        first = write_claim(connection, project_id="p25", analyzer=ANALYZER)
        again = write_claim(connection, project_id="p25", analyzer=ANALYZER)
        # Re-running adoption must not rewrite history, timestamp included.
        assert first == again
        assert read_claim(connection) == first
        assert assert_claim(connection, project_id="p25", analyzer=ANALYZER) == first
    finally:
        connection.close()


def test_a_second_different_claim_is_refused_rather_than_replacing_the_first(
    tmp_path: Path,
) -> None:
    connection = connect_geo_database(tmp_path / "p25.sqlite3")
    try:
        write_claim(connection, project_id="p25", analyzer=ANALYZER)
        with pytest.raises(ProjectError, match="already claimed"):
            write_claim(connection, project_id="vor_north", analyzer=ANALYZER)
        assert read_claim(connection).project_id == "p25"
    finally:
        connection.close()


def test_the_guard_compares_the_analyzer_as_well_as_the_project(tmp_path: Path) -> None:
    """A database read under the wrong analyzer is interpreted by rules it was
    not written under, which is the same failure as opening the wrong file."""
    connection = connect_geo_database(_claimed(tmp_path / "p25.sqlite3"))
    try:
        with pytest.raises(ProjectError, match="was asked for"):
            assert_claim(connection, project_id="p25", analyzer="vor_bearing")
        with pytest.raises(ProjectError, match="was asked for"):
            assert_claim(connection, project_id="vor_north", analyzer=ANALYZER)
    finally:
        connection.close()


def test_sqlite_itself_refuses_a_second_or_partial_claim(tmp_path: Path) -> None:
    """Enforced by the schema rather than by code that could be bypassed."""
    path = _claimed(tmp_path / "p25.sqlite3")
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                f"INSERT INTO {CLAIM_TABLE} VALUES (2, 'vor', 'x', 1, 't', 'v')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            connection.execute(
                f"INSERT INTO {CLAIM_TABLE}(id, project_id) VALUES (3, 'vor')"
            )
    finally:
        connection.close()


# -- the guard at the single chokepoint ---------------------------------------


def test_the_guard_is_inert_when_nothing_is_bound(tmp_path: Path) -> None:
    """Every existing invocation depends on this: unbound, a fresh path is
    still created exactly as it always has been."""
    assert active_binding() is None
    target = tmp_path / "made" / "on" / "open.sqlite3"

    connection = connect_geo_database(target)
    connection.close()

    assert target.is_file()


@pytest.mark.parametrize("kind", ["missing", "empty", "not_sqlite", "directory"])
def test_a_bound_process_creates_nothing_however_wrong_the_path(
    tmp_path: Path, kind: str
) -> None:
    target = {
        "missing": tmp_path / "gone" / "x.sqlite3",
        "empty": tmp_path / "empty.sqlite3",
        "not_sqlite": tmp_path / "notes.txt",
        "directory": tmp_path / "adir",
    }[kind]
    if kind == "empty":
        target.touch()
    elif kind == "not_sqlite":
        target.write_text("nope", encoding="utf-8")
    elif kind == "directory":
        target.mkdir()

    bind_project(project_id="p25", analyzer=ANALYZER, database=target)
    with pytest.raises(ProjectError):
        connect_geo_database(target)

    if kind == "missing":
        assert not target.exists()
        assert not target.parent.exists()
    elif kind == "empty":
        assert target.stat().st_size == 0
    elif kind == "not_sqlite":
        assert target.read_text(encoding="utf-8") == "nope"


def test_a_bound_process_refuses_an_unclaimed_database_without_writing_to_it(
    tmp_path: Path,
) -> None:
    unclaimed = tmp_path / "unclaimed.sqlite3"
    connection = connect_geo_database(unclaimed)
    connection.close()
    before = sqlite3.connect(unclaimed).execute(
        "SELECT count(*) FROM sqlite_master"
    ).fetchone()[0]

    bind_project(project_id="p25", analyzer=ANALYZER, database=unclaimed)
    with pytest.raises(ProjectError, match="no project claim"):
        connect_geo_database(unclaimed)

    after = sqlite3.connect(unclaimed).execute(
        "SELECT count(*) FROM sqlite_master"
    ).fetchone()[0]
    assert after == before, "a refused database must not be written to"


def test_a_bound_process_refuses_a_database_claimed_by_another_project(tmp_path: Path) -> None:
    foreign = _claimed(tmp_path / "vor.sqlite3", project_id="vor_north")

    bind_project(project_id="p25", analyzer=ANALYZER, database=foreign)
    with pytest.raises(ProjectError, match="claimed by project 'vor_north'"):
        connect_geo_database(foreign)


def test_a_bound_process_refuses_a_path_that_is_not_its_own(tmp_path: Path) -> None:
    mine = _claimed(tmp_path / "p25.sqlite3")
    theirs = _claimed(tmp_path / "other.sqlite3")

    bind_project(project_id="p25", analyzer=ANALYZER, database=mine)
    with pytest.raises(ProjectError, match="will not open"):
        connect_geo_database(theirs)


def test_the_guard_reaches_code_that_knows_nothing_about_projects(tmp_path: Path) -> None:
    """`site_overview` opens the database itself, deep in the geo layer. The
    guard sits at the one `sqlite3.connect` in the codebase precisely so that
    every indirect open is covered, not only a check at startup."""
    mine = _claimed(tmp_path / "p25.sqlite3")
    foreign = _claimed(tmp_path / "vor.sqlite3", project_id="vor_north")

    bind_project(project_id="p25", analyzer=ANALYZER, database=mine)
    assert site_overview(database_path=mine) == []
    with pytest.raises(ProjectError):
        site_overview(database_path=foreign)


def test_binding_twice_to_different_projects_is_refused(tmp_path: Path) -> None:
    """A process that changed project half-way would have split its rows
    between two databases with nothing able to say which."""
    bind_project(project_id="p25", analyzer=ANALYZER, database=tmp_path / "a.sqlite3")
    bind_project(project_id="p25", analyzer=ANALYZER, database=tmp_path / "a.sqlite3")
    with pytest.raises(ProjectError, match="already bound"):
        bind_project(project_id="vor_north", analyzer=ANALYZER, database=tmp_path / "b.sqlite3")


# -- migration ----------------------------------------------------------------


def test_a_database_from_before_this_column_upgrades_in_place(tmp_path: Path) -> None:
    """The additive contract, again: an existing Pi database gains the table on
    the next open, carries no claim, and every command that does not name a
    project behaves exactly as before."""
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE runs (run_id TEXT PRIMARY KEY);")
    old.execute("INSERT INTO runs VALUES ('legacy')")
    old.commit()
    old.close()

    connection = connect_database(path)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "runs" in tables
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        # `project_meta` arrives with the survey layer, not the inventory one.
        assert read_claim(connection) is None
    finally:
        connection.close()

    survey = connect_geo_database(path)
    try:
        assert read_claim(survey) is None
        assert survey.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    finally:
        survey.close()


# -- atomic manifest writes ---------------------------------------------------


def test_a_manifest_is_written_atomically_and_leaves_no_temporary(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "project.yaml"

    written = write_manifest_atomically(target, "schema_version: 1\n")

    assert written.read_text(encoding="utf-8") == "schema_version: 1\n"
    assert list(target.parent.glob("*.tmp")) == []


def test_a_failed_manifest_write_leaves_the_original_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project.yaml"
    target.write_text("original\n", encoding="utf-8")

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("dmr_iq_surveyor.project.claim.os.replace", explode)
    with pytest.raises(OSError):
        write_manifest_atomically(target, "replacement\n")

    assert target.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.glob("*.tmp")) == []
