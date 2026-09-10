"""Run provenance: campaign identity, and receiver state that says how well
it is known.

The rule these tests exist to hold: `applied` is only ever what the radio
reported back. A requested setting, and an operator's declaration in a site
profile, are different claims and must stay legible as different claims.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dmr_iq_surveyor.survey.discovery import RfObservation
from dmr_iq_surveyor.survey.profiles import SiteProfile
from dmr_iq_surveyor.survey.provenance import (
    HARDWARE_SCHEMA_VERSION,
    SOURCE_APPLIED,
    SOURCE_DECLARED,
    SOURCE_NOT_RECORDED,
    SOURCE_REQUESTED,
    ProvenanceError,
    capture_report_for,
    hardware_from_capture_manifest,
    hardware_from_recording,
    hardware_provenance,
    hardware_requested,
    identity_value,
    if_gain_reading,
    lna_state_reading,
    load_hardware,
    normalise_campaign_id,
    normalise_hardware,
)
from dmr_iq_surveyor.survey.store import (
    SurveyRunRecord,
    connect_survey_database,
    get_run,
    import_survey_run,
    upsert_site,
)

SITE = SiteProfile(site_id="home", label="Home")

# survey_runs exactly as it stood before campaign_id/hardware_json existed.
# Hand-written rather than generated so this test keeps describing the old
# shape after the live schema moves on.
PRE_PROVENANCE_SURVEY_RUNS_DDL = """
CREATE TABLE survey_runs (
    survey_run_id TEXT PRIMARY KEY,
    site_id TEXT REFERENCES sites(site_id),
    band_profile TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT,
    source_basename TEXT NOT NULL,
    center_frequency_hz REAL NOT NULL,
    sample_rate_hz REAL NOT NULL,
    capture_start_utc TEXT,
    capture_time_source TEXT NOT NULL,
    requested_start_hz REAL NOT NULL,
    requested_stop_hz REAL NOT NULL,
    usable_low_hz REAL,
    usable_high_hz REAL,
    coverage_status TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    analyzed_seconds REAL NOT NULL,
    segment_count INTEGER NOT NULL,
    occupancy_threshold_db REAL NOT NULL,
    detection_settings_json TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}',
    imported_at TEXT NOT NULL,
    status TEXT NOT NULL,
    gps_latitude REAL,
    gps_longitude REAL,
    gps_altitude_m REAL,
    gps_accuracy_m REAL,
    gps_source TEXT NOT NULL DEFAULT 'unknown',
    gps_fetched_at_utc TEXT
);
"""


def _observation(frequency_hz: float) -> RfObservation:
    return RfObservation(
        measured_center_hz=frequency_hz,
        bandwidth_hz=6000.0,
        peak_dbfs_per_hz=-40.0,
        average_dbfs_per_hz=-50.0,
        noise_floor_dbfs_per_hz=-90.0,
        power_unit="dbfs_per_hz",
        calibrated=False,
        snr_db=30.0,
        p95_snr_db=35.0,
        peak_concentration_db=10.0,
        occupancy_pct=15.0,
        occupancy_threshold_db=8.0,
        occupancy_sample_count=1000,
        persistence=1.0,
        segments_detected=5,
        segments_analyzed=5,
        equivalent_width_hz=3000.0,
        spectral_fill=0.5,
        symmetry=0.9,
        nearest_raster_hz=round(frequency_hz / 12500.0) * 12500.0,
        raster_spacing_hz=12500.0,
        raster_error_hz=0.0,
        spectral_class="narrowband_digital_candidate",
        classification="unknown",
        classification_confidence=0.8,
        classification_method="spectral_only",
        edge_warning=False,
        dc_warning=False,
    )


def _run_record(run_id: str, **overrides) -> SurveyRunRecord:
    fields = {
        "survey_run_id": run_id,
        "site_id": SITE.site_id,
        "band_profile": "test_band",
        "source_path": f"/tmp/{run_id}.wav",
        "source_sha256": None,
        "center_frequency_hz": 868_000_000.0,
        "sample_rate_hz": 200_000.0,
        "capture_start_utc": "2026-09-01T00:00:00+00:00",
        "capture_time_source": "auxi",
        "requested_start_hz": 867_800_000.0,
        "requested_stop_hz": 868_200_000.0,
        "usable_low_hz": 867_900_000.0,
        "usable_high_hz": 868_100_000.0,
        "coverage_status": "complete",
        "duration_seconds": 6.0,
        "analyzed_seconds": 6.0,
        "segment_count": 6,
        "occupancy_threshold_db": 8.0,
        "detection_settings": {"scan_step_hz": 6250.0},
        "tool_version": "0.10.0",
    }
    fields.update(overrides)
    return SurveyRunRecord(**fields)


def _capture_manifest(wav_path: Path, *, read_back: bool) -> dict:
    manifest = {
        "tool": "dmr-iq-surveyor",
        "wav_path": str(wav_path),
        "settings": {
            "center_frequency_hz": 867_406_250.0,
            "sample_rate_hz": 5_000_000.0,
            "if_gain_reduction_db": 40.0,
            "lna_state": 2,
            "agc": False,
            "driver": "sdrplay",
            "serial": "230405A498",
            "antenna": None,
            "bandwidth_hz": None,
        },
    }
    if read_back:
        manifest["device_settings_applied"] = {
            "sample_rate_hz": 5_000_000.0,
            "center_frequency_hz": 867_406_250.0,
            "bandwidth_hz": 5_000_000.0,
            "agc": False,
            "gains": {"IFGR": 25.0, "RFGR": 2.0},
        }
    return manifest


# -- campaign id -------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("day1", "day1"),
        ("  Day1  ", "day1"),
        ("2026-09_day1", "2026-09_day1"),
        ("a.b-c_d", "a.b-c_d"),
        (None, None),
        ("", None),
        ("   ", None),
    ],
)
def test_campaign_id_normalisation(given, expected) -> None:
    assert normalise_campaign_id(given) == expected


@pytest.mark.parametrize("given", ["day 1", "day/1", "-leading", "_leading", "a" * 65, "יום"])
def test_campaign_id_rejects_rather_than_mangles(given) -> None:
    """Silently rewriting an id would leave the operator's string and the
    stored string as two different things."""
    with pytest.raises(ProvenanceError):
        normalise_campaign_id(given)


def test_campaign_id_normalisation_is_stable() -> None:
    once = normalise_campaign_id("  Day1  ")
    assert normalise_campaign_id(once) == once


# -- hardware provenance shape ----------------------------------------------


def test_read_back_is_applied_and_requested_is_kept_apart(tmp_path: Path) -> None:
    hardware = hardware_from_capture_manifest(_capture_manifest(tmp_path / "a.wav", read_back=True))

    assert hardware["schema_version"] == HARDWARE_SCHEMA_VERSION
    assert hardware["source"] == SOURCE_APPLIED
    # The radio reported IFGR 25 while 40 was asked for. Both survive, and
    # neither is presented as the other.
    assert hardware["applied"]["gains"] == {"IFGR": 25.0, "RFGR": 2.0}
    assert hardware["requested"]["if_gain_reduction_db"] == 40.0
    assert hardware["identity"] == {"driver": "sdrplay", "serial": "230405A498"}


def test_a_capture_without_read_back_is_requested_not_applied(tmp_path: Path) -> None:
    hardware = hardware_from_capture_manifest(
        _capture_manifest(tmp_path / "a.wav", read_back=False)
    )

    assert hardware["source"] == SOURCE_REQUESTED
    assert hardware["applied"] == {}
    assert hardware["requested"]["if_gain_reduction_db"] == 40.0


def test_nothing_known_is_not_recorded() -> None:
    hardware = hardware_provenance()

    assert hardware["source"] == SOURCE_NOT_RECORDED
    assert hardware["applied"] == {}
    assert hardware["requested"] == {}
    assert hardware["identity"] == {}


def test_unknown_values_are_absent_rather_than_null() -> None:
    """A key holding `None` would read as a recorded value at a glance."""
    hardware = hardware_requested(driver="sdrplay", if_gain_reduction_db=26.0, lna_state=8)

    assert hardware["source"] == SOURCE_REQUESTED
    assert "serial" not in hardware["identity"]
    assert "antenna" not in hardware["requested"]
    assert hardware["requested"]["lna_state"] == 8


def test_normalise_hardware_refuses_a_declared_blob() -> None:
    """`declared` is a reading tier for the sites row, never provenance of a
    run's own receiver state."""
    blob = hardware_provenance(requested={"lna_state": 2})
    blob["source"] = SOURCE_DECLARED

    with pytest.raises(ProvenanceError, match="source"):
        normalise_hardware(blob)


def test_normalise_hardware_accepts_empty_and_refuses_foreign_shapes() -> None:
    assert normalise_hardware(None) == {}
    assert normalise_hardware({}) == {}
    with pytest.raises(ProvenanceError, match="schema_version"):
        normalise_hardware({"source": SOURCE_APPLIED, "schema_version": 99})
    with pytest.raises(ProvenanceError, match="dict"):
        normalise_hardware("IFGR 25")


def test_load_hardware_survives_a_malformed_row() -> None:
    assert load_hardware("{not json") == {}
    assert load_hardware("") == {}
    assert load_hardware(None) == {}
    assert load_hardware("[1, 2]") == {}
    assert load_hardware('{"source": "applied"}') == {"source": "applied"}


# -- capture report linkage --------------------------------------------------


def _write_pair(tmp_path: Path, *, wav_name: str, claimed: Path | None) -> Path:
    wav = tmp_path / wav_name
    wav.write_bytes(b"RIFF")
    report = tmp_path / f"{wav.stem}_capture_report.json"
    manifest = _capture_manifest(claimed if claimed is not None else wav, read_back=True)
    report.write_text(json.dumps(manifest), encoding="utf-8")
    return wav


def test_capture_report_is_used_only_when_it_names_the_recording_back(tmp_path: Path) -> None:
    wav = _write_pair(tmp_path, wav_name="stop.wav", claimed=None)

    assert capture_report_for(wav) is not None
    assert hardware_from_recording(wav)["source"] == SOURCE_APPLIED


def test_a_report_pointing_at_another_file_is_not_a_link(tmp_path: Path) -> None:
    """A neighbouring file with a matching name is a coincidence, and a
    coincidence must not become a gain the solver reads as distance."""
    wav = _write_pair(tmp_path, wav_name="stop.wav", claimed=tmp_path / "somewhere_else.wav")

    assert capture_report_for(wav) is None
    assert hardware_from_recording(wav)["source"] == SOURCE_NOT_RECORDED


def test_a_recording_with_no_report_invents_nothing(tmp_path: Path) -> None:
    lonely = tmp_path / "handed_over.wav"
    lonely.write_bytes(b"RIFF")

    assert capture_report_for(lonely) is None
    assert hardware_from_recording(lonely) == hardware_provenance()


def test_a_malformed_report_invents_nothing(tmp_path: Path) -> None:
    wav = tmp_path / "stop.wav"
    wav.write_bytes(b"RIFF")
    (tmp_path / "stop_capture_report.json").write_text("{truncated", encoding="utf-8")

    assert capture_report_for(wav) is None
    assert hardware_from_recording(wav)["source"] == SOURCE_NOT_RECORDED


# -- readings ----------------------------------------------------------------


def test_reading_prefers_applied_then_requested_then_declared(tmp_path: Path) -> None:
    applied = hardware_from_capture_manifest(_capture_manifest(tmp_path / "a.wav", read_back=True))
    requested = hardware_requested(if_gain_reduction_db=26.0, lna_state=8)

    assert if_gain_reading(applied, declared=40.0).source == SOURCE_APPLIED
    assert if_gain_reading(applied, declared=40.0).value == 25.0
    assert if_gain_reading(requested, declared=40.0).source == SOURCE_REQUESTED
    assert if_gain_reading({}, declared=40.0).source == SOURCE_DECLARED
    assert if_gain_reading({}).source == SOURCE_NOT_RECORDED
    assert lna_state_reading(applied).value == 2.0
    assert lna_state_reading(requested).value == 8
    assert identity_value(applied, "serial") == "230405A498"
    assert identity_value({}, "driver") is None


def test_an_lna_state_read_back_as_a_float_is_the_index_it_names(tmp_path: Path) -> None:
    """The radio reports the LNA state through a gain element, so it arrives
    as 2.0 where a site profile holds 2. Left alone, a campaign mixing the two
    would be warned as running at two different settings."""
    applied = hardware_from_capture_manifest(_capture_manifest(tmp_path / "a.wav", read_back=True))

    measured = lna_state_reading(applied).value
    assert measured == 2
    assert isinstance(measured, int)
    assert measured == lna_state_reading({}, declared=2).value
    # An index that is not one is shown as it arrived rather than rounded away.
    odd = hardware_provenance(applied={"gains": {"RFGR": 2.5}})
    assert lna_state_reading(odd).value == 2.5


def test_a_reading_always_says_how_well_it_is_known(tmp_path: Path) -> None:
    applied = hardware_from_capture_manifest(_capture_manifest(tmp_path / "a.wav", read_back=True))

    assert if_gain_reading(applied).render(" dB") == "25.0 dB (applied)"
    assert if_gain_reading(hardware_requested(if_gain_reduction_db=26.0)).render() == "26.0 (requested)"
    assert if_gain_reading({}, declared=40.0).render() == "40.0 (declared)"
    assert if_gain_reading({}).render() == "not recorded"


# -- migration and round trip ------------------------------------------------


def test_columns_are_added_to_a_pre_existing_survey_runs_table(tmp_path: Path) -> None:
    """The same additive contract already proven for the GPS columns, applied
    to these two: an existing Pi database upgrades in place, with no backfill
    and no change to the rows it already holds."""
    db_path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(db_path)
    old.executescript(
        "CREATE TABLE sites (site_id TEXT PRIMARY KEY, label TEXT NOT NULL,"
        " latitude REAL, longitude REAL, antenna TEXT, receiver TEXT,"
        " gain_mode TEXT, gain REAL, notes TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL);"
        + PRE_PROVENANCE_SURVEY_RUNS_DDL
    )
    old.execute(
        "INSERT INTO survey_runs(survey_run_id, site_id, band_profile, source_path,"
        " source_basename, center_frequency_hz, sample_rate_hz, capture_time_source,"
        " requested_start_hz, requested_stop_hz, coverage_status, duration_seconds,"
        " analyzed_seconds, segment_count, occupancy_threshold_db, detection_settings_json,"
        " tool_version, imported_at, status)"
        " VALUES ('legacy', 'home', 'central_800', '/tmp/legacy.wav', 'legacy.wav',"
        " 868000000.0, 5000000.0, 'auxi', 866000000.0, 870000000.0, 'complete',"
        " 90.0, 30.0, 30, 8.0, '{}', '0.9.0', '2026-08-01T00:00:00+00:00', 'ok')"
    )
    old.commit()
    old.close()

    connection = connect_survey_database(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(survey_runs)")}
        assert {"campaign_id", "hardware_json"} <= columns
        row = get_run(connection, "legacy")
        assert row is not None
        # Not backfilled into a "legacy" campaign, and no receiver state
        # invented for a run that never recorded one.
        assert row["campaign_id"] is None
        assert load_hardware(row["hardware_json"]) == {}
        assert row["tool_version"] == "0.9.0"
    finally:
        connection.close()


def test_campaign_and_hardware_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "round.sqlite3"
    connection = connect_survey_database(db_path)
    try:
        upsert_site(connection, SITE)
        hardware = hardware_from_capture_manifest(
            _capture_manifest(tmp_path / "a.wav", read_back=True)
        )
        import_survey_run(
            connection,
            run=_run_record("r1", campaign_id="  Day1 ", hardware=hardware),
            observations=[_observation(868_050_000.0)],
            raster_tolerance_hz=6250.0,
        )
        row = get_run(connection, "r1")
        assert row is not None
        assert row["campaign_id"] == "day1"
        stored = load_hardware(row["hardware_json"])
        assert stored == hardware
        assert if_gain_reading(stored).render() == "25.0 (applied)"
    finally:
        connection.close()


def test_a_run_without_a_campaign_stays_unassigned(tmp_path: Path) -> None:
    connection = connect_survey_database(tmp_path / "plain.sqlite3")
    try:
        upsert_site(connection, SITE)
        import_survey_run(
            connection,
            run=_run_record("r1"),
            observations=[_observation(868_050_000.0)],
            raster_tolerance_hz=6250.0,
        )
        row = get_run(connection, "r1")
        assert row is not None
        assert row["campaign_id"] is None
        assert row["hardware_json"] == "{}"
    finally:
        connection.close()


def test_the_store_is_the_one_place_a_campaign_id_is_enforced(tmp_path: Path) -> None:
    """Validation sits on the single write path, so no caller can route
    around it by building a record by hand."""
    connection = connect_survey_database(tmp_path / "bad.sqlite3")
    try:
        upsert_site(connection, SITE)
        with pytest.raises(ProvenanceError):
            import_survey_run(
                connection,
                run=_run_record("r1", campaign_id="day 1"),
                observations=[],
                raster_tolerance_hz=6250.0,
            )
    finally:
        connection.close()
