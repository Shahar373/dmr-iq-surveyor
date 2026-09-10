"""The claim a database carries, and the checks that must happen before one is
opened at all.

Two facts drive this module, both measured on this codebase:

  * `sqlite3.connect` does not read a database, it *manufactures* one. Opening
    a mistyped path creates the directories, the file, all four schema layers
    and commits them -- about 180 KB of empty database on a path that never
    existed. So a project-aware caller must decide whether a file is a database
    BEFORE it opens anything, which is what `inspect_database` is for: it reads
    sixteen bytes and connects to nothing.
  * an existing empty file is opened and fully schema'd without complaint, so
    "the file exists" is not evidence that it is a project database. Size and
    header are.

The claim itself is one typed row with a singleton constraint, so a partial or
duplicated claim cannot exist -- SQLite refuses both rather than this module
having to check for them.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.project.manifest import MANIFEST_SCHEMA_VERSION, ProjectError

SQLITE_HEADER = b"SQLite format 3\x00"

CLAIM_TABLE = "project_meta"

# Applied by `survey.store.connect_survey_database` with the rest of the survey
# schema. `CHECK (id = 1)` is what makes a second claim impossible, and NOT NULL
# on every column is what makes a partial one impossible; both are enforced by
# SQLite rather than by code that could be bypassed.
CLAIM_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    project_id TEXT NOT NULL,
    analyzer TEXT NOT NULL,
    manifest_schema_version INTEGER NOT NULL,
    claimed_at TEXT NOT NULL,
    claimed_by_version TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class DatabaseFile:
    """What can be known about a path without opening it as a database."""

    path: Path
    exists: bool
    size_bytes: int
    is_sqlite: bool
    reason: str = ""

    @property
    def usable(self) -> bool:
        return not self.reason


def inspect_database(path: str | Path) -> DatabaseFile:
    """Decide what a path is, without connecting to it.

    Connecting is the thing that would create it, so this reads the header and
    nothing else. Every refusal carries the sentence an operator needs.
    """
    resolved = Path(path).expanduser().resolve()
    if resolved.is_dir():
        return DatabaseFile(resolved, True, 0, False, f"{resolved} is a directory, not a database")
    if not resolved.exists():
        return DatabaseFile(
            resolved,
            False,
            0,
            False,
            f"no database at {resolved}. A project never creates one by opening it; "
            "use `dmr-surveyor project init --create` to make a new one, or "
            "`--adopt` to take on an existing one",
        )
    try:
        size = resolved.stat().st_size
        header = resolved.open("rb").read(len(SQLITE_HEADER))
    except OSError as exc:
        return DatabaseFile(resolved, True, 0, False, f"{resolved} could not be read: {exc}")
    if size == 0:
        return DatabaseFile(
            resolved,
            True,
            0,
            False,
            f"{resolved} is an empty file. An empty file and a mistyped path look the same, "
            "so it is refused rather than filled in",
        )
    if header != SQLITE_HEADER:
        return DatabaseFile(resolved, True, size, False, f"{resolved} is not a SQLite database")
    return DatabaseFile(resolved, True, size, True)


def require_existing_database(path: str | Path) -> Path:
    """The path, or `ProjectError`. Creates nothing, ever."""
    found = inspect_database(path)
    if not found.usable:
        raise ProjectError(found.reason)
    return found.path


# The tables every dmr-iq-surveyor database has carried since Phase 5, and the
# columns that make each of them this project's rather than something else's.
# Adoption checks these because "it is a valid SQLite file" is not a reason to
# claim something: a browser cache, a package index and a phone backup are all
# valid SQLite files, and claiming one writes a row into somebody else's data.
#
# Deliberately the *oldest* layer, not the current one. A database written
# before Phase 6A has only these, and it is still this project's database --
# the later tables arrive by additive migration once it is opened normally.
# Requiring today's full schema would refuse exactly the databases adoption
# exists for.
REQUIRED_TABLES: dict[str, tuple[str, ...]] = {
    "runs": ("run_id", "source_dir", "imported_at"),
    "attempts": ("attempt_key", "run_id", "candidate_id", "frequency_hz", "status"),
    "events": ("event_key", "attempt_key", "event_type", "raw_line"),
    "sessions": ("session_key", "attempt_key", "session_type", "timing_confidence"),
    "channels": ("frequency_hz", "attempt_count", "color_code_consistency"),
}


@dataclass(frozen=True, slots=True)
class DatabaseContents:
    """What a read-only look at a database says about it.

    Everything adoption needs to decide, gathered in one pass through a
    connection that structurally cannot write.
    """

    integrity: str
    tables: frozenset[str]
    missing_tables: tuple[str, ...]
    missing_columns: tuple[str, ...]
    counts: dict[str, int]

    @property
    def intact(self) -> bool:
        return self.integrity == "ok"

    @property
    def recognised(self) -> bool:
        return not self.missing_tables and not self.missing_columns

    @property
    def refusal(self) -> str:
        """Why this database must not be adopted, or "" if it may be."""
        if not self.intact:
            return (
                f"this database fails SQLite's own integrity check: {self.integrity}. "
                "Adoption is refused rather than claiming a file that cannot be trusted to "
                "read back what it holds"
            )
        if self.missing_tables:
            return (
                f"this is a SQLite database, but not a dmr-iq-surveyor one: it has no "
                f"{', '.join(self.missing_tables)}. Being valid SQLite is not a reason to "
                "claim a file"
            )
        if self.missing_columns:
            return (
                f"this database has dmr-iq-surveyor table names but not its columns "
                f"({', '.join(self.missing_columns)}); it is refused rather than claimed"
            )
        return ""


def check_integrity(connection: sqlite3.Connection) -> str:
    """`PRAGMA quick_check`, or the first problem it reports.

    `quick_check` rather than `integrity_check`: it does everything the latter
    does except the index cross-reference, and the target deployment is a
    Raspberry Pi holding a database that grows with every drive. A check so
    slow that an operator skips it protects nothing.
    """
    try:
        rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError as exc:
        return str(exc)
    if not rows:
        return "quick_check returned nothing"
    return str(tuple(rows[0])[0])


def read_contents(connection: sqlite3.Connection) -> DatabaseContents:
    """Everything a read-only look can establish, in one pass."""
    integrity = check_integrity(connection)
    try:
        tables = frozenset(
            str(tuple(row)[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        )
    except sqlite3.DatabaseError:
        tables = frozenset()

    missing_tables = tuple(name for name in REQUIRED_TABLES if name not in tables)
    missing_columns: list[str] = []
    for table, required in REQUIRED_TABLES.items():
        if table in missing_tables:
            continue
        try:
            present = {str(tuple(row)[1]) for row in connection.execute(
                f"PRAGMA table_info({table})"
            )}
        except sqlite3.DatabaseError:
            present = set()
        missing_columns.extend(
            f"{table}.{column}" for column in required if column not in present
        )

    # Counted only when the file is both intact and recognised. Counting rows
    # in a malformed image raises rather than answering, and a count from a
    # database that is not this project's would be a number about somebody
    # else's data printed under our table names.
    countable = integrity == "ok" and not missing_tables and not missing_columns
    return DatabaseContents(
        integrity=integrity,
        tables=tables,
        missing_tables=missing_tables,
        missing_columns=tuple(missing_columns),
        counts=summarise_contents(connection) if countable else {},
    )


@dataclass(frozen=True, slots=True)
class Claim:
    project_id: str
    analyzer: str
    manifest_schema_version: int
    claimed_at: str
    claimed_by_version: str


def open_read_only(path: str | Path) -> sqlite3.Connection:
    """Open an existing database for reading and nothing else.

    SQLite's `mode=ro` refuses a file that does not exist and refuses every
    write, so this cannot create a database however wrong the path is --
    which is what the reporting half of adoption needs. Verified: opening a
    missing path raises and leaves nothing behind, and an INSERT through
    the result raises `attempt to write a readonly database`.
    """
    resolved = require_existing_database(path)
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def summarise_contents(connection: sqlite3.Connection) -> dict[str, int]:
    """Row counts for the tables an operator would recognise.

    What adoption prints so the operator can tell the database they meant
    from one they did not. A table that is absent is reported as absent
    rather than as zero: those are different facts.
    """
    counts: dict[str, int] = {}
    for table in (
        "runs",
        "survey_runs",
        "rf_observations",
        "geo_measurements",
        "p25_sites",
    ):
        try:
            counts[table] = int(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            )
        except sqlite3.DatabaseError:
            # `OperationalError` is the absent table; the wider
            # `DatabaseError` also covers "database disk image is malformed",
            # which a corrupt file raises here. A summary is a report, and a
            # report must not be the thing that crashes on the file it is
            # describing.
            continue
    return counts


def read_claim(connection: sqlite3.Connection) -> Claim | None:
    """The claim this database carries, or `None` if it carries none.

    Defensive about the table's absence, the same way
    `reference/store.py::_site_ids_with_measurements` is: a database written
    before this table existed simply has no claim, which is a fact about it
    rather than an error.

    That is deliberately narrower than "any error means no claim". A
    `project_meta` table that *exists* but cannot be read -- a corrupt page, a
    malformed row -- is not a database that has never been claimed; it is one
    whose answer to "are you claimed?" cannot be trusted, and every caller
    here (`assert_claim`, the resumability check in `project init --create`,
    the dry run in `project init --adopt`) treats `None` as "safe to claim or
    proceed". Silently returning `None` for corruption would let exactly the
    unsafe case through disguised as the safe one. So only the specific,
    narrow "no such table" error is swallowed; every other failure to read
    this table -- including a malformed database image -- is raised, and it
    is raised from a read-only look, before `connect_geo_database` or any
    write path is reached.
    """
    try:
        row = connection.execute(
            "SELECT project_id, analyzer, manifest_schema_version, claimed_at, claimed_by_version "
            f"FROM {CLAIM_TABLE} WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise ProjectError(
            f"the {CLAIM_TABLE} table could not be read ({exc}); refused rather than treated "
            "as unclaimed"
        ) from exc
    except sqlite3.DatabaseError as exc:
        # Not `OperationalError`: a malformed database image raises the wider
        # `DatabaseError` directly, and that is precisely the corruption case
        # this must not swallow.
        raise ProjectError(
            f"the {CLAIM_TABLE} table could not be read ({exc}); refused rather than treated "
            "as unclaimed"
        ) from exc
    if row is None:
        return None
    values = tuple(row)
    return Claim(
        project_id=str(values[0]),
        analyzer=str(values[1]),
        manifest_schema_version=int(values[2]),
        claimed_at=str(values[3]),
        claimed_by_version=str(values[4]),
    )


def assert_claim(connection: sqlite3.Connection, *, project_id: str, analyzer: str) -> Claim:
    """Refuse to go on unless this database is the one it was asked to be.

    Both fields are compared. A database claimed by the right project but read
    by the wrong analyzer would be interpreted under rules it was not written
    under, which is the same failure as opening the wrong file.
    """
    claim = read_claim(connection)
    if claim is None:
        raise ProjectError(
            "this database carries no project claim. Run "
            "`dmr-surveyor project init --adopt` to take it on deliberately; "
            "nothing here will claim it by opening it"
        )
    if claim.project_id != project_id or claim.analyzer != analyzer:
        raise ProjectError(
            f"this database is claimed by project {claim.project_id!r} "
            f"(analyzer {claim.analyzer!r}), but {project_id!r} (analyzer {analyzer!r}) "
            "was asked for"
        )
    return claim


def write_claim(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    analyzer: str,
    manifest_schema_version: int = MANIFEST_SCHEMA_VERSION,
) -> Claim:
    """Claim this database for a project, in one transaction.

    Idempotent: an identical claim already present is left exactly as it was,
    timestamp included, so re-running adoption does not rewrite history. A
    different claim is refused rather than replaced.
    """
    # Close any implicit transaction left open by schema work, so the explicit
    # one below is the only one in flight.
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = read_claim(connection)
        if existing is not None:
            if existing.project_id != project_id or existing.analyzer != analyzer:
                raise ProjectError(
                    f"this database is already claimed by project {existing.project_id!r} "
                    f"(analyzer {existing.analyzer!r}); it cannot be re-claimed as "
                    f"{project_id!r} (analyzer {analyzer!r})"
                )
            connection.commit()
            return existing
        claim = Claim(
            project_id=project_id,
            analyzer=analyzer,
            manifest_schema_version=manifest_schema_version,
            claimed_at=datetime.now(UTC).isoformat(),
            claimed_by_version=__version__,
        )
        connection.execute(
            f"INSERT INTO {CLAIM_TABLE}(id, project_id, analyzer, manifest_schema_version, "
            "claimed_at, claimed_by_version) VALUES (1, ?, ?, ?, ?, ?)",
            (
                claim.project_id,
                claim.analyzer,
                claim.manifest_schema_version,
                claim.claimed_at,
                claim.claimed_by_version,
            ),
        )
        connection.commit()
        return claim
    except BaseException:
        connection.rollback()
        raise


def write_manifest_atomically(path: str | Path, text: str) -> Path:
    """Write a manifest so a reader never sees a half-written one.

    A temporary sibling, flushed and fsynced, then `os.replace`, which is
    atomic within a filesystem. A failure anywhere leaves the original exactly
    as it was and removes the temporary.
    """
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=destination.parent, prefix=f"{destination.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return destination


__all__ = [
    "CLAIM_SCHEMA",
    "CLAIM_TABLE",
    "REQUIRED_TABLES",
    "SQLITE_HEADER",
    "Claim",
    "DatabaseContents",
    "DatabaseFile",
    "check_integrity",
    "read_contents",
    "assert_claim",
    "open_read_only",
    "summarise_contents",
    "inspect_database",
    "read_claim",
    "require_existing_database",
    "write_claim",
    "write_manifest_atomically",
]
