"""Phase 8, Stage 3.2-3.3: where a drive measurement was taken, and how much road it covers.

1. A phone's repeated old fix must age, not look fresh on every POST.
2. GPS accuracy is stored for drive bins and turned into a position
   uncertainty the emitter solver can use.
3. A bin's windows can be spread over its span instead of being the first
   ten that arrived, and a bin that covered under half its span says so.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fixtures.geo_scenario import build_database
from test_live_session import TRANSMITTER, _Drive, _run, _session, _settings
from test_web_live import _service

from dmr_iq_surveyor.geo.emitters import position_sigma_m
from dmr_iq_surveyor.geo.model import GeoMeasurement
from dmr_iq_surveyor.geo.solver import solve_site
from dmr_iq_surveyor.live.session import Position

APP_JS = Path(__file__).resolve().parents[1] / "src/dmr_iq_surveyor/web/static/app.js"


# --- 1. fix age ---------------------------------------------------------------


def test_a_fix_reported_as_old_is_backdated_and_stale(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = service.push_live_position(
        {"latitude": 32.05, "longitude": 34.8, "accuracy_m": 5.0, "fix_age_s": 30.0}
    )
    assert result["accepted"] is True
    assert result["stale"] is True
    assert result["fix_age_s"] == 30.0
    age = service.live_position_age_seconds()
    assert age is not None and age >= 30.0


def test_a_fresh_fix_and_a_phone_that_sends_no_age_behave_as_before(tmp_path: Path) -> None:
    service = _service(tmp_path)
    fresh = service.push_live_position({"latitude": 32.05, "longitude": 34.8, "fix_age_s": 0.4})
    assert fresh["stale"] is False
    silent = service.push_live_position({"latitude": 32.05, "longitude": 34.8})
    assert silent["fix_age_s"] is None and silent["stale"] is False
    age = service.live_position_age_seconds()
    assert age is not None and age < 5.0


@pytest.mark.parametrize(("sent", "expected"), [(-0.2, 0.0), ("soon", None), (True, None),
                                                (float("nan"), None), (1e12, 86_400.0)])
def test_a_malformed_age_never_costs_the_position(tmp_path: Path, sent, expected) -> None:
    service = _service(tmp_path)
    result = service.push_live_position({"latitude": 32.05, "longitude": 34.8, "fix_age_s": sent})
    assert result["accepted"] is True
    assert result["fix_age_s"] == expected


def test_the_page_sends_the_age_measured_on_the_phones_own_clock() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    assert "fix_age_s: fixAgeSeconds(fix)" in source
    assert "Date.now() - fix.timestamp" in source
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; syntax not checked")
    checked = subprocess.run([node, "--check", str(APP_JS)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


# --- 2. accuracy ----------------------------------------------------------------


class _AccurateDrive(_Drive):
    def __init__(self, *, accuracy_m: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self.accuracy_m = accuracy_m

    def position(self) -> Position:
        fix = super().position()
        return Position(
            latitude=fix.latitude, longitude=fix.longitude, accuracy_m=self.accuracy_m, at=fix.at
        )


def _live_runs(database: Path) -> list[dict]:
    connection = build_database(database)
    try:
        return [
            {**dict(row), "settings": json.loads(row["settings_json"] or "{}")}
            for row in connection.execute(
                "SELECT * FROM survey_runs WHERE survey_run_id LIKE 'live_%'"
            )
        ]
    finally:
        connection.close()


def test_drive_bins_store_the_typical_reported_accuracy(tmp_path: Path) -> None:
    database = tmp_path / "accurate.sqlite3"
    connection = build_database(database)
    drive = _AccurateDrive(accuracy_m=8.0, start=(32.0700, 34.7900), east_step_m=40.0)
    _run(drive, connection, windows=12, settings=_settings())
    connection.close()
    runs = _live_runs(database)
    assert runs
    assert all(run["gps_accuracy_m"] == 8.0 for run in runs)


def test_a_source_without_accuracy_leaves_it_unrecorded(tmp_path: Path) -> None:
    database = tmp_path / "plain.sqlite3"
    connection = build_database(database)
    _run(_Drive(start=(32.0700, 34.7900), east_step_m=40.0), connection, windows=12,
         settings=_settings())
    connection.close()
    runs = _live_runs(database)
    assert runs and all(run["gps_accuracy_m"] is None for run in runs)


def test_position_sigma_combines_gps_accuracy_and_bin_spread() -> None:
    assert position_sigma_m(None, None) is None
    assert position_sigma_m(24.5, None) == pytest.approx(10.0)
    assert position_sigma_m(None, 60.0) == pytest.approx(30.0)
    assert position_sigma_m(24.5, 60.0) == pytest.approx((10.0**2 + 30.0**2) ** 0.5)
    assert position_sigma_m("bad", -5.0) is None


def _near_campaign(position_sigma: float) -> list[GeoMeasurement]:
    import math

    points = [(0.0, 0.0, 40.0)]  # right on top of the transmitter
    for index in range(7):
        angle = 2 * math.pi * index / 7
        points.append((3000 * math.cos(angle), 3000 * math.sin(angle), 40.0 - 30 * math.log10(3)))
    measurements = []
    for index, (x, y, level) in enumerate(points):
        measurements.append(
            GeoMeasurement(
                label=f"s{index}",
                latitude=32.0 + y / 111_320.0,
                longitude=34.8 + x / (111_320.0 * math.cos(math.radians(32.0))),
                detected=True,
                level_db=level,
                censor_level_db=0.0,
                survey_run_id=f"s{index}",
                position_sigma_m=position_sigma,
            )
        )
    return measurements


def test_position_uncertainty_is_inert_at_zero_and_widens_near_a_stop() -> None:
    from fixtures.geo_scenario import fast_solve_settings

    exact = solve_site(_near_campaign(0.0), fast_solve_settings())
    assert exact.diagnostics["position_uncertainty"]["modelled"] is False
    uncertain = solve_site(_near_campaign(150.0), fast_solve_settings())
    assert uncertain.diagnostics["position_uncertainty"]["modelled"] is True
    assert uncertain.diagnostics["position_uncertainty"]["median_sigma_m"] == 150.0
    assert exact.surface is not None and uncertain.surface is not None
    # The stop on top of the transmitter can no longer pin it to within a
    # few metres of a point that is itself uncertain by 150 m.
    assert uncertain.surface.probability.max() < exact.surface.probability.max()


# --- 3. spread sampling -----------------------------------------------------------


def test_slow_traffic_files_a_spot_as_a_stretch_and_says_so(tmp_path: Path) -> None:
    database = tmp_path / "crawl.sqlite3"
    connection = build_database(database)
    drive = _Drive(start=(32.0700, 34.7900), east_step_m=4.2)  # 15 km/h
    _run(drive, connection, windows=60, settings=_settings(max_windows_per_bin=10))
    connection.close()
    runs = _live_runs(database)
    assert runs
    assert all(run["settings"]["under_spanned"] is True for run in runs)
    assert all(run["settings"]["spread_windows_over_bin"] is False for run in runs)


def test_spreading_the_windows_covers_the_bin(tmp_path: Path) -> None:
    database = tmp_path / "spread.sqlite3"
    connection = build_database(database)
    drive = _Drive(start=(32.0700, 34.7900), east_step_m=4.2)
    settings = _settings(max_windows_per_bin=10, spread_windows_over_bin=True)
    stats = _run(drive, connection, windows=200, settings=settings)
    connection.close()
    runs = _live_runs(database)
    assert stats.windows_spaced_out > 0
    assert runs
    covered = [run for run in runs if not run["settings"]["under_spanned"]]
    assert covered, "with spread sampling a crawling bin covers its span"
    assert max(run["settings"]["position_spread_m"] for run in runs) > 40.0


def test_a_parked_receiver_still_writes_its_bin_with_spreading_on(tmp_path: Path) -> None:
    database = tmp_path / "parked.sqlite3"
    connection = build_database(database)
    drive = _Drive(start=TRANSMITTER, east_step_m=0.0)
    settings = _settings(min_windows_per_bin=2, max_windows_per_bin=3,
                         spread_windows_over_bin=True)
    session = _session(drive, settings)
    stats = session.run(stop=lambda: drive.now >= 20.0, connection=connection)
    connection.close()
    assert stats.bins_written == 1
    assert stats.windows_spaced_out > 0


def test_emitter_solve_stores_position_sigma_and_uses_it_only_on_request(tmp_path: Path) -> None:
    from fixtures.geo_scenario import Transmitter, fast_solve_settings, seed_run

    from dmr_iq_surveyor.geo.emitters import (
        connect_emitter_database,
        solve_emitters,
        upsert_emitter,
    )

    near = Transmitter(867_762_500.0, 32.050, 34.800, reference_level_db=25.0)
    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    stops = [(32.045, 34.795), (32.056, 34.806), (32.041, 34.809), (32.059, 34.791),
             (32.020, 34.760), (32.085, 34.770), (32.075, 34.855), (31.95, 34.70)]
    for index, (latitude, longitude) in enumerate(stops):
        seed_run(connection, run_id=f"r{index}", latitude=latitude, longitude=longitude,
                 transmitters=[near])
    connection.execute("UPDATE survey_runs SET gps_accuracy_m = 49.0")
    connection.commit()
    connection.close()
    connection = connect_emitter_database(database)
    upsert_emitter(connection, emitter_key="near", frequency_hz=near.frequency_hz)
    connection.close()

    default = solve_emitters(database_path=database, settings=fast_solve_settings())
    assert default["solutions"][0]["diagnostics"]["position_uncertainty"]["modelled"] is False
    connection = connect_emitter_database(database)
    sigmas = {row[0] for row in connection.execute(
        "SELECT position_sigma_m FROM emitter_measurements")}
    connection.close()
    assert len(sigmas) == 1 and sigmas.pop() == pytest.approx(20.0)

    modelled = solve_emitters(
        database_path=database, settings=fast_solve_settings(), model_position_uncertainty=True
    )
    uncertainty = modelled["solutions"][0]["diagnostics"]["position_uncertainty"]
    assert uncertainty["modelled"] is True and uncertainty["median_sigma_m"] == 20.0
