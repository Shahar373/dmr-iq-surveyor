"""Run provenance: the campaign a survey run belongs to, and what the
receiver was set to while it measured.

Three claims about a receiver setting look alike and are not:

    applied     what the radio reported back when it was asked
    requested   what this software asked the radio for
    declared    what the operator wrote in the site profile beforehand

They are stored in separate buckets and never merged. The geolocation solver
reads level as distance, so a declaration presented as a measurement is the
kind of confident wrong number this project refuses everywhere else. Keeping
them apart is not the same as discarding the weaker ones: a declaration is
the only thing many runs have, and it is recorded per run so that editing a
site profile later cannot silently rewrite what an earlier run was taken
with.

The stored shape (`survey_runs.hardware_json`), version 1:

    {
      "schema_version": 1,
      "source": "applied" | "requested" | "declared" | "not_recorded",
      "identity":  {"driver": ..., "serial": ...},
      "applied":   {"center_frequency_hz": ..., "gains": {"IFGR": ...}, ...},
      "requested": {"center_frequency_hz": ..., "if_gain_reduction_db": ...},
      "declared":  {"site_id": ..., "receiver": ..., "gain": ..., ...}
    }

An absent bucket is `{}`, meaning nothing of that kind is known, never a
guess. `source` is *derived*, never chosen: it names the strongest bucket
that carries anything, and `normalise_hardware()` refuses a blob whose
`source` disagrees with its own contents.

`SOURCE_DECLARED_SITE_ROW` is the one source that is never stored. It marks a
value read out of the mutable `sites` row, which is all a run written before
this column existed can offer, and which any later run rewrites.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dmr_iq_surveyor.survey.profiles import SiteProfile

HARDWARE_SCHEMA_VERSION = 1

SOURCE_APPLIED = "applied"
SOURCE_REQUESTED = "requested"
SOURCE_DECLARED = "declared"
SOURCE_NOT_RECORDED = "not_recorded"

# Read out of the `sites` row rather than the run's own blob. `sites` is
# current state -- `upsert_site` rewrites it on every run -- so this is only
# ever offered for rows written before runs carried their own declaration,
# and it is labelled distinctly so nobody mistakes it for one.
SOURCE_DECLARED_SITE_ROW = "declared_site_row"

STORED_SOURCES = (SOURCE_APPLIED, SOURCE_REQUESTED, SOURCE_DECLARED, SOURCE_NOT_RECORDED)

BUCKETS = ("identity", "applied", "requested", "declared")

SOURCE_LABELS = {
    SOURCE_APPLIED: "applied",
    SOURCE_REQUESTED: "requested",
    SOURCE_DECLARED: "declared",
    SOURCE_DECLARED_SITE_ROW: "declared, from the site row",
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


# -- campaign id -------------------------------------------------------------


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


# -- building a blob ---------------------------------------------------------


# A receiver setting is a number, a name, or a flag. Nothing this module
# records is a list or a nested object, with one exception: the radio
# reports its gains as a mapping of element name to value. Anything else
# arriving in a bucket is not a reading, and a report that tried to count
# or render it would raise rather than print.
_SCALARS = (str, int, float, bool)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, _SCALARS)


def _scalar_mapping(values: Any) -> dict[str, Any]:
    """A flat mapping of names to scalars, with everything else dropped."""
    if not isinstance(values, dict):
        return {}
    return {
        str(key): value
        for key, value in values.items()
        if isinstance(key, str) and _is_scalar(value)
    }


def _known(values: Any) -> dict[str, Any]:
    """Keep only the values that are actually known, and usable.

    A key that is absent says nothing was observed. A key holding `None`
    would say the same thing while looking like a recorded reading, so it
    never reaches the database. Neither does a value of a shape no reading
    has -- a list where a gain belongs is not a gain, and carrying it
    forward only moves the failure into whatever tries to print it.
    `gains` is the one nested mapping, and it is flattened to its scalars.
    """
    if not isinstance(values, dict):
        return {}
    kept: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        name = str(key)
        if name == 'gains':
            gains = _scalar_mapping(value)
            if gains:
                kept[name] = gains
        elif _is_scalar(value):
            kept[name] = value
    return kept


def _derive_source(applied: dict, requested: dict, declared: dict) -> str:
    """The strongest bucket that carries anything.

    Derived rather than chosen, so a blob cannot claim better evidence than it
    holds. `normalise_hardware()` recomputes this and rejects a mismatch.
    """
    if applied:
        return SOURCE_APPLIED
    if requested:
        return SOURCE_REQUESTED
    if declared:
        return SOURCE_DECLARED
    return SOURCE_NOT_RECORDED


def hardware_provenance(
    *,
    identity: Any = None,
    applied: Any = None,
    requested: Any = None,
    declared: Any = None,
) -> dict[str, Any]:
    """Build a provenance blob. The only constructor stored blobs come from."""
    resolved_applied = _known(applied)
    resolved_requested = _known(requested)
    resolved_declared = _known(declared)
    return {
        "schema_version": HARDWARE_SCHEMA_VERSION,
        "source": _derive_source(resolved_applied, resolved_requested, resolved_declared),
        "identity": _known(identity),
        "applied": resolved_applied,
        "requested": resolved_requested,
        "declared": resolved_declared,
    }


def applied_bucket(applied_settings: Any) -> dict[str, Any]:
    """The `applied` bucket from a device's own read-back.

    `SoapyIqDevice._configure()` asks the radio what it ended up at and stores
    the answer in `applied_settings`. A device that exposes no read-back
    leaves this empty, which is the honest description of it.
    """
    if not isinstance(applied_settings, dict):
        return {}
    gains = applied_settings.get("gains")
    return _known(
        {
            "center_frequency_hz": applied_settings.get("center_frequency_hz"),
            "sample_rate_hz": applied_settings.get("sample_rate_hz"),
            "bandwidth_hz": applied_settings.get("bandwidth_hz"),
            "agc": applied_settings.get("agc"),
            "gains": _known(gains) or None,
        }
    )


def requested_bucket(
    *,
    center_frequency_hz: float | None = None,
    sample_rate_hz: float | None = None,
    bandwidth_hz: float | None = None,
    if_gain_reduction_db: float | None = None,
    lna_state: int | None = None,
    agc: bool | None = None,
    antenna: str | None = None,
) -> dict[str, Any]:
    """The `requested` bucket: what this software asked the radio for."""
    return _known(
        {
            "center_frequency_hz": center_frequency_hz,
            "sample_rate_hz": sample_rate_hz,
            "bandwidth_hz": bandwidth_hz,
            "if_gain_reduction_db": if_gain_reduction_db,
            "lna_state": lna_state,
            "agc": agc,
            "antenna": antenna,
        }
    )


def declared_bucket(site: SiteProfile | None) -> dict[str, Any]:
    """The `declared` bucket: the site profile as it stood for this run.

    Snapshotted per run on purpose. `sites` holds one mutable row that
    `upsert_site` rewrites, so without this a profile edited next month would
    retroactively change the receiver, antenna and gain every earlier run
    appears to have been taken with.
    """
    if site is None:
        return {}
    return _known(
        {
            "site_id": site.site_id,
            "receiver": site.receiver,
            "antenna": site.antenna,
            "gain_mode": site.gain_mode,
            "gain": site.gain,
            "lna_state": site.lna_state,
        }
    )


def with_declared(hardware: Any, declared: Any) -> dict[str, Any]:
    """Attach a declaration to a blob, leaving its measured buckets alone."""
    base = normalise_hardware(hardware)
    return hardware_provenance(
        identity=base.get("identity"),
        applied=base.get("applied"),
        requested=base.get("requested"),
        declared=declared if declared else base.get("declared"),
    )


def hardware_from_capture_manifest(manifest: Any) -> dict[str, Any]:
    """Provenance for a capture this software ran, from its capture report.

    `device_settings_applied` is the radio's own read-back, so it is the only
    thing filed as `applied`. A manifest that is malformed, or whose settings
    are not a mapping, yields an empty blob rather than a half-read one: a
    report nobody can parse is not evidence about a radio, and reading half
    of one would file a read-back under a request nobody can see.
    """
    if not isinstance(manifest, dict):
        return hardware_provenance()
    settings = manifest.get("settings")
    if not isinstance(settings, dict):
        return hardware_provenance()
    return hardware_provenance(
        identity={"driver": settings.get("driver"), "serial": settings.get("serial")},
        applied=applied_bucket(manifest.get("device_settings_applied")),
        requested=requested_bucket(
            center_frequency_hz=settings.get("center_frequency_hz"),
            sample_rate_hz=settings.get("sample_rate_hz"),
            bandwidth_hz=settings.get("bandwidth_hz"),
            if_gain_reduction_db=settings.get("if_gain_reduction_db"),
            lna_state=settings.get("lna_state"),
            agc=settings.get("agc"),
            antenna=settings.get("antenna"),
        ),
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
    if not isinstance(claimed, str) or not claimed:
        return None
    try:
        if Path(claimed).expanduser().resolve() != source:
            return None
    except OSError:
        return None
    return manifest


def hardware_from_recording(recording: str | Path) -> dict[str, Any]:
    """Provenance for a recording analysed after the fact.

    Nothing about the radio is known unless this very recording's own capture
    report is found beside it and names it back. Analysing a file someone
    handed over must not invent a receiver setting for it. Both the CLI and
    the field app route through here, so one file gets one answer whichever
    asks.
    """
    return hardware_from_capture_manifest(capture_report_for(recording))


# -- validating and reading a blob ------------------------------------------


def _validate_bucket(bucket: str, held: dict[Any, Any]) -> None:
    """Every value a report will read has to be one it can read.

    Structure alone is not enough. A blob whose `IFGR` is a list validates
    as a mapping of mappings and then raises inside whatever counts or
    formats it, which is a crash in a report rather than a rejected row.
    """
    for key, item in held.items():
        if not isinstance(key, str):
            raise ProvenanceError(
                f"hardware provenance bucket {bucket!r} has a non-string key {key!r}"
            )
        if bucket == "applied" and key == "gains":
            if not isinstance(item, dict):
                raise ProvenanceError(
                    f"hardware provenance gains must be a dict, got {type(item).__name__}"
                )
            for element, reading in item.items():
                if not isinstance(element, str) or not _is_scalar(reading):
                    raise ProvenanceError(
                        f"hardware provenance gain {element!r} must be a number or a name, "
                        f"got {type(reading).__name__}"
                    )
            continue
        if not _is_scalar(item):
            raise ProvenanceError(
                f"hardware provenance {bucket}.{key} must be a number, a name or a flag, "
                f"got {type(item).__name__}"
            )


def normalise_hardware(value: Any) -> dict[str, Any]:
    """Validate a provenance blob, structure and all, on its way into the
    database.

    `{}` is a legitimate value: it says this run recorded nothing about the
    receiver, which is true of every run written before this column existed.
    Anything non-empty must be a blob this module could have built -- every
    bucket a mapping, the schema version known, and `source` agreeing with the
    contents. A blob whose `source` overstates its buckets is the one failure
    this whole design exists to prevent, so it is rejected rather than
    repaired.
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
    for bucket in BUCKETS:
        held = value.get(bucket, {})
        if not isinstance(held, dict):
            raise ProvenanceError(
                f"hardware provenance bucket {bucket!r} must be a dict, "
                f"got {type(held).__name__}"
            )
        _validate_bucket(bucket, held)
    source = value.get("source")
    if source not in STORED_SOURCES:
        raise ProvenanceError(
            f"hardware provenance source must be one of {list(STORED_SOURCES)}, got {source!r}"
        )
    expected = _derive_source(
        value.get("applied") or {}, value.get("requested") or {}, value.get("declared") or {}
    )
    if source != expected:
        raise ProvenanceError(
            f"hardware provenance claims source {source!r} but its buckets say {expected!r}"
        )
    canonical: dict[str, Any] = {
        "schema_version": HARDWARE_SCHEMA_VERSION,
        "source": source,
    }
    for bucket in BUCKETS:
        held = dict(value.get(bucket) or {})
        if "gains" in held:
            held["gains"] = dict(held["gains"])
        canonical[bucket] = held
    return canonical


def load_hardware(raw: Any) -> dict[str, Any]:
    """Read a stored `hardware_json` back, defensively.

    Anything that does not validate reads as not recorded. A report about a
    campaign must not die on one malformed row, and a row that cannot be
    trusted to say what it means must not be believed either.
    """
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    try:
        return normalise_hardware(raw)
    except ProvenanceError:
        return {}


@dataclass(frozen=True, slots=True)
class Reading:
    """One receiver value together with the evidence behind it."""

    value: Any | None
    source: str

    @property
    def known(self) -> bool:
        return self.source != SOURCE_NOT_RECORDED

    @property
    def measured(self) -> bool:
        """True only where the radio itself reported the value back."""
        return self.source == SOURCE_APPLIED

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


def _bucket(hardware: Any, name: str) -> dict[str, Any]:
    if not isinstance(hardware, dict):
        return {}
    held = hardware.get(name)
    return held if isinstance(held, dict) else {}


def _gain_reading(
    hardware: Any, *, element: str, key: str, site_row: Any
) -> Reading:
    gains = _bucket(hardware, "applied").get("gains")
    if isinstance(gains, dict) and gains.get(element) is not None:
        return Reading(gains[element], SOURCE_APPLIED)
    requested = _bucket(hardware, "requested")
    if requested.get(key) is not None:
        return Reading(requested[key], SOURCE_REQUESTED)
    declared = _bucket(hardware, "declared")
    if declared.get(key if key != "if_gain_reduction_db" else "gain") is not None:
        name = key if key != "if_gain_reduction_db" else "gain"
        return Reading(declared[name], SOURCE_DECLARED)
    if site_row is not None:
        return Reading(site_row, SOURCE_DECLARED_SITE_ROW)
    return NOT_RECORDED


def if_gain_reading(hardware: Any, site_row: Any | None = None) -> Reading:
    """The IF gain reduction this run measured at, and how well it is known."""
    return _gain_reading(
        hardware, element=GAIN_ELEMENT_IF, key="if_gain_reduction_db", site_row=site_row
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


def lna_state_reading(hardware: Any, site_row: Any | None = None) -> Reading:
    """The LNA state this run measured at, and how well it is known."""
    reading = _gain_reading(
        hardware, element=GAIN_ELEMENT_RF, key="lna_state", site_row=site_row
    )
    if not reading.known:
        return reading
    return Reading(_as_lna_index(reading.value), reading.source)


@dataclass(frozen=True, slots=True)
class ReceiverSettings:
    """What one run's receiver was set to, each half with its own evidence."""

    survey_run_id: str
    if_gain_reduction: Reading
    lna_state: Reading

    @property
    def sources(self) -> tuple[str, str]:
        return self.if_gain_reduction.source, self.lna_state.source

    @property
    def from_legacy_site_row(self) -> bool:
        """True when a value could only be had from the mutable `sites` row."""
        return SOURCE_DECLARED_SITE_ROW in self.sources


def receiver_settings(row: Any) -> ReceiverSettings:
    """Resolve one run's receiver settings from the row that holds them.

    THE resolver. Every reader of a run's gain goes through here, so there is
    one precedence and one set of labels rather than a ladder per caller:

        applied -> requested -> declared -> the legacy `sites` row

    `applied` means the radio reported the value back; nothing else may ever
    be labelled that way. `declared` is the operator's own claim, snapshotted
    into the run when it was recorded, so editing a profile afterwards cannot
    rewrite what a run appears to have been taken with.

    The `sites` row is last and labelled apart because it is *current state*:
    `upsert_site` rewrites it on every run, so it describes the profile as it
    stands now, not as it stood for the run being read. It is offered only
    when the run's own blob says nothing at all -- which is exactly the case
    of a row written before runs carried their own declaration. A run WITH
    provenance never falls through to it.

    `row` needs `survey_run_id` and `hardware_json`; `gain` and `lna_state`
    are consulted only if the caller joined them in, and their absence simply
    removes the legacy tier.
    """
    hardware = load_hardware(_column(row, "hardware_json"))
    return ReceiverSettings(
        survey_run_id=str(_column(row, "survey_run_id")),
        if_gain_reduction=if_gain_reading(hardware, site_row=_column(row, "gain")),
        lna_state=lna_state_reading(hardware, site_row=_column(row, "lna_state")),
    )


def _column(row: Any, name: str) -> Any:
    """One column, or `None` when the caller did not select it.

    `sqlite3.Row` raises `IndexError` for a column that is not in the query
    rather than returning `None`, and a mapping raises `KeyError`; both mean
    the same thing here -- the caller did not ask for it, so that tier of
    evidence is simply not available.
    """
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return None


def identity_value(hardware: Any, key: str) -> Any | None:
    """A value from the `identity` bucket, or `None`.

    Identity carries no reading tier: a driver name is what the software
    opened the device as, not a quantity anything measured.
    """
    return _bucket(hardware, "identity").get(key)


def declared_value(hardware: Any, key: str) -> Any | None:
    """A value from this run's own snapshot of the site profile."""
    return _bucket(hardware, "declared").get(key)


def hardware_source_label(hardware: Any) -> str:
    """How this run's receiver state is known, in words."""
    if not isinstance(hardware, dict) or not hardware:
        return SOURCE_LABELS[SOURCE_NOT_RECORDED]
    return SOURCE_LABELS.get(hardware.get("source", SOURCE_NOT_RECORDED), "unknown")


__all__ = [
    "BUCKETS",
    "CAPTURE_REPORT_SUFFIX",
    "GAIN_ELEMENT_IF",
    "GAIN_ELEMENT_RF",
    "HARDWARE_SCHEMA_VERSION",
    "NOT_RECORDED",
    "ReceiverSettings",
    "SOURCE_APPLIED",
    "SOURCE_DECLARED",
    "SOURCE_DECLARED_SITE_ROW",
    "SOURCE_LABELS",
    "SOURCE_NOT_RECORDED",
    "SOURCE_REQUESTED",
    "STORED_SOURCES",
    "ProvenanceError",
    "Reading",
    "applied_bucket",
    "capture_report_for",
    "declared_bucket",
    "declared_value",
    "hardware_from_capture_manifest",
    "hardware_from_recording",
    "hardware_provenance",
    "hardware_source_label",
    "identity_value",
    "if_gain_reading",
    "lna_state_reading",
    "load_hardware",
    "normalise_campaign_id",
    "normalise_hardware",
    "receiver_settings",
    "requested_bucket",
    "with_declared",
]
