"""The project a running process is bound to.

A process-wide value, set once by a project-aware entry point before it does
any work, and immutable afterwards. Process-wide rather than a `ContextVar`
because `web/jobs.py` runs every capture, drive and solve on threads that do
not inherit context, and a `web serve` process serves exactly one project for
its whole life.

Its only reader is `inventory/store.py::connect_database`, the single place in
this codebase that calls `sqlite3.connect`. When nothing is bound that function
behaves exactly as it always has, which is what keeps every existing invocation
working unchanged.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from dmr_iq_surveyor.project.manifest import ProjectError

_lock = threading.Lock()
_binding: ProjectBinding | None = None


@dataclass(frozen=True, slots=True)
class ProjectBinding:
    """What a bound process is allowed to open, and on whose behalf."""

    project_id: str
    analyzer: str
    database: Path


def bind_project(*, project_id: str, analyzer: str, database: str | Path) -> ProjectBinding:
    """Bind this process to one project and one database.

    Idempotent for identical values, so a re-entrant caller is harmless.
    Rebinding to anything else is refused: a process that changed project
    half-way through would have written some of its rows to one database and
    some to another, and nothing afterwards could tell which.
    """
    global _binding
    wanted = ProjectBinding(
        project_id=project_id,
        analyzer=analyzer,
        database=Path(database).expanduser().resolve(),
    )
    with _lock:
        if _binding is not None and _binding != wanted:
            raise ProjectError(
                f"this process is already bound to project {_binding.project_id!r} "
                f"({_binding.database}); it cannot also serve {wanted.project_id!r} "
                f"({wanted.database})"
            )
        _binding = wanted
        return wanted


@contextmanager
def project_binding(
    *, project_id: str, analyzer: str, database: str | Path
) -> Iterator[ProjectBinding]:
    """Hold a binding for the length of a block, and drop it on every exit.

    A binding is process-wide, so leaving one behind after the work it was
    made for has ended is not a tidiness problem: the next thing this process
    does would be silently judged against a project it is no longer serving.
    That is why every entry point takes the binding through here rather than
    calling `bind_project` and hoping to reach a matching `clear_binding` --
    normal return, an exception, `typer.Exit` from a failure three steps
    later, all leave through the `finally`.

    Nested identical bindings are safe: only the block that actually made the
    binding drops it, so an inner scope cannot unbind an outer one.
    """
    already = active_binding()
    binding = bind_project(project_id=project_id, analyzer=analyzer, database=database)
    try:
        yield binding
    finally:
        if already is None:
            clear_binding()


def active_binding() -> ProjectBinding | None:
    """The binding in force, or `None` when the process is project-agnostic."""
    with _lock:
        return _binding


def clear_binding() -> None:
    """Drop the binding. For tests, which need one process to play many roles."""
    global _binding
    with _lock:
        _binding = None


__all__ = [
    "ProjectBinding",
    "active_binding",
    "bind_project",
    "clear_binding",
    "project_binding",
]
