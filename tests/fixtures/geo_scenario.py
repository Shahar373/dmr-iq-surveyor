"""Synthetic multi-session geolocation scenarios for Phase 7 tests.

Builds survey runs and RF observations directly in the database, the same
shapes `survey run` would store, so geolocation can be tested end to end
without generating and analysing wideband IQ for every case.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dmr_iq_surveyor.geo.model import haversine_m
from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.reference.p25_sites import parse_p25_site_csv
from dmr_iq_surveyor.reference.store import import_snapshot
from dmr_iq_surveyor.survey.discovery import RfObservation
from dmr_iq_surveyor.survey.profiles import SiteProfile
from dmr_iq_surveyor.survey.store import SurveyRunRecord, import_survey_run, upsert_site

DETECTION_THRESHOLD_DB = 4.0

SITE_CSV = """wacn_hex,system_id_hex,rfss,site,observation_status,primary_cc_mhz,nac_hex,notes
BEE00,37D,1,30,DIRECT,867.762500,37B,
BEE00,37D,1,33,DIRECT,866.712500,377,
BEE00,37D,1,50,NEIGHBOR_ONLY,867.912500,,reuse with site 82
BEE00,37D,1,82,NEIGHBOR_ONLY,867.912500,,reuse with site 50
BEE00,37D,1,81,NEIGHBOR_ONLY,,,control channel never observed
"""


@dataclass(frozen=True, slots=True)
class Transmitter:
    frequency_hz: float
    latitude: float
    longitude: float
    reference_level_db: float = 30.0
    path_loss_exponent: float = 3.2


def _observation(frequency_hz: float, snr_db: float) -> RfObservation:
    raster = round(frequency_hz / 6250.0) * 6250.0
    return RfObservation(
        measured_center_hz=frequency_hz,
        bandwidth_hz=12500.0,
        peak_dbfs_per_hz=-40.0,
        average_dbfs_per_hz=-50.0,
        noise_floor_dbfs_per_hz=-90.0,
        power_unit="dbfs_per_hz",
        calibrated=False,
        snr_db=snr_db,
        p95_snr_db=snr_db + 4.0,
        peak_concentration_db=8.0,
        occupancy_pct=95.0,
        occupancy_threshold_db=8.0,
        occupancy_sample_count=1000,
        persistence=1.0,
        segments_detected=10,
        segments_analyzed=10,
        equivalent_width_hz=8000.0,
        spectral_fill=0.6,
        symmetry=0.95,
        nearest_raster_hz=raster,
        raster_spacing_hz=6250.0,
        raster_error_hz=frequency_hz - raster,
        spectral_class="narrowband_digital_candidate",
        classification="unknown",
        classification_confidence=0.8,
        classification_method="spectral_only",
        edge_warning=False,
        dc_warning=False,
    )


def predicted_level_db(transmitter: Transmitter, latitude: float, longitude: float) -> float:
    distance = max(haversine_m(latitude, longitude, transmitter.latitude, transmitter.longitude), 50.0)
    return transmitter.reference_level_db - 10.0 * transmitter.path_loss_exponent * math.log10(
        distance / 1000.0
    )


def seed_run(
    connection: Any,
    *,
    run_id: str,
    latitude: float | None,
    longitude: float | None,
    transmitters: list[Transmitter],
    usable_low_hz: float = 866_000_000.0,
    usable_high_hz: float = 869_000_000.0,
    capture_start_utc: str = "2026-08-01T10:00:00+00:00",
    site_id: str = "mobile",
    gain: float | None = 40.0,
    threshold_db: float = DETECTION_THRESHOLD_DB,
    coverage_status: str = "complete",
) -> list[float]:
    """Store one survey run whose observations follow the given transmitters.

    Returns the frequencies that ended up detected, so a test can assert
    against the same physics the scenario generated.
    """
    upsert_site(
        connection,
        SiteProfile(site_id=site_id, label=site_id, gain=gain, gain_mode="manual"),
    )
    observations = []
    detected: list[float] = []
    for transmitter in transmitters:
        if latitude is None or longitude is None:
            continue
        if not usable_low_hz <= transmitter.frequency_hz <= usable_high_hz:
            continue
        level = predicted_level_db(transmitter, latitude, longitude)
        if level >= threshold_db:
            observations.append(_observation(transmitter.frequency_hz, level))
            detected.append(transmitter.frequency_hz)
    record = SurveyRunRecord(
        survey_run_id=run_id,
        site_id=site_id,
        band_profile="central_800",
        source_path=f"/tmp/{run_id}.wav",
        source_sha256=None,
        center_frequency_hz=867_500_000.0,
        sample_rate_hz=5_000_000.0,
        capture_start_utc=capture_start_utc,
        capture_time_source="auxi",
        requested_start_hz=866_000_000.0,
        requested_stop_hz=870_000_000.0,
        usable_low_hz=usable_low_hz,
        usable_high_hz=usable_high_hz,
        coverage_status=coverage_status,
        duration_seconds=120.0,
        analyzed_seconds=24.0,
        segment_count=12,
        occupancy_threshold_db=8.0,
        detection_settings={
            "min_average_channel_snr_db": threshold_db,
            "min_p95_channel_snr_db": threshold_db + 5.0,
        },
        tool_version="test",
        gps_latitude=latitude,
        gps_longitude=longitude,
        gps_source="user" if latitude is not None else "not_configured",
    )
    import_survey_run(
        connection, run=record, observations=observations, raster_tolerance_hz=6250.0
    )
    return detected


def build_database(path: Path) -> Any:
    connection = connect_geo_database(path)
    import_snapshot(
        connection,
        snapshot_id="test_snapshot",
        snapshot=parse_p25_site_csv(SITE_CSV),
        source_path="test.csv",
    )
    return connection




def fast_solve_settings(**overrides: Any) -> Any:
    """Solve settings tuned for tests: a small region at coarse resolution.

    Tests assert on behaviour -- which status is returned, whether a region
    shrinks -- not on metre-level precision, so the grid can be small enough
    to keep the suite quick.
    """
    from dmr_iq_surveyor.geo.model import SolveSettings

    defaults: dict[str, Any] = {
        "margin_m": 12_000.0,
        "coarse_resolution_m": 750.0,
        "resolution_m": 250.0,
        "max_coarse_cells": 20_000,
        "max_fine_cells": 20_000,
        "target_fine_cells": 6_000,
        "min_resolution_m": 50.0,
        "path_loss_exponents": (2.5, 3.0, 3.5, 4.0),
        "reference_level_samples": 17,
    }
    defaults.update(overrides)
    return SolveSettings(**defaults)


__all__ = [
    "DETECTION_THRESHOLD_DB",
    "SITE_CSV",
    "Transmitter",
    "build_database",
    "fast_solve_settings",
    "predicted_level_db",
    "seed_run",
]
