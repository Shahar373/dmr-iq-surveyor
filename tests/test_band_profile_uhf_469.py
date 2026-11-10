"""The shipped `uhf_469_narrow` profile works with the capture it documents.

The profile's header names one capture (2 MS/s centred at 469.250 MHz). A
profile whose range the documented capture cannot actually cover would make
every run report `partial` coverage, and a channel landing on the tuner's DC
spike would be flagged as a receiver artefact -- so both are pinned here on
the real survey pipeline rather than assumed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fixtures.synthetic import SyntheticTone, write_synthetic_iq_wav

from dmr_iq_surveyor.survey.pipeline import run_survey
from dmr_iq_surveyor.survey.profiles import SiteProfile, resolve_band_profile

REPO_ROOT = Path(__file__).resolve().parents[1]
CENTER_HZ = 469_250_000
SAMPLE_RATE_HZ = 2_000_000
CARRIER_HZ = 469_350_000


def test_profile_loads_and_sits_inside_the_documented_capture() -> None:
    profile = resolve_band_profile("uhf_469_narrow", base_dir=REPO_ROOT)
    assert profile.name == "uhf_469_narrow"
    assert profile.start_frequency_hz < CARRIER_HZ < profile.stop_frequency_hz
    assert profile.start_frequency_hz > CENTER_HZ - SAMPLE_RATE_HZ / 2
    assert profile.stop_frequency_hz < CENTER_HZ + SAMPLE_RATE_HZ / 2


def test_documented_capture_detects_a_continuous_carrier_at_469_350(tmp_path: Path) -> None:
    profile = resolve_band_profile("uhf_469_narrow", base_dir=REPO_ROOT)
    recording = tmp_path / "uhf.wav"
    write_synthetic_iq_wav(
        recording,
        sample_rate_hz=SAMPLE_RATE_HZ,
        center_frequency_hz=CENTER_HZ,
        duration_seconds=3.0,
        tones=[
            SyntheticTone(
                offset_hz=float(CARRIER_HZ - CENTER_HZ),
                amplitude=0.3,
                modulation_bandwidth_hz=8000.0,
            )
        ],
    )
    result = run_survey(
        recording,
        tmp_path / "out",
        band=profile,
        site=SiteProfile(site_id="mobile", label="mobile", gain=40.0, gain_mode="manual"),
        run_id="uhf_trial",
        database_path=tmp_path / "db.sqlite3",
    )

    assert result["coverage_status"] == "complete", result
    connection = sqlite3.connect(result["database_path"])
    connection.row_factory = sqlite3.Row
    observations = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM rf_observations WHERE survey_run_id = ?", (result["run_id"],)
        )
    ]
    connection.close()
    near = [
        o for o in observations if abs(float(o["measured_center_hz"]) - CARRIER_HZ) <= 6250.0
    ]
    assert near, f"no observation within 6.25 kHz of 469.350 MHz: {observations}"
    assert not near[0].get("dc_warning"), "the documented tuning must keep the carrier off DC"
