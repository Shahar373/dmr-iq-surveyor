"""The field service with a Pi-side GNSS receiver and with gain calibration.

What is pinned here is the part the operator relies on without seeing it:
whose fix places a drive's windows, that a calibrated gain is only put in
force where it cannot mix two gains inside one campaign, and that the
progress of a calibration is reported step by step.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fixtures.device_probe import StubProbeRunner, present
from fixtures.geo_scenario import build_database
from fixtures.simulated_receiver import CENTER, RATE, TARGET, SimulatedReceiver

from dmr_iq_surveyor.capture.gnss import GNSS_SOURCE_USB, GnssFix
from dmr_iq_surveyor.web.service import FieldService, FieldSettings


class _StubGnss:
    """Stands in for the reader thread: the service only asks it for status."""

    def status(self) -> dict:
        return {"configured": "/dev/fake", "state": "fix", "error": ""}

    def stop(self) -> None:
        pass


def _service(tmp_path: Path, **overrides) -> FieldService:
    database = tmp_path / "db.sqlite3"
    build_database(database).close()
    base = {
        "database_path": database,
        "output_root": tmp_path / "out",
        "recordings_dir": tmp_path / "rec",
        "center_frequency_hz": CENTER,
        "sample_rate_hz": RATE,
        "if_gain_reduction_db": 40.0,
        "lna_state": 4,
        "campaign_id": "uhf_trial",
    }
    base.update(overrides)
    return FieldService(FieldSettings(**base), probe_runner=StubProbeRunner(present("fake SDR")))


def _fix(latitude: float = 32.07, longitude: float = 34.81) -> GnssFix:
    return GnssFix(latitude=latitude, longitude=longitude, speed_mps=12.0, course_deg=90.0,
                   satellites=9, hdop=0.9, altitude_m=40.0, time_utc="083559.00")


# -- GNSS --------------------------------------------------------------------


def test_without_a_gnss_device_nothing_changes_for_the_phone(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        answer = service.push_live_position({"latitude": 32.0, "longitude": 34.8})
        assert answer["accepted"] is True
        status = service.live_status()
        assert status["gnss"] is None
        assert status["position_source"] == "phone"
    finally:
        service.close()


def test_a_usb_fix_feeds_the_drive_and_the_phone_does_not_interleave(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._gnss = _StubGnss()
    try:
        service._accept_gnss_fix(_fix())
        assert service.live_status()["position_source"] == GNSS_SOURCE_USB
        assert service._live_position().latitude == 32.07

        answer = service.push_live_position({"latitude": 31.0, "longitude": 35.0})
        assert answer["accepted"] is False and answer["ignored"] == "usb_gnss_in_use"
        assert service._live_position().latitude == 32.07, "the phone must not move the car"
        assert service.live_status()["gnss"]["state"] == "fix"
    finally:
        service.close()


def test_the_phone_takes_over_when_the_usb_fix_goes_stale(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._gnss = _StubGnss()
    try:
        service._accept_gnss_fix(_fix())
        with service._live_lock:
            service._live_fix.at -= service.settings.live_max_position_age_seconds + 1
        answer = service.push_live_position({"latitude": 31.0, "longitude": 35.0})
        assert answer["accepted"] is True
        assert service.live_status()["position_source"] == "phone"
    finally:
        service.close()


def test_starting_a_drive_without_a_fix_names_the_usb_receiver(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._gnss = _StubGnss()
    try:
        try:
            service.start_live({})
        except ValueError as exc:
            assert "USB GPS" in str(exc)
        else:
            raise AssertionError("a drive with no fix must be refused")
    finally:
        service.close()


# -- gain calibration ----------------------------------------------------------


def _calibrate(service: FieldService, receiver: SimulatedReceiver) -> dict:
    service.calibration_device_factory = lambda: receiver
    job = service.start_gain_calibration({"target_frequencies_hz": [TARGET]})
    deadline = time.monotonic() + 60
    while not job.is_terminal() and time.monotonic() < deadline:
        time.sleep(0.05)
    snapshot = job.snapshot()
    assert snapshot["status"] == "succeeded", snapshot
    return {"job": job, "result": snapshot["result"]}


def test_calibration_applies_in_an_empty_campaign_and_records_why(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        outcome = _calibrate(service, SimulatedReceiver(signal_db=-75.0))
        result = outcome["result"]
        assert result["status"] == "ok" and result["method"] == "target_snr_plateau"
        assert result["applied"] is True
        recommended = result["recommended"]
        assert service.settings.if_gain_reduction_db == recommended["if_gain_reduction_db"]
        assert service.settings.lna_state == recommended["lna_state"]
        assert service.gain_origin and service.gain_origin.startswith("calibration")
        assert "lna_state:" in result["site_profile_yaml"]

        # The evidence is kept on disk, and the state the phone polls shows it.
        report = json.loads(Path(result["report_path"]).read_text())
        assert len(report["steps"]) == len(result["steps"]) > 10
        assert service.state()["calibration"]["recommended"] == recommended

        # Progress was reported step by step, rising.
        events = [e for e in outcome["job"].events if e.get("stage") == "calibrate"]
        steps = [e for e in events if (e.get("message") or "").startswith("step ")]
        assert len(steps) == len(result["steps"])
        progress = [e["progress"] for e in steps]
        assert progress == sorted(progress) and progress[-1] > 0.9
    finally:
        service.close()


def test_calibration_never_changes_gain_inside_a_campaign_with_runs(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        connection = service._reading(None)
        with connection as reader:
            columns = [row[1] for row in reader.execute("PRAGMA table_info(survey_runs)")]
            assert "campaign_id" in columns
        _insert_run(service.settings.database_path, campaign_id="uhf_trial")

        outcome = _calibrate(service, SimulatedReceiver(signal_db=-75.0))
        result = outcome["result"]
        assert result["status"] == "ok"
        if (result["recommended"]["if_gain_reduction_db"], result["recommended"]["lna_state"]) \
                != (40.0, 4):
            assert result["applied"] is False
            assert "uhf_trial" in result["apply_reason"]
        assert service.settings.if_gain_reduction_db == 40.0
        assert service.settings.lna_state == 4
        assert service.gain_origin is None
    finally:
        service.close()


def test_calibration_holds_the_device_like_a_capture(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        receiver = SimulatedReceiver(signal_db=-75.0)
        service.calibration_device_factory = lambda: receiver
        job = service.start_gain_calibration({"target_frequencies_hz": [TARGET]})
        try:
            service.start_gain_calibration({"target_frequencies_hz": [TARGET]})
        except RuntimeError as exc:
            assert "already running" in str(exc)
        else:
            if not job.is_terminal():
                raise AssertionError("a second calibration must not open the SDR concurrently")
        while not job.is_terminal():
            time.sleep(0.05)
        assert receiver.closed
    finally:
        service.close()


def _insert_run(database: Path, *, campaign_id: str) -> None:
    """One minimal survey run in `campaign_id`, through the real schema."""
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        columns = {
            row[1]: (row[2], row[3], row[4])
            for row in connection.execute("PRAGMA table_info(survey_runs)")
        }
        values: dict[str, object] = {}
        for name, (kind, not_null, default) in columns.items():
            if name == "survey_run_id":
                values[name] = "existing_run"
            elif name == "campaign_id":
                values[name] = campaign_id
            elif not_null and default is None:
                values[name] = 0 if "INT" in kind.upper() or "REAL" in kind.upper() else "x"
        if "site_id" in values:
            connection.execute(
                "INSERT OR IGNORE INTO sites (site_id) VALUES (?)", (values["site_id"],)
            )
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        connection.execute(f"INSERT INTO survey_runs ({names}) VALUES ({marks})",
                           list(values.values()))
        connection.commit()
    finally:
        connection.close()
