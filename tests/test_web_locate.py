"""The field service's Locate target: choosing a frequency switches what is written.

Pinned here: choosing a target points every later write at that target's
campaign, tuning, band profile and gain; the switch never happens while a job
is running or from a read-only view; a calibration of the active target is
remembered against its campaign, so a restarted service resumes it at the same
gain; and a service that never chooses a target behaves exactly as before.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fixtures.device_probe import StubProbeRunner, present
from fixtures.geo_scenario import build_database
from fixtures.simulated_receiver import CENTER, RATE, TARGET, SimulatedReceiver

from dmr_iq_surveyor.geo.locate import connect_locate_database, describe_target
from dmr_iq_surveyor.web.service import FieldService, FieldSettings
from dmr_iq_surveyor.web.viewscope import ReadOnlyViewError, parse_view_scope


def _service(tmp_path: Path, *, build: bool = True, **overrides) -> FieldService:
    database = tmp_path / "db.sqlite3"
    if build:
        build_database(database).close()
    base = {
        "database_path": database,
        "output_root": tmp_path / "out",
        "recordings_dir": tmp_path / "rec",
        "band": "central_800",
        "center_frequency_hz": 867_406_250.0,
        "sample_rate_hz": 5_000_000.0,
        "if_gain_reduction_db": 40.0,
        "lna_state": 4,
        "campaign_id": "startup_round",
    }
    base.update(overrides)
    return FieldService(FieldSettings(**base), probe_runner=StubProbeRunner(present("fake SDR")))


# The simulated receiver is fixed at 1 MS/s around 469.25 MHz, so the tests
# choose that tuning; the automatic one (2 MS/s) is pinned in test_geo_locate.
_SIM_TUNING = {"center_frequency_hz": CENTER, "sample_rate_hz": RATE}


def _wait(job, seconds: float = 60.0) -> dict:
    deadline = time.monotonic() + seconds
    while not job.is_terminal() and time.monotonic() < deadline:
        time.sleep(0.05)
    return job.snapshot()


def test_choosing_a_target_switches_campaign_tuning_profile_and_gain(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        answer = service.select_locate_target({"frequency_mhz": 469.35, "label": "cc"})
        active = answer["active"]
        assert active["campaign_id"] == "loc-f469_350000"
        settings = service.settings
        assert settings.capture_campaign_id == "loc-f469_350000"
        assert settings.band == active["band_profile_path"]
        assert settings.center_frequency_hz == 469_250_000.0
        assert settings.sample_rate_hz == 2_000_000.0
        assert (settings.if_gain_reduction_db, settings.lna_state) == (40.0, 4)
        assert service.gain_origin.startswith("Locate target f469_350000")
        state = service.state()
        assert state["locate"]["active"]["emitter_key"] == "f469_350000"
        assert state["scope"]["capture_campaign_id"] == "loc-f469_350000"
        assert [row["campaign_id"] for row in service.locate_targets()["targets"]] == [
            "loc-f469_350000"
        ]
    finally:
        service.close()


def test_without_a_target_nothing_changes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        assert service.state()["locate"] == {"active": None}
        assert service.settings.capture_campaign_id == "startup_round"
        assert service.settings.band == "central_800"
        assert service.locate_targets() == {"active": None, "targets": []}
    finally:
        service.close()


def test_the_target_never_changes_while_a_job_runs(tmp_path: Path) -> None:
    service = _service(tmp_path)
    release = threading.Event()
    try:
        job = service.jobs.submit(
            kind="solve", label="hold", work=lambda job: (release.wait(10), {})[1]
        )
        with pytest.raises(RuntimeError, match="job is running"):
            service.select_locate_target({"frequency_mhz": 469.35})
        assert service.settings.capture_campaign_id == "startup_round"
        release.set()
        _wait(job)
        service.select_locate_target({"frequency_mhz": 469.35})
        assert service.settings.capture_campaign_id == "loc-f469_350000"
    finally:
        release.set()
        service.close()


def test_a_read_only_view_cannot_choose_a_target(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        view = parse_view_scope("all", capture_campaign_id="startup_round")
        with pytest.raises(ReadOnlyViewError):
            service.select_locate_target({"frequency_mhz": 469.35}, view)
        assert service.settings.capture_campaign_id == "startup_round"
    finally:
        service.close()


def test_a_project_bound_service_refuses_and_names_the_fieldctl_route(tmp_path: Path) -> None:
    service = _service(tmp_path, project_id="p25_central_il")
    try:
        with pytest.raises(ValueError, match="fieldctl campaign new"):
            service.select_locate_target({"frequency_mhz": 469.35})
        assert service.settings.capture_campaign_id == "startup_round"
    finally:
        service.close()


def test_a_bad_frequency_is_refused_in_words(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        with pytest.raises(ValueError, match="frequency_mhz"):
            service.select_locate_target({})
        with pytest.raises(ValueError, match="positive"):
            service.select_locate_target({"frequency_mhz": -1})
    finally:
        service.close()


def test_calibrating_the_target_is_remembered_and_resumed_after_a_restart(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        service.select_locate_target({"frequency_mhz": TARGET / 1e6, **_SIM_TUNING})
        service.calibration_device_factory = lambda: SimulatedReceiver(signal_db=-75.0)
        snapshot = _wait(service.start_gain_calibration({}))
        assert snapshot["status"] == "succeeded", snapshot
        result = snapshot["result"]
        # Judged by the target, with no registry row and no explicit list.
        assert result["method"] == "target_snr_plateau"
        assert result["applied"] is True
        recommended = result["recommended"]
        stored = result["locate_target"]["gain"]
        assert stored["source"] == "calibration"
        assert stored["if_gain_reduction_db"] == recommended["if_gain_reduction_db"]
        assert stored["lna_state"] == recommended["lna_state"]
        assert stored["calibration_path"] == result["report_path"]
        assert service.locate_active()["needs_calibration"] is False
    finally:
        service.close()

    # A restart comes up on its startup campaign and gain ...
    restarted = _service(tmp_path, build=False, if_gain_reduction_db=20.0, lna_state=0)
    try:
        assert restarted.settings.capture_campaign_id == "startup_round"
        # ... and choosing the frequency again resumes the calibrated gain.
        restarted.select_locate_target({"frequency_mhz": TARGET / 1e6})
        assert restarted.settings.if_gain_reduction_db == recommended["if_gain_reduction_db"]
        assert restarted.settings.lna_state == recommended["lna_state"]
        assert restarted.settings.center_frequency_hz == CENTER
        assert restarted.settings.sample_rate_hz == RATE
        assert "calibrated" in restarted.gain_origin
    finally:
        restarted.close()


def test_switching_targets_does_not_carry_one_targets_gain_to_another(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        service.select_locate_target({"frequency_mhz": TARGET / 1e6, **_SIM_TUNING})
        connection = connect_locate_database(tmp_path / "db.sqlite3")
        try:
            connection.execute(
                "UPDATE locate_targets SET if_gain_reduction_db = 52, lna_state = 6"
            )
            connection.commit()
        finally:
            connection.close()
        service.select_locate_target({"frequency_mhz": 469.4})
        service.select_locate_target({"frequency_mhz": TARGET / 1e6})
        assert (service.settings.if_gain_reduction_db, service.settings.lna_state) == (52.0, 6)
        # The second target was opened at the gain in force then (40/4), and keeps it.
        service.select_locate_target({"frequency_mhz": 469.4})
        assert (service.settings.if_gain_reduction_db, service.settings.lna_state) == (40.0, 4)
    finally:
        service.close()


def test_a_calibration_for_another_campaign_is_not_stored_on_the_target(tmp_path: Path) -> None:
    """Without a chosen target, calibration behaves exactly as before."""
    service = _service(tmp_path, center_frequency_hz=CENTER, sample_rate_hz=RATE)
    try:
        service.calibration_device_factory = lambda: SimulatedReceiver(signal_db=-75.0)
        snapshot = _wait(service.start_gain_calibration({"target_frequencies_hz": [TARGET]}))
        assert snapshot["status"] == "succeeded", snapshot
        assert snapshot["result"]["locate_target"] is None
    finally:
        service.close()


def test_the_stored_target_reads_back_as_the_service_reported_it(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        active = service.select_locate_target({"frequency_mhz": 469.35})["active"]
        connection = connect_locate_database(tmp_path / "db.sqlite3")
        try:
            stored = describe_target(connection, active["campaign_id"])
        finally:
            connection.close()
        assert stored["tuning"] == active["tuning"]
        assert stored["gain"] == active["gain"]
    finally:
        service.close()


# -- HTTP -----------------------------------------------------------------------


def _call(base: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        base + path,
        method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
    )
    request.add_header("X-Auth-Token", "s3cret")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode() or "{}")


def test_the_locate_endpoints_choose_list_and_refuse(tmp_path: Path, monkeypatch) -> None:
    from dmr_iq_surveyor.web.server import create_server

    monkeypatch.setattr("dmr_iq_surveyor.web.service.default_probe_runner", StubProbeRunner)
    build_database(tmp_path / "db.sqlite3").close()
    settings = FieldSettings(
        database_path=tmp_path / "db.sqlite3",
        output_root=tmp_path / "out",
        recordings_dir=tmp_path / "rec",
        token="s3cret",
        campaign_id="startup_round",
    )
    server = create_server(settings, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _call(base, "/api/locate/targets") == (200, {"active": None, "targets": []})

        status, answer = _call(base, "/api/locate/target", {"frequency_mhz": 469.35})
        assert status == 200, answer
        assert answer["active"]["campaign_id"] == "loc-f469_350000"

        status, listed = _call(base, "/api/locate/targets")
        assert status == 200 and listed["targets"][0]["emitter_key"] == "f469_350000"

        status, refused = _call(base, "/api/locate/target", {"frequency_mhz": 469.353})
        assert status == 400 and "ambiguous" in refused["error"]

        status, refused = _call(
            base, "/api/locate/target?scope=all", {"frequency_mhz": 469.35}
        )
        assert status == 409, refused
    finally:
        server.shutdown()
        server.server_close()


# -- a drive after choosing a target -----------------------------------------------


class _NoiseRig:
    """A 2 MS/s receiver in a moving car: noise only, positions posted as it reads.

    Detection is not what is under test here (`test_web_live.py` pins it);
    where the bins are written, and with what tuning and profile, is.
    """

    def __init__(self, service: FieldService) -> None:
        import numpy as np

        self.service = service
        self.elapsed = 0.0
        self.opened_with = None
        self.closed = False
        self.overflow_count = 0
        self._rng = np.random.default_rng(3)

    def open(self, settings) -> None:
        settings.validate()
        self.opened_with = settings

    def read_stream_chunk(self, max_frames: int):
        import numpy as np

        rate = self.opened_with.sample_rate_hz
        self.elapsed += max_frames / rate
        # 25 m/s eastwards from 32.04 N.
        longitude = 34.78 + self.elapsed * 25.0 / (111_320.0 * 0.847)
        self.service.push_live_position(
            {"latitude": 32.04, "longitude": longitude, "accuracy_m": 5.0}
        )
        noise = self._rng.normal(scale=0.02, size=(max_frames, 2))
        return (noise[:, 0] + 1j * noise[:, 1]).astype(np.complex64)

    def close(self) -> None:
        self.closed = True


def test_a_drive_after_choosing_a_target_writes_into_its_campaign(
    tmp_path: Path, monkeypatch
) -> None:
    import sqlite3

    from fixtures.live_profiles import write_profiles

    from dmr_iq_surveyor.live import session as live_session

    _, site = write_profiles(tmp_path / "profiles", center_hz=469_250_000.0)
    service = _service(
        tmp_path,
        site_profile=str(site),
        live_bin_size_m=150.0,
        live_min_windows_per_bin=2,
        live_max_windows_per_bin=3,
        live_anchor=(32.04, 34.78),
    )
    rig = _NoiseRig(service)
    monkeypatch.setattr(live_session, "SoapyIqDevice", lambda: rig)
    try:
        active = service.select_locate_target({"frequency_mhz": 469.35})["active"]
        service.push_live_position({"latitude": 32.04, "longitude": 34.78, "accuracy_m": 5.0})
        job = service.start_live(
            {"solve_every_bins": 1_000, "max_seconds": 60.0, "fft_size": 4096,
             "frames_per_window": 4}
        )
        deadline = time.monotonic() + 90.0
        while service.live_status()["bin_count"] < 2 and time.monotonic() < deadline:
            if job.is_terminal():
                break
            time.sleep(0.02)
        job.request_cancel()
        _wait(job, 90.0)

        assert rig.opened_with.center_frequency_hz == 469_250_000.0
        assert rig.opened_with.sample_rate_hz == 2_000_000.0
        assert (rig.opened_with.if_gain_reduction_db, rig.opened_with.lna_state) == (40.0, 4)
        written = {entry["survey_run_id"] for entry in service.live_status()["bins"]}
        assert written, "the drive wrote no bins"

        connection = sqlite3.connect(tmp_path / "db.sqlite3")
        try:
            rows = connection.execute(
                f"SELECT campaign_id, band_profile FROM survey_runs WHERE survey_run_id IN "
                f"({', '.join('?' for _ in written)})",
                sorted(written),
            ).fetchall()
        finally:
            connection.close()
        assert {row[0] for row in rows} == {active["campaign_id"]}
        assert {row[1] for row in rows} == {"target_f469_350000"}
    finally:
        service.close()
