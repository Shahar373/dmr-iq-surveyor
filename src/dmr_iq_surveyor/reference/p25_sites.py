"""Parse an external P25 site snapshot into validated registry records.

This is *reference* data: it was produced somewhere else (an earlier
decoding session, a published system list) and is imported after the fact.
It never influences what the RF detector looks for -- `survey/` does not
import this package, and measurements are matched against it only once
observations are already stored. See `docs/phase7-geolocation-design.md`.

The snapshot's own uncertainty is preserved rather than flattened. A site
with no control-channel frequency stays in the registry with no channel; a
frequency used by two sites stays attached to both. Both facts are what
later make a measurement honestly unusable, so losing them here would
silently manufacture confidence downstream.
"""

from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Columns the snapshot format is required to carry. Extra columns are kept
# out of the registry rather than guessed at.
REQUIRED_COLUMNS = frozenset(
    {
        "wacn_hex",
        "system_id_hex",
        "rfss",
        "site",
        "observation_status",
        "primary_cc_mhz",
        "nac_hex",
    }
)

# Statuses the snapshot may report. `DIRECT` means the source observed the
# site's own control channel; `NEIGHBOR_ONLY` means it was only ever seen
# advertised by another site. Neither is evidence about *this* project's
# recordings -- it is provenance carried through from the snapshot.
OBSERVATION_STATUSES = frozenset({"DIRECT", "NEIGHBOR_ONLY"})

CHANNEL_ROLE_PRIMARY_CONTROL = "primary_control"
CHANNEL_EVIDENCE_SNAPSHOT = "external_snapshot"


class ReferenceError(ValueError):
    """Raised for a malformed or unusable reference snapshot."""


@dataclass(slots=True)
class P25SiteRecord:
    wacn_hex: str
    system_id_hex: str
    rfss: int
    site: int
    observation_status: str
    nac_hex: str | None
    control_frequency_hz: float | None
    notes: str = ""

    @property
    def site_key(self) -> str:
        """Stable human-facing identity, e.g. `BEE00:37D:1:30`."""
        return f"{self.wacn_hex}:{self.system_id_hex}:{self.rfss}:{self.site}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["site_key"] = self.site_key
        return payload


@dataclass(slots=True)
class P25SiteSnapshot:
    source_kind: str
    records: list[P25SiteRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def sites_with_frequency(self) -> int:
        return sum(1 for record in self.records if record.control_frequency_hz is not None)

    def reused_frequencies_hz(self, tolerance_hz: float = 1.0) -> dict[float, list[str]]:
        """Control frequencies claimed by more than one site.

        Returned keyed by the first frequency seen in each cluster, mapping
        to every site key using it. A measurement on one of these cannot be
        attributed to a single site -- this is what makes that detectable
        without re-deriving it at every call site.
        """
        clusters: dict[float, list[str]] = {}
        for record in self.records:
            if record.control_frequency_hz is None:
                continue
            for existing in clusters:
                if abs(existing - record.control_frequency_hz) <= tolerance_hz:
                    clusters[existing].append(record.site_key)
                    break
            else:
                clusters[record.control_frequency_hz] = [record.site_key]
        return {
            frequency: sorted(keys) for frequency, keys in clusters.items() if len(keys) > 1
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "record_count": len(self.records),
            "sites_with_frequency": self.sites_with_frequency,
            "reused_frequencies_hz": {
                str(frequency): keys for frequency, keys in self.reused_frequencies_hz().items()
            },
            "warnings": list(self.warnings),
            "records": [record.to_dict() for record in self.records],
        }


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _parse_int(value: str, *, column: str, row_number: int) -> int:
    try:
        return int(_clean(value))
    except (TypeError, ValueError) as exc:
        raise ReferenceError(
            f"row {row_number}: column {column!r} must be an integer, got {value!r}"
        ) from exc


def _parse_optional_frequency_hz(value: str, *, row_number: int) -> float | None:
    """Parse a `primary_cc_mhz` cell into Hz.

    An empty cell is not an error and not a zero: the snapshot genuinely
    does not know the site's control channel. It becomes `None`, and the
    site is later reported `frequency_unknown` rather than measured at
    0 Hz or dropped.
    """
    text = _clean(value)
    if not text:
        return None
    try:
        megahertz = float(text)
    except ValueError as exc:
        raise ReferenceError(
            f"row {row_number}: primary_cc_mhz must be a number or empty, got {value!r}"
        ) from exc
    if megahertz <= 0:
        raise ReferenceError(f"row {row_number}: primary_cc_mhz must be positive, got {value!r}")
    # Round to the nearest hertz: a MHz string like 866.712500 is exact to
    # 100 Hz, and float multiplication otherwise leaves 866712499.9999999,
    # which then fails exact-frequency joins downstream.
    return float(round(megahertz * 1_000_000.0))


def _normalise_hex(value: str) -> str | None:
    text = _clean(value).upper().removeprefix("0X")
    return text or None


def parse_p25_site_csv(text: str, *, source_kind: str = "p25_observed_sites_csv") -> P25SiteSnapshot:
    """Parse the P25 observed-site CSV format into a `P25SiteSnapshot`.

    Raises `ReferenceError` for a structurally invalid file (missing
    columns, non-numeric identifiers, duplicate site rows). Recoverable
    oddities -- an unrecognised observation status, a site with no
    frequency -- are recorded as warnings so they stay visible instead of
    failing an entire import.
    """
    # Strip a UTF-8 BOM: spreadsheet exports routinely carry one, and it
    # would otherwise turn the first header into "\ufeffwacn_hex" and fail
    # the required-column check with a confusing message.
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames is None:
        raise ReferenceError("reference snapshot is empty")
    present = {name.strip() for name in reader.fieldnames if name}
    missing = REQUIRED_COLUMNS - present
    if missing:
        raise ReferenceError(f"reference snapshot is missing required columns: {sorted(missing)}")

    snapshot = P25SiteSnapshot(source_kind=source_kind)
    seen: dict[tuple[str, str, int, int], int] = {}
    for offset, row in enumerate(reader):
        row_number = offset + 2  # +1 for the header, +1 for 1-based counting
        wacn = _normalise_hex(row.get("wacn_hex", ""))
        system_id = _normalise_hex(row.get("system_id_hex", ""))
        if wacn is None or system_id is None:
            # A trailing blank line is normal in exported CSVs; a row with
            # real content but no system identity is not.
            if any(_clean(value) for value in row.values()):
                snapshot.warnings.append(
                    f"row {row_number}: skipped, missing wacn_hex or system_id_hex"
                )
            continue

        rfss = _parse_int(row.get("rfss", ""), column="rfss", row_number=row_number)
        site = _parse_int(row.get("site", ""), column="site", row_number=row_number)
        identity = (wacn, system_id, rfss, site)
        if identity in seen:
            raise ReferenceError(
                f"row {row_number}: duplicate site {wacn}:{system_id}:{rfss}:{site} "
                f"(already defined on row {seen[identity]})"
            )
        seen[identity] = row_number

        status = _clean(row.get("observation_status", "")).upper()
        if status not in OBSERVATION_STATUSES:
            snapshot.warnings.append(
                f"row {row_number}: unrecognised observation_status {status or '(empty)'!r}, "
                "stored verbatim"
            )
        frequency_hz = _parse_optional_frequency_hz(
            row.get("primary_cc_mhz", ""), row_number=row_number
        )
        record = P25SiteRecord(
            wacn_hex=wacn,
            system_id_hex=system_id,
            rfss=rfss,
            site=site,
            observation_status=status,
            nac_hex=_normalise_hex(row.get("nac_hex", "")),
            control_frequency_hz=frequency_hz,
            notes=_clean(row.get("notes", "")),
        )
        if frequency_hz is None:
            snapshot.warnings.append(
                f"{record.site_key}: no control-channel frequency on record; "
                "the site cannot be measured until one is discovered"
            )
        snapshot.records.append(record)

    if not snapshot.records:
        raise ReferenceError("reference snapshot contained no usable site rows")

    for frequency_hz, site_keys in snapshot.reused_frequencies_hz().items():
        snapshot.warnings.append(
            f"{frequency_hz / 1e6:.6f} MHz is claimed by {len(site_keys)} sites "
            f"({', '.join(site_keys)}); measurements on it cannot be attributed to one site"
        )
    return snapshot


def load_p25_site_csv(
    path: str | Path, *, source_kind: str = "p25_observed_sites_csv"
) -> P25SiteSnapshot:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return parse_p25_site_csv(
        resolved.read_text(encoding="utf-8-sig"), source_kind=source_kind
    )


__all__ = [
    "CHANNEL_EVIDENCE_SNAPSHOT",
    "CHANNEL_ROLE_PRIMARY_CONTROL",
    "OBSERVATION_STATUSES",
    "REQUIRED_COLUMNS",
    "P25SiteRecord",
    "P25SiteSnapshot",
    "ReferenceError",
    "load_p25_site_csv",
    "parse_p25_site_csv",
]
