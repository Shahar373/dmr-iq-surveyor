"""The real chain, end to end: synthetic IQ -> survey run -> measurements -> solve.

Every other Phase 7 test seeds the database directly, which is fast but
cannot catch a mismatch between what `run_survey` actually stores and what
`geo/measurements.py` reads. That gap would not fail loudly -- it would
produce zero usable measurements, in the field, after a day of driving. So
this module drives the genuine pipeline on synthetic recordings instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fixtures.synthetic import SyntheticTone, write_synthetic_iq_wav

from dmr_iq_surveyor.geo.measurements import USABILITY_USABLE
from dmr_iq_surveyor.geo.pipeline import (
    import_reference_sites,
    materialise_measurements,
    solve_all_sites,
)
from dmr_iq_surveyor.geo.store import connect_geo_database, fetch_all_measurements
from dmr_iq_surveyor.survey.pipeline import run_survey
from dmr_iq_surveyor.survey.profiles import BandProfile, SiteProfile

SAMPLE_RATE_HZ = 200_000
CENTER_HZ = 868_000_000

# Two "control channels" either side of centre, and a third the registry
# knows about but the recording will never contain -- so the run produces a
# detection, a detection, and a genuine non-detection.
HEARD_A_HZ = CENTER_HZ + 50_000
HEARD_B_HZ = CENTER_HZ - 70_000
SILENT_HZ = CENTER_HZ + 20_000

SITE_CSV = (
    "wacn_hex,system_id_hex,rfss,site,observation_status,primary_cc_mhz,nac_hex,notes\n"
    f"BEE00,37D,1,10,DIRECT,{HEARD_A_HZ / 1e6:.6f},37B,\n"
    f"BEE00,37D,1,11,DIRECT,{HEARD_B_HZ / 1e6:.6f},37B,\n"
    f"BEE00,37D,1,12,NEIGHBOR_ONLY,{SILENT_HZ / 1e6:.6f},,never transmitted in the fixture\n"
)


def _band() -> BandProfile:
    return BandProfile(
        name="integration_band",
        label="integration",
        start_frequency_hz=CENTER_HZ - 90_000.0,
        stop_frequency_hz=CENTER_HZ + 90_000.0,
        raster_spacings_hz=[12500.0, 6250.0],
        detection_overrides={
            "scan_step_hz": 6250.0,
            "integration_width_hz": 12500.0,
            "min_p95_channel_snr_db": 9.0,
            "min_average_channel_snr_db": 4.0,
            "min_equivalent_width_hz": 1500.0,
            "min_width_90_hz": 1000.0,
            "max_width_90_hz": 13000.0,
            "merge_tolerance_hz": 4000.0,
            "passband_warning_low_hz": 866_000_000.0,
            "passband_warning_high_hz": 870_000_000.0,
        },
        segment_seconds=1.0,
        segment_stride_seconds=1.0,
        max_segments=6,
    )


def _record(path: Path, *, amplitude_a: float, amplitude_b: float, hour: int) -> None:
    write_synthetic_iq_wav(
        path,
        sample_rate_hz=SAMPLE_RATE_HZ,
        center_frequency_hz=CENTER_HZ,
        duration_seconds=4.0,
        tones=[
            SyntheticTone(offset_hz=float(HEARD_A_HZ - CENTER_HZ), amplitude=amplitude_a),
            SyntheticTone(offset_hz=float(HEARD_B_HZ - CENTER_HZ), amplitude=amplitude_b),
        ],
        capture_start_utc=datetime(2026, 8, 1, hour, 0, 0, tzinfo=UTC),
    )


def _survey(tmp_path: Path, database: Path, *, run_id: str, latitude: float, longitude: float,
            amplitude_a: float, amplitude_b: float, hour: int) -> dict:
    recording = tmp_path / f"{run_id}.wav"
    _record(recording, amplitude_a=amplitude_a, amplitude_b=amplitude_b, hour=hour)
    return run_survey(
        recording,
        tmp_path / run_id,
        band=_band(),
        site=SiteProfile(site_id=run_id, label=run_id, gain=40.0, gain_mode="manual"),
        run_id=run_id,
        database_path=database,
        gps_latitude=latitude,
        gps_longitude=longitude,
        gps_source="user",
    )


def test_the_real_survey_pipeline_feeds_geolocation(tmp_path: Path) -> None:
    """The columns run_survey writes are the columns measurements.py reads.

    Guards the seam the fixture-based tests cannot see: detection settings,
    the measured usable passband, the GPS columns and the measured centre all
    have to line up, or a whole campaign yields nothing usable.
    """
    database = tmp_path / "db.sqlite3"
    connect_geo_database(database).close()
    csv_path = tmp_path / "sites.csv"
    csv_path.write_text(SITE_CSV, encoding="utf-8")
    import_reference_sites(csv_path, database_path=database, snapshot_id="integration")

    result = _survey(
        tmp_path,
        database,
        run_id="stop_a",
        latitude=32.050,
        longitude=34.800,
        amplitude_a=0.35,
        amplitude_b=0.35,
        hour=12,
    )
    assert result["observation_count"] >= 2

    measurements = materialise_measurements(database_path=database)
    summary = measurements["summary"]
    assert summary["detections"] >= 2, (
        "the real pipeline must produce detections geolocation can use; "
        f"got {summary}"
    )
    assert summary["non_detections"] >= 1, (
        "a registry frequency inside the measured passband with nothing on it "
        "must become a usable non-detection"
    )

    connection = connect_geo_database(database)
    rows = fetch_all_measurements(connection)
    connection.close()

    by_site = {row["site_key"]: row for row in rows}
    assert by_site["BEE00:37D:1:10"]["detected"] == 1
    assert by_site["BEE00:37D:1:11"]["detected"] == 1
    assert by_site["BEE00:37D:1:12"]["detected"] == 0

    heard = by_site["BEE00:37D:1:10"]
    assert heard["usability"] == USABILITY_USABLE
    assert heard["level_db"] is not None and heard["level_db"] > heard["censor_level_db"]
    assert heard["latitude"] == 32.050 and heard["longitude"] == 34.800
    assert heard["position_source"] == "run_gps"
    # The measured centre must land on the registry frequency within tolerance,
    # which is what proves the frequency match is real and not accidental.
    assert abs(heard["measured_center_hz"] - HEARD_A_HZ) <= 6250.0
    assert heard["censor_level_db"] > 0.0, "a censoring level of 0 dB would mean the run's detection settings were not read"


def test_a_multi_stop_campaign_solves_from_real_recordings(tmp_path: Path) -> None:
    """Several real survey runs at different places produce a real solution."""
    database = tmp_path / "db.sqlite3"
    connect_geo_database(database).close()
    csv_path = tmp_path / "sites.csv"
    csv_path.write_text(SITE_CSV, encoding="utf-8")
    import_reference_sites(csv_path, database_path=database, snapshot_id="integration")

    stops = [
        ("stop_0", 32.050, 34.800, 0.40, 0.40),
        ("stop_1", 32.070, 34.820, 0.30, 0.30),
        ("stop_2", 32.030, 34.830, 0.22, 0.22),
        ("stop_3", 32.060, 34.770, 0.26, 0.26),
    ]
    for index, (run_id, latitude, longitude, amplitude_a, amplitude_b) in enumerate(stops):
        _survey(
            tmp_path,
            database,
            run_id=run_id,
            latitude=latitude,
            longitude=longitude,
            amplitude_a=amplitude_a,
            amplitude_b=amplitude_b,
            hour=10 + index,
        )

    materialise_measurements(database_path=database)
    report = solve_all_sites(
        database_path=database,
        output_root=tmp_path / "geo",
        settings=__import__(
            "fixtures.geo_scenario", fromlist=["fast_solve_settings"]
        ).fast_solve_settings(),
    )
    solutions = {row["site_key"]: row for row in report["solutions"]}

    # Every registry site is accounted for, with an explicit status.
    assert set(solutions) == {"BEE00:37D:1:10", "BEE00:37D:1:11", "BEE00:37D:1:12"}
    for solution in solutions.values():
        assert solution["status"]

    heard = solutions["BEE00:37D:1:10"]
    assert heard["detection_count"] == len(stops), (
        "a channel present in every recording must be detected at every stop"
    )
    silent = solutions["BEE00:37D:1:12"]
    assert silent["detection_count"] == 0
    assert silent["non_detection_count"] == len(stops)
    assert silent["status"] == "insufficient_evidence", (
        "a site never heard must be refused, not located"
    )
