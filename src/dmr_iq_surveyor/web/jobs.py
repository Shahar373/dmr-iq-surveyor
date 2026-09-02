"""Background jobs for the field web app.

One capture is a several-minute chain -- record, survey, extract
measurements, solve -- and the operator is holding a phone in a car park
watching it. So each stage reports progress as it goes, every stage
transition is kept as an event a late-joining browser can replay, and a
failure reports which stage failed and why rather than a bare "error".

Threads, not processes: the work is numpy- and I/O-bound, the job count is
one, and keeping it in-process means the SQLite connection discipline and
the memory bounds of the pipeline stages are exactly the ones already
tested elsewhere in this project.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED})


class JobCancelled(Exception):
    """Raised inside a job's own thread once cancellation is requested."""


@dataclass
class Job:
    job_id: str
    kind: str
    label: str
    created_at: str
    status: str = STATUS_PENDING
    stage: str = ""
    message: str = ""
    progress: float = 0.0
    result: dict[str, Any] | None = None
    error: str | None = None
    error_detail: str = ""
    finished_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _condition: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "job_id": self.job_id,
                "kind": self.kind,
                "label": self.label,
                "created_at": self.created_at,
                "status": self.status,
                "stage": self.stage,
                "message": self.message,
                "progress": self.progress,
                "result": self.result,
                "error": self.error,
                "error_detail": self.error_detail,
                "finished_at": self.finished_at,
                "event_count": len(self.events),
            }

    # -- reporting, called from the worker thread -------------------------

    def emit(
        self,
        stage: str,
        message: str,
        *,
        progress: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        with self._condition:
            self.stage = stage
            self.message = message
            if progress is not None:
                self.progress = max(0.0, min(1.0, progress))
            event = {
                "at": datetime.now(UTC).isoformat(),
                "stage": stage,
                "message": message,
                "progress": self.progress,
                "status": self.status,
            }
            if extra:
                event.update(extra)
            self.events.append(event)
            self._condition.notify_all()

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled("cancelled by the operator")

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def request_cancel(self) -> None:
        self._cancel.set()
        self.emit("cancelling", "cancellation requested; stopping at the next safe point")

    def _finish(self, status: str, **fields: Any) -> None:
        with self._condition:
            self.status = status
            self.finished_at = datetime.now(UTC).isoformat()
            for key, value in fields.items():
                setattr(self, key, value)
            if status == STATUS_SUCCEEDED:
                self.progress = 1.0
            self.events.append(
                {
                    "at": self.finished_at,
                    "stage": "finished",
                    "message": fields.get("message", status),
                    "progress": self.progress,
                    "status": status,
                }
            )
            self._condition.notify_all()

    # -- reading, called from request threads ------------------------------

    def wait_for_events(self, cursor: int, timeout: float) -> list[dict[str, Any]]:
        """Block until events past `cursor` exist, the job ends, or timeout.

        Returning an empty list on timeout is what lets the caller send an
        SSE heartbeat, which is how a dropped phone connection is noticed at
        all -- otherwise a stalled reader would hold a thread forever.
        """
        with self._condition:
            if cursor >= len(self.events) and self.status not in TERMINAL_STATUSES:
                self._condition.wait(timeout)
            return self.events[cursor:]

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class JobRegistry:
    """In-memory registry of jobs, with one job running at a time.

    Serialising jobs is deliberate: there is one SDR, and two concurrent
    captures would fail on device contention in a way that is confusing in
    the field. A rejected start says so immediately instead.
    """

    def __init__(self, *, history_limit: int = 50) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._active: str | None = None
        self._history_limit = history_limit

    def active_job(self) -> Job | None:
        with self._lock:
            if self._active is None:
                return None
            job = self._jobs.get(self._active)
        if job is not None and job.is_terminal():
            with self._lock:
                if self._active == job.job_id:
                    self._active = None
            return None
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in reversed(self._order)]
        return [job.snapshot() for job in jobs]

    def submit(self, *, kind: str, label: str, work: Callable[[Job], dict[str, Any]]) -> Job:
        if self.active_job() is not None:
            raise RuntimeError(
                "another job is already running; wait for it to finish or cancel it first"
            )
        job = Job(
            job_id=uuid.uuid4().hex[:12],
            kind=kind,
            label=label,
            created_at=datetime.now(UTC).isoformat(),
        )
        with self._lock:
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._active = job.job_id
            while len(self._order) > self._history_limit:
                stale = self._order.pop(0)
                if stale != self._active:
                    self._jobs.pop(stale, None)

        def runner() -> None:
            job.status = STATUS_RUNNING
            job.emit("starting", label)
            try:
                result = work(job)
            except JobCancelled as exc:
                job._finish(STATUS_CANCELLED, message=str(exc))
            except Exception as exc:  # noqa: BLE001 -- a field job must never take the server down
                job._finish(
                    STATUS_FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                    error_detail=traceback.format_exc(limit=8),
                    message=f"failed during {job.stage or 'startup'}",
                )
            else:
                job._finish(STATUS_SUCCEEDED, result=result, message="complete")
            finally:
                with self._lock:
                    if self._active == job.job_id:
                        self._active = None

        threading.Thread(target=runner, name=f"job-{job.job_id}", daemon=True).start()
        return job


__all__ = [
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_SUCCEEDED",
    "TERMINAL_STATUSES",
    "Job",
    "JobCancelled",
    "JobRegistry",
]
