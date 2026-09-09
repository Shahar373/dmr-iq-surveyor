"""Band and site profiles for Phase 6 RF surveys.

A band profile describes *where to look* (a frequency range, raster
spacings, detector threshold overrides, segmentation parameters, and
comparison tolerances) -- never an expected frequency list or protocol.
A site profile records the fixed context of one measurement location
(antenna, receiver, gain) so runs from the same site are comparable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from dmr_iq_surveyor.detect.core import DetectionSettings

_DEFAULT_BAND_DIRS = ("config/bands",)
_DEFAULT_SITE_DIRS = ("config/sites",)


class ProfileError(ValueError):
    """Raised for an invalid or unresolvable band/site profile."""


@dataclass(slots=True)
class ComparisonTolerances:
    frequency_tolerance_hz: float = 6250.0
    snr_delta_db: float = 3.0
    occupancy_delta_pct: float = 10.0
    persistence_delta: float = 0.25
    analyzed_seconds_ratio_limit: float = 4.0

    def validate(self) -> None:
        if self.frequency_tolerance_hz < 0:
            raise ProfileError("comparison.frequency_tolerance_hz must be non-negative")
        if self.snr_delta_db < 0:
            raise ProfileError("comparison.snr_delta_db must be non-negative")
        if not 0.0 <= self.occupancy_delta_pct <= 100.0:
            raise ProfileError("comparison.occupancy_delta_pct must be in [0, 100]")
        if not 0.0 <= self.persistence_delta <= 1.0:
            raise ProfileError("comparison.persistence_delta must be in [0, 1]")
        if self.analyzed_seconds_ratio_limit < 1.0:
            raise ProfileError("comparison.analyzed_seconds_ratio_limit must be >= 1")

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(slots=True)
class BandProfile:
    name: str
    label: str
    start_frequency_hz: float
    stop_frequency_hz: float
    raster_spacings_hz: list[float] = field(default_factory=lambda: [12500.0, 6250.0])
    detection_overrides: dict[str, float] = field(default_factory=dict)
    segment_seconds: float | None = None
    segment_stride_seconds: float | None = None
    max_segments: int | None = None
    usable_passband_rolloff_db: float = 3.0
    comparison: ComparisonTolerances = field(default_factory=ComparisonTolerances)

    def validate(self) -> None:
        if self.stop_frequency_hz <= self.start_frequency_hz:
            raise ProfileError("stop_frequency_hz must exceed start_frequency_hz")
        if not self.raster_spacings_hz:
            raise ProfileError("raster_spacings_hz must not be empty")
        if any(spacing <= 0 for spacing in self.raster_spacings_hz):
            raise ProfileError("raster_spacings_hz entries must be positive")
        allowed = set(DetectionSettings.__dataclass_fields__)
        unknown = set(self.detection_overrides) - allowed
        if unknown:
            raise ProfileError(f"Unknown detection overrides: {sorted(unknown)}")
        if self.segment_seconds is not None and self.segment_seconds <= 0:
            raise ProfileError("segment_seconds must be positive when set")
        if self.segment_stride_seconds is not None and self.segment_stride_seconds <= 0:
            raise ProfileError("segment_stride_seconds must be positive when set")
        if self.max_segments is not None and self.max_segments < 1:
            raise ProfileError("max_segments must be positive when set")
        if self.usable_passband_rolloff_db <= 0:
            raise ProfileError("usable_passband_rolloff_db must be positive")
        self.comparison.validate()

    def detection_settings(self) -> DetectionSettings:
        """Build a DetectionSettings, applying this profile's overrides on top
        of the module defaults."""
        settings = DetectionSettings(**self.detection_overrides)
        settings.validate()
        return settings

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["comparison"] = self.comparison.to_dict()
        return payload


@dataclass(slots=True)
class SiteProfile:
    site_id: str
    label: str
    latitude: float | None = None
    longitude: float | None = None
    antenna: str | None = None
    receiver: str | None = None
    gain_mode: str | None = None
    gain: float | None = None
    # SDRplay LNA state (0..9-ish depending on model), the other half of a
    # fixed manual gain -- IFGR alone does not reproduce a receiver setting,
    # since the LNA stage sets the noise figure ahead of it. Optional and
    # additive: a site profile written before this field existed parses to
    # `None` here, exactly like an unset `gain`.
    lna_state: int | None = None
    notes: str = ""

    def validate(self) -> None:
        if not self.site_id.strip():
            raise ProfileError("site_id must not be empty")
        if self.latitude is not None and not -90.0 <= self.latitude <= 90.0:
            raise ProfileError("latitude must be in [-90, 90]")
        if self.longitude is not None and not -180.0 <= self.longitude <= 180.0:
            raise ProfileError("longitude must be in [-180, 180]")
        if self.lna_state is not None and self.lna_state < 0:
            raise ProfileError("lna_state must not be negative")

    @property
    def is_gain_comparable(self) -> bool:
        return self.gain_mode is not None and self.gain is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ProfileError(f"{path} must contain a YAML mapping")
    return raw


def _reject_unknown_keys(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ProfileError(f"Unknown keys in {where}: {sorted(unknown)}")


def load_band_profile(path: str | Path) -> BandProfile:
    source = Path(path).expanduser().resolve()
    raw = _load_yaml_mapping(source)
    raw = dict(raw)
    comparison_raw = raw.pop("comparison", {}) or {}
    if not isinstance(comparison_raw, dict):
        raise ProfileError("comparison must be a mapping")
    _reject_unknown_keys(
        comparison_raw,
        set(ComparisonTolerances.__dataclass_fields__),
        f"{source} comparison",
    )
    detection_raw = raw.pop("detection", {}) or {}
    if not isinstance(detection_raw, dict):
        raise ProfileError("detection must be a mapping")
    allowed = {item.name for item in fields(BandProfile)} - {"detection_overrides", "comparison"}
    _reject_unknown_keys(raw, allowed, str(source))
    if "name" not in raw or "label" not in raw:
        raise ProfileError(f"{source} requires 'name' and 'label'")
    if "start_frequency_hz" not in raw or "stop_frequency_hz" not in raw:
        raise ProfileError(f"{source} requires 'start_frequency_hz' and 'stop_frequency_hz'")
    profile = BandProfile(
        name=str(raw["name"]),
        label=str(raw["label"]),
        start_frequency_hz=float(raw["start_frequency_hz"]),
        stop_frequency_hz=float(raw["stop_frequency_hz"]),
        raster_spacings_hz=[float(v) for v in raw.get("raster_spacings_hz", [12500.0, 6250.0])],
        detection_overrides={key: float(value) for key, value in detection_raw.items()},
        segment_seconds=(
            float(raw["segment_seconds"]) if raw.get("segment_seconds") is not None else None
        ),
        segment_stride_seconds=(
            float(raw["segment_stride_seconds"])
            if raw.get("segment_stride_seconds") is not None
            else None
        ),
        max_segments=(
            int(raw["max_segments"]) if raw.get("max_segments") is not None else None
        ),
        usable_passband_rolloff_db=float(raw.get("usable_passband_rolloff_db", 3.0)),
        comparison=ComparisonTolerances(**comparison_raw),
    )
    profile.validate()
    return profile


def load_site_profile(path: str | Path) -> SiteProfile:
    source = Path(path).expanduser().resolve()
    raw = _load_yaml_mapping(source)
    allowed = set(SiteProfile.__dataclass_fields__)
    _reject_unknown_keys(raw, allowed, str(source))
    if "site_id" not in raw or "label" not in raw:
        raise ProfileError(f"{source} requires 'site_id' and 'label'")
    profile = SiteProfile(
        site_id=str(raw["site_id"]),
        label=str(raw["label"]),
        latitude=(float(raw["latitude"]) if raw.get("latitude") is not None else None),
        longitude=(float(raw["longitude"]) if raw.get("longitude") is not None else None),
        antenna=(str(raw["antenna"]) if raw.get("antenna") is not None else None),
        receiver=(str(raw["receiver"]) if raw.get("receiver") is not None else None),
        gain_mode=(str(raw["gain_mode"]) if raw.get("gain_mode") is not None else None),
        gain=(float(raw["gain"]) if raw.get("gain") is not None else None),
        lna_state=(int(raw["lna_state"]) if raw.get("lna_state") is not None else None),
        notes=str(raw.get("notes", "")),
    )
    profile.validate()
    return profile


def resolve_band_profile(
    band: str | Path,
    *,
    search_dirs: tuple[str, ...] = _DEFAULT_BAND_DIRS,
    base_dir: str | Path = ".",
) -> BandProfile:
    """Resolve a band profile from an explicit path, or by name under one of
    `search_dirs` (relative to `base_dir`), trying `<name>.yaml`."""
    candidate = Path(band).expanduser()
    if candidate.is_file():
        return load_band_profile(candidate)
    base = Path(base_dir).expanduser().resolve()
    for directory in search_dirs:
        guess = base / directory / f"{band}.yaml"
        if guess.is_file():
            return load_band_profile(guess)
    raise ProfileError(
        f"Could not resolve band profile {band!r}: not a file, and not found "
        f"as '<name>.yaml' under {[str(base / d) for d in search_dirs]}"
    )


def resolve_site_profile(
    site: str | Path,
    *,
    search_dirs: tuple[str, ...] = _DEFAULT_SITE_DIRS,
    base_dir: str | Path = ".",
) -> SiteProfile:
    """Resolve a site profile from an explicit path, or by name under one of
    `search_dirs` (relative to `base_dir`), trying `<name>.yaml`."""
    candidate = Path(site).expanduser()
    if candidate.is_file():
        return load_site_profile(candidate)
    base = Path(base_dir).expanduser().resolve()
    for directory in search_dirs:
        guess = base / directory / f"{site}.yaml"
        if guess.is_file():
            return load_site_profile(guess)
    raise ProfileError(
        f"Could not resolve site profile {site!r}: not a file, and not found "
        f"as '<name>.yaml' under {[str(base / d) for d in search_dirs]}"
    )


__all__ = [
    "BandProfile",
    "ComparisonTolerances",
    "ProfileError",
    "SiteProfile",
    "load_band_profile",
    "load_site_profile",
    "resolve_band_profile",
    "resolve_site_profile",
]
