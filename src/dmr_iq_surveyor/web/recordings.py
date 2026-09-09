"""Recording retention and the disk gate for a field campaign.

A 5 MS/s, 120 s stop writes about 2.24 GiB. A 20-stop campaign is therefore
around 45 GiB of raw IQ, which does not fit on the storage a Raspberry Pi in
the field actually has. But the recording is only needed until the survey has
extracted its observations into SQLite: after that it is 2.24 GiB describing
something already measured.

So this module does two things, and refuses to do them silently:

* **Gate.** Before a capture starts, check that the space it needs is really
  there. Filling the card mid-capture costs the stop, can leave the recording
  unreadable, and (before the guard in `capture/core.py`) could leave the SDR
  claimed for the rest of the day.
* **Retain.** After a survey succeeds, delete recordings beyond the newest
  `keep`, and write down what was deleted and why. Every capture keeps its
  `*_capture_report.json` -- a few kilobytes recording exactly what was
  recorded -- so a discarded recording leaves evidence, not a gap.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dmr_iq_surveyor.capture.preflight import capture_size_bytes

LEDGER_NAME = "retention.json"

# Never plan to fill the last of the medium: the survey stage writes spectrum
# artifacts, reports and the SQLite database alongside the recording. Matches
# the factor `capture/preflight.py` already applies.
_HEADROOM_FACTOR = 1.15

GIB = 1024**3


@dataclass(slots=True)
class DiskStatus:
    directory: str
    free_bytes: int
    total_bytes: int
    per_capture_bytes: int
    retained_count: int
    retained_bytes: int
    keep_recordings: int
    ready: bool
    reason: str

    @property
    def captures_that_fit(self) -> int:
        if self.per_capture_bytes <= 0:
            return 0
        return int(self.free_bytes / (self.per_capture_bytes * _HEADROOM_FACTOR))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["free_gib"] = round(self.free_bytes / GIB, 2)
        payload["per_capture_gib"] = round(self.per_capture_bytes / GIB, 2)
        payload["retained_gib"] = round(self.retained_bytes / GIB, 2)
        payload["captures_that_fit"] = self.captures_that_fit
        return payload


def recordings_in(directory: Path) -> list[Path]:
    """Every captured WAV in `directory`, oldest first.

    Ordered by modification time rather than by name: a Raspberry Pi has no
    real-time clock, so a run id built from a stale clock can sort before a
    recording made hours earlier, and retention would then delete the newest.
    """
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.wav"), key=lambda path: path.stat().st_mtime)


def disk_status(
    directory: str | Path,
    *,
    sample_rate_hz: float,
    duration_seconds: float,
    keep_recordings: int,
) -> DiskStatus:
    """Whether one more capture of this size actually fits."""
    resolved = Path(directory).expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    needed = int(capture_size_bytes(sample_rate_hz, duration_seconds))
    retained = recordings_in(resolved)
    retained_bytes = sum(path.stat().st_size for path in retained)
    try:
        usage = shutil.disk_usage(resolved)
    except OSError as exc:
        return DiskStatus(
            directory=str(resolved),
            free_bytes=0,
            total_bytes=0,
            per_capture_bytes=needed,
            retained_count=len(retained),
            retained_bytes=retained_bytes,
            keep_recordings=keep_recordings,
            ready=False,
            reason=f"cannot read free space for {resolved}: {exc}",
        )

    required = needed * _HEADROOM_FACTOR
    if usage.free >= required:
        return DiskStatus(
            directory=str(resolved),
            free_bytes=usage.free,
            total_bytes=usage.total,
            per_capture_bytes=needed,
            retained_count=len(retained),
            retained_bytes=retained_bytes,
            keep_recordings=keep_recordings,
            ready=True,
            reason="",
        )

    hint = (
        f"{retained_bytes / GIB:.2f} GiB is held by {len(retained)} kept recording(s); "
        "purge them, or lower --keep-recordings"
        if retained
        else "shorten the capture, or free space on the device"
    )
    return DiskStatus(
        directory=str(resolved),
        free_bytes=usage.free,
        total_bytes=usage.total,
        per_capture_bytes=needed,
        retained_count=len(retained),
        retained_bytes=retained_bytes,
        keep_recordings=keep_recordings,
        ready=False,
        reason=(
            f"{usage.free / GIB:.2f} GiB free, but this capture needs "
            f"{needed / GIB:.2f} GiB plus room for artifacts. {hint}."
        ),
    )


def _append_ledger(directory: Path, entries: list[dict[str, Any]]) -> None:
    """Record every deletion. A recording that vanished without a note would
    be exactly the silent gap this project's "missing is not null" rule
    exists to prevent."""
    if not entries:
        return
    path = directory / LEDGER_NAME
    history: list[dict[str, Any]] = []
    if path.is_file():
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            history = []
    history.extend(entries)
    path.write_text(json.dumps(history, indent=2), encoding="utf-8")


def enforce_retention(
    directory: str | Path, *, keep: int, reason: str = "retention policy"
) -> dict[str, Any]:
    """Delete recordings beyond the newest `keep`, and write down what went.

    Only ever called after the survey for a recording has SUCCEEDED, so a
    failed stop keeps its IQ and can be re-analysed.
    """
    resolved = Path(directory).expanduser().resolve()
    recordings = recordings_in(resolved)
    doomed = recordings[: max(0, len(recordings) - max(0, keep))]
    freed = 0
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in doomed:
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        freed += size
        entries.append(
            {
                "recording": path.name,
                "bytes": size,
                "deleted_at": datetime.now(UTC).isoformat(),
                "reason": reason,
                # The capture report stays behind and holds the settings,
                # frame count, overflow count and timing of what was recorded.
                "capture_report": f"{path.stem}_capture_report.json",
            }
        )
    _append_ledger(resolved, entries)
    return {
        "deleted": [entry["recording"] for entry in entries],
        "deleted_count": len(entries),
        "freed_bytes": freed,
        "freed_gib": round(freed / GIB, 2),
        "kept": [path.name for path in recordings[len(doomed) :]],
        "errors": errors,
    }


def purge_recordings(directory: str | Path) -> dict[str, Any]:
    """Delete every recording, on the operator's explicit request."""
    return enforce_retention(directory, keep=0, reason="purged by the operator")


__all__ = [
    "GIB",
    "LEDGER_NAME",
    "DiskStatus",
    "disk_status",
    "enforce_retention",
    "purge_recordings",
    "recordings_in",
]
