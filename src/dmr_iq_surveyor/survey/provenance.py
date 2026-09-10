"""Run provenance: the campaign a survey run belongs to, and what the
receiver was actually set to while it measured.

Two questions that look alike and are not:

    requested   what this software asked the radio for
    applied     what the radio reported back when it was asked

They are stored in separate buckets and never merged. A value that was not
observed is absent, not inferred -- the solver reads level as distance, so a
gain copied out of a profile and presented as measured is exactly the sort of
confident wrong number this project refuses everywhere else.

A site profile's `gain` and `lna_state` are a *declaration* by the operator.
They stay in the `sites` row where they have always lived and are never
copied in here as though the radio had confirmed them. A reader that falls
back to that row has to say so: `SOURCE_DECLARED` is the label for it, and it
is the one source value that is never written into a stored blob --
`normalise_hardware()` rejects it.

The stored shape (`survey_runs.hardware_json`), version 1:

    {
      "schema_version": 1,
      "source": "applied" | "requested" | "not_recorded",
      "identity":  {"driver": ..., "serial": ...},
      "requested": {"center_frequency_hz": ..., "if_gain_reduction_db": ..., ...},
      "applied":   {"center_frequency_hz": ..., "gains": {"IFGR": ..., "RFGR": ...}, ...}
    }

An absent bucket is `{}`, meaning not recorded, never a guess. `source` names
the strongest bucket that carries anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HARDWARE_SCHEMA_VERSION = 1

SOURCE_APPLIED = "applied"
SOURCE_REQUESTED = "requested"
SOURCE_DECLARED = "declared"
SOURCE_NOT_RECORDED = "not_recorded"

# What may appear as a stored blob's `source`. `SOURCE_DECLARED` is absent on
# purpose: a site profile's declaration is not provenance of this run's
# receiver state, and writing it here would make it indistinguishable from a
# reading later.
STORED_SOURCES = (SOURCE_APPLIED, SOURCE_REQUESTED, SOURCE_NOT_RECORDED)

SOURCE_LABELS = {
    SOURCE_APPLIED: "applied",
    SOURCE_REQUESTED: "requested",
    SOURCE_DECLARED: "declared",
    SOURCE_NOT_RECORDED: "not recorded",
}

# SDRplay's two named gain elements, as `capture/device.py` writes them into a
# capture report's `device_settings_applied.gains`. Spelled again here rather
# than imported: `capture/` builds on `survey/`, and a survey module reaching
# back into it would invert that.
GAIN_ELEMENT_IF = "IFGR"
GAIN_ELEMENT_RF = "RFGR"

CAPTURE_REPORT_SUFFIX = "_capture_report.json"

_CAMPAIGN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ProvenanceError(ValueError):
    """Raised for an invalid campaign id or hardware provenance blob."""


def normalise_campaign_id(value: str | None) -> str | None:
    """The single validator for a campaign id, applied wherever one is written.

    Stable: trimming and lowercasing are idempotent, so a value that has been
    through here once survives every later pass unchanged. Anything that is
    still not a slug afterwards is rejected rather than mangled into one --
    quietly turning `Day 1` into `day-1` would leave the id the operator typed
    and the id the database holds as two different strings.

    `None` and an empty string both mean unassigned, which is what every run
    written before campaigns existed is.
    """
    if value is None:
        return None
    candidate = str(value).strip().lower()
    if not candidate:
        return None
    if not _CAMPAIGN_ID_RE.match(candidate):
        raise ProvenanceError(
            f"invalid campaign id {value!r}: use 1-64 characters from a-z, 0-9, '.', '_' or '-', "
            "starting with a letter or a digit"
        )
    return candidate


def _known(values: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only the values that are actually known.

    A key that is absent says nothing was observed. A key holding `None`
    would say the same thing while looking like a recorded reading, so it
    never reaches the database.
    """
    if not values:
        return {}
    return {key: value for key, value in values.items() if value is not None}


def hardware_provenance(
    *,
    identity: dict[str, Any] | None = None,
    requested: dict[str, Any] | None = None,
    applied: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a provenance blob. The only constructor stored blobs come from."""
    resolved_applied = _known(applied)
    resolved_requested = _known(requested)
    if resolved_applied:
        source = SOURCE_APPLIED
    elif resolved_requested:
        source = SOURCE_REQUESTED
    else:
        source = SOURCE_NOT_RECORDED
    return {
        "schema_version": HARDWARE_SCHEMA_VERSION,
        "source": source,
        "identity": _known(identity),
        "requested": resolved_requested,
        "applied": resolved_applied,
    }


def hardware_requested(
    *,
    driver: str | None = None,
    serial: str | None = None,
    center_frequency_hz: float | None = None,
    sample_rate_hz: float | None = None,
    bandwidth_hz: float | None = None,
    if_gain_reduction_db: float | None = None,
    lna_state: int | None = None,
    agc: bool | None = None,
    antenna: str | None = None,
) -> dict[str, Any]:
    """Provenance for a capture this software commanded but never read back.

    The live drive path: the settings went to the radio, and nothing asked the
    radio what it did with them. That is `requested`, and it must not be filed
    as `applied`.
    """
    return hardware_provenance(
        identity={"driver": driver, "serial": serial},
        requested={
            "center_frequency_hz": center_frequency_hz,
            "sample_rate_hz": sample_rate_hz,
            "bandwidth_hz": bandwidth_hz,
            "if_gain_reduction_db": if_gain_reduction_db,
            "lna_state": lna_state,
            "agc": agc,
            "antenna": antenna,
        },
    )


def hardware_from_capture_manifest(manifest: dict[str, Any] | None) -> dict[str, Any]:
    """Provenance for a capture this software ran, from its capture report.

    `device_settings_applied` is the radio's own read-back (`capture/device.py`
    asks the device what it did), so it is the only thing filed as `applied`.
    A device that reports nothing back, and a manifest without that key, leave
    `applied` empty and the run described as `requested` -- which is precisely
    what was known about it.
    """
    if not manifest:
        return hardware_provenance()
    settings = manifest.get("settings") or {}
    applied = manifest.get("device_settings_applied") or {}
    gains = applied.get("gains") or {}
    return hardware_provenance(
        identity={"driver": settings.get("driver"), "serial": settings.get("serial")},
        requested={
            "center_frequency_hz": settings.get("center_frequency_hz"),
            "sample_rate_hz": settings.get("sample_rate_hz"),
            "bandwidth_hz": settings.get("bandwidth_hz"),
            "if_gain_reduction_db": settings.get("if_gain_reduction_db"),
            "lna_state": settings.get("lna_state"),
            "agc": settings.get("agc"),
            "antenna": settings.get("antenna"),
        },
        applied={
            "center_frequency_hz": applied.get("center_frequency_hz"),
            "sample_rate_hz": applied.get("sample_rate_hz"),
            "bandwidth_hz": applied.get("bandwidth_hz"),
            "agc": applied.get("agc"),
            "gains": dict(gains) if gains else None,
        },
    )


def capture_report_for(recording: str | Path) -> dict[str, Any] | None:
    """The capture report that provably describes this recording, or `None`.

    `run_capture()` writes `<stem>_capture_report.json` beside the WAV and
    records inside it the path it wrote. Both halves have to agree before the
    report may describe the recording: a file merely sitting in the same
    directory under a matching name is a coincidence, and a coincidence must
    not become a gain reading that the solver then reads as distance.
    Unreadable, malformed, or pointing elsewhere is no link at all.
    """
    try:
        source = Path(recording).expanduser().resolve()
    except OSError:
        return None
    candidate = source.with_name(f"{source.stem}{CAPTURE_REPORT_SUFFIX}")
    if not candidate.is_file():
        return None
    try:
        manifest = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    claimed = manifest.get("wav_path")
    if not claimed:
        return None
    try:
        if Path(str(claimed)).expanduser().resolve() != source:
            return None
    except OSError:
        return None
    return manifest


def hardware_from_recording(recording: str | Path) -> dict[str, Any]:
    """Provenance for a recording analysed after the fact.

    Nothing about the radio is known unless this very recording's own capture
    report is found beside it and names it back. Analysing a file someone
    handed over must not invent a receiver setting for it.
    """
    return hardware_from_capture_manifest(capture_report_for(recording))


def normalise_hardware(value: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a provenance blob on its way into the database.

    `{}` is a legitimate value: it says this run recorded nothing about the
    receiver, which is true of every run written before this column existed.
    Anything non-empty has to be a blob `hardware_provenance()` built, so no
    caller can invent a shape that later readers would misread -- including a
    blob claiming `declared`, which is a reading tier and never provenance.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProvenanceError(f"hardware provenance must be a dict, got {type(value).__name__}")
    if not value:
        return {}
    if value.get("schema_version") != HARDWARE_SCHEMA_VERSION:
        raise ProvenanceError(
            f"hardware provenance schema_version must be {HARDWARE_SCHEMA_VERSION}, "
            f"got {value.get('schema_version')!r}"
        )
    source = value.get("source")
    if source not in STORED_SOURCES:
        raise ProvenanceError(
            f"hardware provenance source must be one of {list(STORED_SOURCES)}, got {source!r}"
        )
    return dict(value)


def load_hardware(raw: Any) -> dict[str, Any]:
    """Read a stored `hardware_json` back, defensively.

    Anything unreadable reads as not recorded rather than raising: a report
    about a campaign must not die on one malformed row.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


@dataclass(frozen=True, slots=True)
class Reading:
    """One receiver value together with the evidence behind it."""

    value: Any | None
    source: str

    @property
    def known(self) -> bool:
        return self.source != SOURCE_NOT_RECORDED

    def render(self, unit: str = "") -> str:
        """The value and where it came from, in the same breath.

        A reader must never have to guess whether a gain was measured, asked
        for, or merely written down by the operator beforehand.
        """
        if not self.known:
            return SOURCE_LABELS[SOURCE_NOT_RECORDED]
        shown = f"{self.value}{unit}" if unit else f"{self.value}"
        return f"{shown} ({SOURCE_LABELS[self.source]})"


NOT_RECORDED = Reading(None, SOURCE_NOT_RECORDED)


def _gain_reading(
    hardware: dict[str, Any], *, element: str, requested_key: str, declared: Any | None
) -> Reading:
    applied = (hardware.get("applied") or {}).get("gains") or {}
    if isinstance(applied, dict) and applied.get(element) is not None:
        return Reading(applied[element], SOURCE_APPLIED)
    requested = hardware.get("requested") or {}
    if isinstance(requested, dict) and requested.get(requested_key) is not None:
        return Reading(requested[requested_key], SOURCE_REQUESTED)
    if declared is not None:
        return Reading(declared, SOURCE_DECLARED)
    return NOT_RECORDED


def if_gain_reading(hardware: dict[str, Any], declared: Any | None = None) -> Reading:
    """The IF gain reduction this run measured at, and how well it is known."""
    return _gain_reading(
        hardware,
        element=GAIN_ELEMENT_IF,
        requested_key="if_gain_reduction_db",
        declared=declared,
    )


def _as_lna_index(value: Any) -> Any:
    """Narrow an integral LNA state to the index it names.

    An LNA state is an index. The radio reports it through a gain element, so
    it comes back as a float, while a site profile stores the index itself. A
    campaign holding both would otherwise look like two settings where there
    is one, and the digest would warn that levels are incomparable when they
    are not. A non-integral value is left exactly as it arrived: that is not
    an LNA state, and rounding it away would hide the fact.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return int(value) if float(value).is_integer() else value


def lna_state_reading(hardware: dict[str, Any], declared: Any | None = None) -> Reading:
    """The LNA state this run measured at, and how well it is known."""
    reading = _gain_reading(
        hardware,
        element=GAIN_ELEMENT_RF,
        requested_key="lna_state",
        declared=declared,
    )
    if not reading.known:
        return reading
    return Reading(_as_lna_index(reading.value), reading.source)


def identity_value(hardware: dict[str, Any], key: str) -> Any | None:
    """A value from the `identity` bucket, or `None`.

    Identity carries no reading tier: a driver name is what the software
    opened the device as, not a quantity anything measured.
    """
    identity = hardware.get("identity") or {}
    return identity.get(key) if isinstance(identity, dict) else None


def hardware_source_label(hardware: dict[str, Any]) -> str:
    """How this run's receiver state is known, in words."""
    return SOURCE_LABELS.get(hardware.get("source", SOURCE_NOT_RECORDED), "unknown")


__all__ = [
    "CAPTURE_REPORT_SUFFIX",
    "GAIN_ELEMENT_IF",
    "GAIN_ELEMENT_RF",
    "HARDWARE_SCHEMA_VERSION",
    "NOT_RECORDED",
    "SOURCE_APPLIED",
    "SOURCE_DECLARED",
    "SOURCE_LABELS",
    "SOURCE_NOT_RECORDED",
    "SOURCE_REQUESTED",
    "STORED_SOURCES",
    "ProvenanceError",
    "Reading",
    "capture_report_for",
    "hardware_from_capture_manifest",
    "hardware_from_recording",
    "hardware_provenance",
    "hardware_requested",
    "hardware_source_label",
    "identity_value",
    "if_gain_reading",
    "lna_state_reading",
    "load_hardware",
    "normalise_campaign_id",
    "normalise_hardware",
]
