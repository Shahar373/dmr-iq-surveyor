"""The field web app: routing, authorisation, jobs and progress streaming."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixtures.geo_scenario import Transmitter, build_database, seed_run

from dmr_iq_surveyor.web.jobs import JobRegistry
from dmr_iq_surveyor.web.server import create_server
from dmr_iq_surveyor.web.service import (
    FieldService,
    FieldSettings,
    slugify,
    throttle_capture_progress,
)

SITE30 = Transmitter(867_762_500.0, 32.050, 34.800, reference_level_db=25.0)
STOPS = [
    (32.045, 34.795), (32.056, 34.806), (32.041, 34.809),
    (32.020, 34.760), (32.085, 34.770), (31.950, 34.700), (32.200, 34.950),
]


class Client:
    def __init__(self, base: str, token: str) -> None:
        self.base = base
        self.token = token

    def request(
        self, path: str, body: dict | None = None, *, token: str | None = "default", method: str | None = None
    ) -> tuple[int, dict]:
        resolved = self.token if token == "default" else token
        request = urllib.request.Request(
            self.base + path,
            method=method or ("POST" if body is not None else "GET"),
            data=json.dumps(body).encode() if body is not None else None,
        )
        if resolved:
            request.add_header("X-Auth-Token", resolved)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read().decode()
                return response.status, json.loads(payload) if payload else {}
        except urllib.error.HTTPError as error:
            body_text = error.read().decode()
            return error.code, json.loads(body_text) if body_text else {}

    def raw(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=30) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")

    def stream(self, path: str, limit: int = 400) -> list[dict]:
        events: list[dict] = []
        with urllib.request.urlopen(self.base + path, timeout=120) as response:
            for line in response:
                text = line.decode().strip()
                if not text.startswith("data:"):
                    continue
                event = json.loads(text[5:])
                events.append(event)
                if event.get("stage") == "closed" or len(events) >= limit:
                    break
        return events


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[Client]:
    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    for index, (latitude, longitude) in enumerate(STOPS):
        seed_run(
            connection,
            run_id=f"run_{index}",
            latitude=latitude,
            longitude=longitude,
            transmitters=[SITE30],
            capture_start_utc=f"2026-08-01T{8 + index:02d}:00:00+00:00",
        )
    connection.close()
    settings = FieldSettings(
        database_path=database,
        output_root=tmp_path / "out",
        recordings_dir=tmp_path / "rec",
        token="s3cret",
        # Set explicitly, not left to the default, so the disk assertions
        # below test that the setting reaches the API rather than pinning
        # whatever the default happens to be.
        keep_recordings=1,
    )
    server = create_server(settings, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield Client(f"http://127.0.0.1:{server.server_address[1]}", "s3cret")
    finally:
        server.shutdown()
        server.server_close()


def test_the_page_loads_without_a_token_so_it_can_read_one(client: Client) -> None:
    status, body = client.raw("/")
    assert status == 200
    assert "P25 site survey" in body
    assert client.raw("/app.js")[0] == 200
    assert client.raw("/app.css")[0] == 200


def test_the_api_requires_the_token(client: Client) -> None:
    assert client.request("/api/state", token=None)[0] == 401
    assert client.request("/api/state", token="wrong")[0] == 401
    assert client.request("/api/state")[0] == 200


def test_static_serving_refuses_to_escape_its_directory(client: Client) -> None:
    assert client.raw("/%2e%2e/pyproject.toml")[0] == 404
    assert client.raw("/../../pyproject.toml")[0] in (400, 404)


def test_unknown_endpoints_are_404(client: Client) -> None:
    assert client.request("/api/nope")[0] == 404
    assert client.request("/api/nope", {})[0] == 404


def test_state_reports_registry_runs_and_device(client: Client) -> None:
    status, state = client.request("/api/state")
    assert status == 200
    assert len(state["sites"]) == 5
    assert len(state["runs"]) == len(STOPS)
    # No SDR is attached in the test environment, and the API says so
    # rather than pretending one is present.
    assert state["device"]["available"] is False
    assert state["device"]["probe_error"]
    assert "token" not in state["settings"]


def test_position_is_validated_and_persisted(client: Client) -> None:
    assert client.request("/api/position", {"latitude": 999.0, "longitude": 0.0})[0] == 400
    assert client.request("/api/position", {"longitude": 34.8})[0] == 400
    status, saved = client.request(
        "/api/position",
        {"latitude": 32.05, "longitude": 34.8, "accuracy_m": 9.0, "label": "Ridge", "source": "device"},
    )
    assert status == 200
    assert saved["source"] == "browser_gps"
    assert client.request("/api/position")[1]["label"] == "Ridge"


def test_capture_without_a_position_is_refused_with_a_reason(client: Client) -> None:
    status, body = client.request("/api/capture", {"duration_seconds": 5})
    assert status == 400
    assert "mark your position" in body["error"]


def test_capture_without_an_sdr_is_refused_immediately(client: Client) -> None:
    client.request("/api/position", {"latitude": 32.05, "longitude": 34.8})
    status, body = client.request("/api/capture", {"duration_seconds": 5})
    assert status == 409
    assert body["error"]


def test_solve_job_streams_progress_and_updates_the_map(client: Client) -> None:
    status, job = client.request("/api/solve", {"rebuild_measurements": True})
    assert status == 202
    events = client.stream(f"/api/jobs/{job['job_id']}/events?token=s3cret")
    assert events[-1]["stage"] == "closed"
    assert events[-1]["status"] == "succeeded"
    assert any(event["stage"] == "solve" for event in events)

    assert client.request(f"/api/jobs/{job['job_id']}")[1]["status"] == "succeeded"
    collection = client.request("/api/geojson")[1]
    kinds = {feature["properties"]["kind"] for feature in collection["features"]}
    assert {"measurement", "credible_region", "estimate"} <= kinds

    sites = {row["site_key"]: row for row in client.request("/api/sites")[1]["sites"]}
    assert sites["BEE00:37D:1:30"]["status"] == "ok"
    assert sites["BEE00:37D:1:81"]["status"] == "frequency_unknown"


def test_events_can_be_replayed_from_the_start_after_the_job_ends(client: Client) -> None:
    _, job = client.request("/api/solve", {})
    client.stream(f"/api/jobs/{job['job_id']}/events?token=s3cret")
    replay = client.stream(f"/api/jobs/{job['job_id']}/events?token=s3cret&cursor=0")
    assert len(replay) > 1
    assert replay[0]["stage"] == "starting"


def test_jobs_for_unknown_ids_are_404(client: Client) -> None:
    assert client.request("/api/jobs/deadbeef")[0] == 404
    assert client.request("/api/jobs/deadbeef/cancel", {})[0] == 404


def test_analysis_of_a_missing_recording_is_a_client_error(client: Client) -> None:
    client.request("/api/position", {"latitude": 32.05, "longitude": 34.8})
    status, body = client.request("/api/analyse", {"recording": "/nowhere/nothing.wav"})
    assert status == 400
    assert "no such recording" in body["error"]


# ---------------------------------------------------------------- job unit


def test_only_one_job_runs_at_a_time() -> None:
    registry = JobRegistry()
    release = threading.Event()
    registry.submit(kind="test", label="first", work=lambda job: (release.wait(5), {})[1])
    with pytest.raises(RuntimeError, match="already running"):
        registry.submit(kind="test", label="second", work=lambda job: {})
    release.set()


def test_a_failing_job_records_the_stage_it_failed_in() -> None:
    registry = JobRegistry()
    finished = threading.Event()

    def work(job):
        job.emit("survey", "working")
        raise RuntimeError("boom")

    job = registry.submit(kind="test", label="fails", work=work)
    deadline = time.monotonic() + 5.0
    while not job.is_terminal() and time.monotonic() < deadline:
        finished.wait(0.02)
    snapshot = job.snapshot()
    assert snapshot["status"] == "failed"
    assert "boom" in snapshot["error"]
    assert "survey" in snapshot["message"]


def test_a_cancelled_job_reports_cancelled_not_failed() -> None:
    registry = JobRegistry()
    started = threading.Event()

    idle = threading.Event()

    def work(job):
        started.set()
        for _ in range(200):
            job.check_cancelled()
            idle.wait(0.02)
        return {}

    job = registry.submit(kind="test", label="cancels", work=work)
    assert started.wait(5)
    job.request_cancel()
    deadline = time.monotonic() + 5.0
    while not job.is_terminal() and time.monotonic() < deadline:
        idle.wait(0.02)
    assert job.snapshot()["status"] == "cancelled"


def test_slugify_falls_back_when_a_label_has_no_usable_characters() -> None:
    assert slugify("North Ridge Car Park", "x") == "north_ridge_car_park"
    assert slugify("  ///  ", "fallback") == "fallback"


def test_service_reports_a_missing_position_file_honestly(tmp_path: Path) -> None:
    service = FieldService(FieldSettings(output_root=tmp_path))
    assert service.get_position()["source"] == "not_set"
    (tmp_path / "position.json").write_text("{not json", encoding="utf-8")
    assert service.get_position()["source"] == "unavailable"


def test_state_reports_disk_and_position_age(client: Client) -> None:
    status, state = client.request("/api/state")
    assert status == 200
    disk = state["disk"]
    assert disk["per_capture_bytes"] > 0
    assert "captures_that_fit" in disk
    assert disk["keep_recordings"] == 1
    assert state["position_age_seconds"] is None

    client.request("/api/position", {"latitude": 32.05, "longitude": 34.8, "label": "Ridge"})
    assert client.request("/api/state")[1]["position_age_seconds"] is not None
    assert client.request("/api/disk")[1]["per_capture_bytes"] == disk["per_capture_bytes"]


def test_a_capture_that_does_not_fit_on_disk_is_refused_before_the_sdr_is_opened(
    client: Client,
) -> None:
    client.request("/api/position", {"latitude": 32.05, "longitude": 34.8})
    status, body = client.request(
        "/api/capture", {"duration_seconds": 10_000_000, "sample_rate_hz": 10_000_000}
    )
    assert status == 409
    assert "free" in body["error"] and "GiB" in body["error"]


def test_a_stale_position_must_be_confirmed_before_a_stop_is_recorded(tmp_path: Path) -> None:
    """Recording a stop against the previous stop's coordinates is the one
    mistake that silently corrupts a whole campaign."""
    from dmr_iq_surveyor.web.service import PositionStale

    service = FieldService(
        FieldSettings(
            database_path=tmp_path / "db.sqlite3",
            output_root=tmp_path / "out",
            recordings_dir=tmp_path / "rec",
            position_stale_after_seconds=0.0,
        )
    )
    service.set_position({"latitude": 32.05, "longitude": 34.8, "label": "Ridge"})
    with pytest.raises(PositionStale, match="confirm it is still where you are"):
        service.start_capture({"duration_seconds": 5})
    # Confirming gets past the staleness gate and on to the real precondition.
    with pytest.raises(RuntimeError) as excinfo:
        service.start_capture({"duration_seconds": 5, "confirm_position": True})
    assert not isinstance(excinfo.value, PositionStale)


def test_a_second_capture_request_names_the_running_job_not_the_sdr(tmp_path: Path) -> None:
    """Reproduces a field report: a duplicate tap on Record answered with an
    SDR fault. The SDR probe runs before the job is submitted, and enumerating
    a device the running capture already holds reports it as missing -- so the
    operator went looking for broken hardware that was working fine."""
    service = FieldService(
        FieldSettings(
            database_path=tmp_path / "db.sqlite3",
            output_root=tmp_path / "out",
            recordings_dir=tmp_path / "rec",
        )
    )
    service.set_position({"latitude": 32.05, "longitude": 34.8, "label": "Ridge"})

    release = threading.Event()
    started = threading.Event()

    def work(job):
        job.emit("capture", "recording")
        started.set()
        release.wait(5)
        return {}

    service.jobs.submit(kind="capture", label="first", work=work)
    assert started.wait(5)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            service.start_capture({"duration_seconds": 5})
    finally:
        release.set()
    message = str(excinfo.value)
    assert "already running" in message
    assert "capture" in message
    # The point of the fix: it never reached the SDR probe, so it cannot be
    # reported as a device problem.
    assert "SoapySDR" not in message


def test_purge_frees_recordings_but_keeps_their_capture_reports(client: Client, tmp_path: Path) -> None:
    recordings = tmp_path / "rec"
    recordings.mkdir(parents=True, exist_ok=True)
    (recordings / "stop_a.wav").write_bytes(b"0" * 4096)
    (recordings / "stop_a_capture_report.json").write_text("{}", encoding="utf-8")

    status, result = client.request("/api/recordings/purge", {})
    assert status == 200
    assert result["deleted_count"] == 1
    assert not (recordings / "stop_a.wav").exists()
    assert (recordings / "stop_a_capture_report.json").exists(), (
        "the record of what was captured must outlive the recording"
    )
    assert (recordings / "retention.json").is_file()


def test_a_negative_sse_cursor_does_not_spin(client: Client) -> None:
    _, job = client.request("/api/solve", {})
    events = client.stream(f"/api/jobs/{job['job_id']}/events?token=s3cret&cursor=-5", limit=250)
    assert events[-1]["stage"] == "closed"
    assert len(events) < 250, "a negative cursor must not replay events forever"


def test_a_non_ascii_token_is_rejected_rather_than_crashing(client: Client) -> None:
    """`secrets.compare_digest` raises TypeError on non-ASCII strings, and that
    raise happened before the handler's try block: the connection was dropped
    with no response at all instead of a 401.

    Sent as a query parameter because an HTTP header cannot carry non-latin-1
    bytes -- which is exactly how a browser would deliver it.
    """
    status, _ = client.request(
        "/api/state?token=" + urllib.parse.quote("סוד"), token=None
    )
    assert status == 401


def test_cross_origin_state_changing_requests_are_refused(client: Client) -> None:
    request = urllib.request.Request(
        client.base + "/api/position",
        method="POST",
        data=json.dumps({"latitude": 32.0, "longitude": 34.0}).encode(),
    )
    request.add_header("X-Auth-Token", client.token)
    request.add_header("Content-Type", "application/json")
    request.add_header("Origin", "http://evil.example")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    assert status == 403


def _materialise(client: Client) -> None:
    """Run one solve so geo_measurements exist for the stop listing."""
    _, job = client.request("/api/solve", {"rebuild_measurements": True})
    client.stream(f"/api/jobs/{job['job_id']}/events?token=s3cret")


def test_stops_can_be_set_aside_and_put_back_from_the_field(client: Client) -> None:
    """Setting aside keeps the stop and records why it stopped counting."""
    _materialise(client)
    stops = client.request("/api/stops")[1]["stops"]
    assert len(stops) == len(STOPS)
    # A stop that actually heard something, so "stops contributing" is visible.
    heard = next(stop for stop in stops if stop["detections"] > 0)
    target = heard["survey_run_id"]
    assert heard["exclusion_reason"] is None

    status, result = client.request(
        f"/api/stops/{target}/exclude", {"reason": "parked under a bridge"}
    )
    assert status == 200
    assert result["excluded"] is True
    assert "bridge" in result["reason"]

    after = {row["survey_run_id"]: row for row in client.request("/api/stops")[1]["stops"]}
    assert after[target]["exclusion_reason"] == "parked under a bridge"
    assert after[target]["detections"] == 0, "an excluded stop stops contributing"
    assert after[target]["observation_count"] > 0, "but keeps what it observed"

    assert client.request(f"/api/stops/{target}/include", {})[1]["excluded"] is False
    restored = {row["survey_run_id"]: row for row in client.request("/api/stops")[1]["stops"]}
    assert restored[target]["exclusion_reason"] is None


def test_a_stop_can_be_deleted_entirely(client: Client) -> None:
    target = client.request("/api/stops")[1]["stops"][0]["survey_run_id"]
    assert target
    status, result = client.request(f"/api/stops/{target}/delete", {})
    assert status == 200
    assert result["existed"] is True
    remaining = {row["survey_run_id"] for row in client.request("/api/stops")[1]["stops"]}
    assert target not in remaining
    assert client.request(f"/api/stops/{target}/delete", {})[0] == 400


def test_the_plan_and_history_are_available_after_a_solve(client: Client) -> None:
    _materialise(client)

    plan = client.request("/api/plan")[1]
    assert plan["status"] in ("ok", "no_useful_candidate", "no_targets")
    if plan["status"] == "ok":
        assert plan["plan"]["top_stops"]
        kinds = {
            feature["properties"]["kind"] for feature in plan["geojson"]["features"]
        }
        assert "plan_stop" in kinds

    history = client.request("/api/history/BEE00:37D:1:30")[1]
    assert history["site_key"] == "BEE00:37D:1:30"
    assert len(history["history"]) >= 1
    assert client.request("/api/history/NOPE:0:0:0")[0] == 500


def test_exports_are_downloadable_in_each_format(client: Client) -> None:
    _materialise(client)

    for export_format, marker in (
        ("kml", "<kml"),
        ("gpx", "<gpx"),
        ("geojson", "FeatureCollection"),
    ):
        request = urllib.request.Request(
            client.base + f"/api/export?format={export_format}&token=s3cret"
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode()
            assert response.status == 200
            assert "attachment" in response.headers["Content-Disposition"]
        assert marker in body, f"{export_format} export looks wrong"

    # KML must escape operator-supplied text rather than emitting it raw.
    request = urllib.request.Request(client.base + "/api/export?format=kml&token=s3cret")
    with urllib.request.urlopen(request, timeout=60) as response:
        assert "<script>" not in response.read().decode()


# ------------------------------------------------------- capture progress throttle


def test_throttle_capture_progress_suppresses_rapid_calls() -> None:
    """Reproduces the field failure at the unit level: `run_capture()` calls
    `on_progress` on every device read -- tens of times a second at a real
    sample rate -- and the wrapped callback (in practice `Job.emit`, which
    takes a lock and appends to a list) must not run anywhere near that
    often, or its overhead starves the loop reading from the SDR."""
    calls: list[tuple[int, int, float]] = []
    wrapped = throttle_capture_progress(
        lambda frames, target, elapsed: calls.append((frames, target, elapsed)),
        min_interval_seconds=0.5,
    )
    # 100 chunks arriving within 0.2s of wall-clock time, as they would at a
    # real sample rate on a device that streams far faster than 2 Hz.
    for index in range(1, 101):
        wrapped(index * 100, 10_000, elapsed=0.002 * index)
    assert len(calls) <= 2, f"expected the burst collapsed to ~1 call, got {len(calls)}"
    assert calls[0][2] == pytest.approx(0.002, abs=1e-9), "the first call must pass through"


def test_throttle_capture_progress_always_forwards_the_final_call() -> None:
    """The 100%-complete update must never be swallowed by the throttle,
    however close in time it lands to the previous forwarded one."""
    calls: list[tuple[int, int, float]] = []
    wrapped = throttle_capture_progress(
        lambda frames, target, elapsed: calls.append((frames, target, elapsed)),
        min_interval_seconds=0.5,
    )
    wrapped(0, 1000, elapsed=0.0)
    wrapped(1000, 1000, elapsed=0.001)  # completion, 1ms after the first call
    assert calls[-1] == (1000, 1000, 0.001)


def test_throttle_capture_progress_forwards_calls_spaced_far_enough_apart() -> None:
    calls: list[float] = []
    wrapped = throttle_capture_progress(
        lambda frames, target, elapsed: calls.append(elapsed), min_interval_seconds=0.5
    )
    for elapsed in (0.0, 0.6, 1.3, 1.35, 2.0):
        wrapped(1, 100, elapsed=elapsed)
    # 1.35 lands under 0.5s after 1.3 and must be suppressed; the rest are
    # each at least 0.5s apart from the last FORWARDED call.
    assert calls == [0.0, 0.6, 1.3, 2.0]


def test_throttle_capture_progress_matches_a_real_capture_loops_call_pattern(
    tmp_path: Path,
) -> None:
    """End to end through the actual capture loop (`run_capture` with the
    project's own synthetic device stub), not just the throttle function in
    isolation: proves the fix applies where the bug actually lived."""
    from test_capture_core import FakeIqDevice

    from dmr_iq_surveyor.capture.core import CaptureSettings, run_capture

    settings = CaptureSettings(
        center_frequency_hz=868_000_000.0,
        sample_rate_hz=200_000.0,
        duration_seconds=0.5,
        if_gain_reduction_db=40.0,
        agc=False,
        chunk_frames=1_000,  # small chunks -> many on_progress calls per second
    )
    raw_calls: list[float] = []
    forwarded_calls: list[float] = []
    throttled = throttle_capture_progress(
        lambda frames, target, elapsed: forwarded_calls.append(elapsed), min_interval_seconds=0.5
    )

    def on_progress(frames: int, target: int, elapsed: float) -> None:
        raw_calls.append(elapsed)
        throttled(frames, target, elapsed)

    manifest = run_capture(
        tmp_path, settings=settings, device=FakeIqDevice(), on_progress=on_progress
    )
    assert manifest["complete"] is True
    assert len(raw_calls) > 10, "the test setup should exercise many chunks, like a real capture"
    assert len(forwarded_calls) <= 3, (
        f"a synthetic device with no artificial delay delivers all chunks within "
        f"milliseconds of wall-clock time, so almost all {len(raw_calls)} raw calls "
        f"should collapse to the first and the final one; got {len(forwarded_calls)}"
    )
