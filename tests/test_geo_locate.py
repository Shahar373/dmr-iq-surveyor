"""Phase 8 Locate targets: a frequency, its tuning, profile, campaign and gain.

What is pinned here is what the field app relies on without showing it: the
same frequency always resumes the same campaign at the same gain and tuning, a
campaign's tuning cannot change under measurements already taken, and a target
is never declared beside an emitter close enough to make both ambiguous.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml
from fixtures.geo_scenario import build_database

from dmr_iq_surveyor.geo.emitters import list_emitters, upsert_emitter
from dmr_iq_surveyor.geo.locate import (
    GAIN_CALIBRATED,
    GAIN_IN_FORCE,
    LocateError,
    auto_tuning,
    band_profile_mapping,
    campaign_id_for,
    check_tuning,
    connect_locate_database,
    list_targets,
    prepare_target,
    record_target_gain,
    target_key,
    write_band_profile,
)
from dmr_iq_surveyor.survey.profiles import resolve_band_profile

REPO = Path(__file__).resolve().parent.parent


def _connection(tmp_path: Path) -> sqlite3.Connection:
    build_database(tmp_path / "db.sqlite3").close()
    return connect_locate_database(tmp_path / "db.sqlite3")


def _prepare(connection: sqlite3.Connection, tmp_path: Path, mhz: float = 469.35, **payload):
    gain = payload.pop("gain", (40.0, 4))
    return prepare_target(
        connection,
        frequency_hz=mhz * 1e6,
        output_root=tmp_path / "out",
        gain_in_force=gain,
        payload=payload,
    )


def _add_run(connection: sqlite3.Connection, campaign_id: str, run_id: str = "run1") -> None:
    """One minimal survey run in `campaign_id`, through the real schema."""
    columns = {
        row[1]: (row[2], row[3], row[4])
        for row in connection.execute("PRAGMA table_info(survey_runs)")
    }
    values: dict[str, object] = {}
    for name, (kind, not_null, default) in columns.items():
        if name == "survey_run_id":
            values[name] = run_id
        elif name == "campaign_id":
            values[name] = campaign_id
        elif not_null and default is None:
            values[name] = 0 if "INT" in kind.upper() or "REAL" in kind.upper() else "x"
    if "site_id" in values:
        connection.execute("INSERT OR IGNORE INTO sites (site_id) VALUES (?)", (values["site_id"],))
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO survey_runs ({names}) VALUES ({marks})", list(values.values())
    )
    connection.commit()


# -- derived values -------------------------------------------------------------


def test_a_frequency_has_one_key_one_tuning_and_one_campaign_name() -> None:
    assert target_key(469.35e6) == "f469_350000"
    assert target_key(469_350_000.4) == "f469_350000"
    assert target_key(164.5375e6) == "f164_537500"
    assert auto_tuning(469.35e6) == (469_250_000.0, 2_000_000.0)
    assert campaign_id_for("f469_350000", 1) == "loc-f469_350000"
    assert campaign_id_for("f469_350000", 3) == "loc-f469_350000-3"
    assert campaign_id_for("CC 469!", 1) == "loc-cc-469"


def test_the_automatic_tuning_measures_the_whole_window_off_the_dc_spike() -> None:
    for mhz in (148.0125, 164.5375, 469.35, 867.40625):
        centre, rate = auto_tuning(mhz * 1e6)
        check_tuning(mhz * 1e6, centre, rate)


def test_a_tuning_that_misses_the_window_or_sits_on_dc_is_refused() -> None:
    with pytest.raises(LocateError, match="does not cover"):
        check_tuning(469.35e6, 469.0e6, 1e6)
    with pytest.raises(LocateError, match="DC spike"):
        check_tuning(469.35e6, 469.35e6, 2e6)


def test_the_generated_profile_thresholds_match_the_469_trial_profile(tmp_path: Path) -> None:
    """Same detector terms as every narrowband profile: the target changes
    where the receiver looks, never how a carrier is judged."""
    trial = yaml.safe_load((REPO / "config/bands/uhf_469_narrow.yaml").read_text())
    generated = band_profile_mapping("f469_350000", 469.35e6)
    for key in ("detection", "comparison", "raster_spacings_hz", "segment_seconds",
                "segment_stride_seconds", "max_segments", "usable_passband_rolloff_db"):
        assert generated[key] == trial[key], key
    assert generated["start_frequency_hz"] == 469_100_000
    assert generated["stop_frequency_hz"] == 469_600_000

    path = write_band_profile(tmp_path, "f469_350000", 469.35e6)
    profile = resolve_band_profile(str(path))
    assert profile.name == "target_f469_350000"


def test_an_existing_profile_is_never_rewritten(tmp_path: Path) -> None:
    path = write_band_profile(tmp_path, "f469_350000", 469.35e6)
    text = path.read_text() + "# a run already names this file\n"
    path.write_text(text)
    assert write_band_profile(tmp_path, "f469_350000", 469.35e6) == path
    assert path.read_text() == text


# -- campaigns and gain ---------------------------------------------------------


def test_a_new_target_opens_its_campaign_at_the_gain_in_force(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    target = _prepare(connection, tmp_path, label="control channel")
    assert target["emitter_key"] == "f469_350000"
    assert target["campaign_id"] == "loc-f469_350000"
    assert target["label"] == "control channel"
    assert target["emission"] == "continuous"
    assert target["tuning"] == {
        "center_frequency_hz": 469_250_000.0, "sample_rate_hz": 2_000_000.0, "source": "auto"
    }
    assert target["gain"]["if_gain_reduction_db"] == 40.0
    assert target["gain"]["lna_state"] == 4
    assert target["gain"]["source"] == GAIN_IN_FORCE
    assert target["needs_calibration"] is True
    assert Path(target["band_profile_path"]).is_file()
    assert [emitter["emitter_key"] for emitter in list_emitters(connection)] == ["f469_350000"]


def test_choosing_it_again_resumes_the_same_campaign_tuning_and_gain(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    first = _prepare(connection, tmp_path)
    record_target_gain(connection, first["campaign_id"], if_gain_reduction_db=47.0,
                       lna_state=3, calibration_path="/x/gain.json")
    again = _prepare(connection, tmp_path, gain=(20.0, 0))
    assert again["campaign_id"] == first["campaign_id"]
    assert again["gain"]["if_gain_reduction_db"] == 47.0
    assert again["gain"]["lna_state"] == 3
    assert again["gain"]["source"] == GAIN_CALIBRATED
    assert again["needs_calibration"] is False
    assert again["label"] == first["label"]


def test_starting_over_opens_the_next_campaign_with_its_own_gain(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    first = _prepare(connection, tmp_path, label="cc")
    _add_run(connection, first["campaign_id"])
    second = _prepare(connection, tmp_path, start_over=True, gain=(30.0, 2))
    assert second["campaign_id"] == "loc-f469_350000-2"
    assert second["generation"] == 2
    assert second["gain"]["if_gain_reduction_db"] == 30.0
    assert second["run_count"] == 0 and second["needs_calibration"] is True
    assert second["label"] == "cc", "starting over keeps the target, not just the number"
    assert [row["campaign_id"] for row in list_targets(connection)] == ["loc-f469_350000-2"]


def test_a_campaign_with_runs_keeps_its_tuning(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    target = _prepare(connection, tmp_path)
    moved = _prepare(connection, tmp_path, center_frequency_hz=469.3e6)
    assert moved["tuning"]["center_frequency_hz"] == 469.3e6
    assert moved["tuning"]["source"] == "operator"

    _add_run(connection, target["campaign_id"])
    with pytest.raises(LocateError, match="not be comparable"):
        _prepare(connection, tmp_path, center_frequency_hz=469.25e6)
    # Asking for the tuning it already has is not a change.
    assert _prepare(connection, tmp_path, center_frequency_hz=469.3e6)["run_count"] == 1


def test_a_campaign_with_runs_does_not_offer_calibration(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    target = _prepare(connection, tmp_path)
    _add_run(connection, target["campaign_id"])
    assert _prepare(connection, tmp_path)["needs_calibration"] is False


# -- emitters -------------------------------------------------------------------


def test_an_emitter_already_declared_at_the_frequency_is_reused(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    upsert_emitter(connection, emitter_key="cc469", frequency_hz=469.35e6, label="from the CLI")
    target = _prepare(connection, tmp_path)
    assert target["emitter_key"] == "cc469"
    assert target["campaign_id"] == "loc-cc469"
    assert target["label"] == "from the CLI"
    assert len(list_emitters(connection)) == 1


def test_a_target_next_to_a_declared_emitter_is_refused_not_made_ambiguous(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    upsert_emitter(connection, emitter_key="cc469", frequency_hz=469.35e6)
    with pytest.raises(LocateError, match="cc469"):
        _prepare(connection, tmp_path, mhz=469.355)
    assert len(list_emitters(connection)) == 1
    # A neighbour outside both tolerances is a separate target.
    assert _prepare(connection, tmp_path, mhz=469.375)["emitter_key"] == "f469_375000"


def test_a_known_position_is_stored_kept_and_cleared(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    truth = {"latitude": 32.08, "longitude": 34.78, "source": "published site list"}
    assert _prepare(connection, tmp_path, truth=truth)["has_known_position"] is True
    assert _prepare(connection, tmp_path)["has_known_position"] is True
    assert _prepare(connection, tmp_path, truth={})["has_known_position"] is False
    with pytest.raises(LocateError, match="source"):
        _prepare(connection, tmp_path, truth={"latitude": 32.0, "longitude": 34.0})


def test_an_intermittent_emission_is_carried_to_the_emitter(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    assert _prepare(connection, tmp_path, emission="intermittent")["emission"] == "intermittent"
    with pytest.raises(LocateError):
        _prepare(connection, tmp_path, emission="sometimes")


def test_an_existing_database_upgrades_in_place(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    build_database(database).close()
    before = sqlite3.connect(database)
    tables = {row[0] for row in before.execute("SELECT name FROM sqlite_master")}
    sites = before.execute("SELECT COUNT(*) FROM p25_sites").fetchone()[0]
    before.close()

    connect_locate_database(database).close()
    after = sqlite3.connect(database)
    upgraded = {row[0] for row in after.execute("SELECT name FROM sqlite_master")}
    assert tables <= upgraded and "locate_targets" in upgraded
    assert after.execute("SELECT COUNT(*) FROM p25_sites").fetchone()[0] == sites
    after.close()
