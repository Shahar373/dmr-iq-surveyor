from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path

import pytest
from test_survey_store import SITE, _observation, _run_record

from dmr_iq_surveyor.reporting.export import collect_frequencies, export_survey
from dmr_iq_surveyor.survey.store import connect_survey_database, import_survey_run, upsert_site


def _populated_database(tmp_path: Path):
    """Three runs at one site, tiling a band, with two frequencies falling in
    the overlap between adjacent captures -- the shape of a real session."""
    connection = connect_survey_database(tmp_path / "db.sqlite3")
    upsert_site(connection, replace(SITE, site_id="park1"))
    for run_id, frequencies, captured_at in (
        ("park1_lo", [867_263_300.0, 867_500_800.0], "2026-08-21T11:38:37+00:00"),
        ("park1_mid", [867_501_100.0, 868_576_100.0], "2026-08-21T11:41:40+00:00"),
        ("park1_hi", [868_575_800.0, 869_513_000.0], "2026-08-21T11:44:34+00:00"),
    ):
        run = replace(
            _run_record(run_id, capture_start_utc=captured_at, capture_time_source="auxi"),
            site_id="park1",
        )
        import_survey_run(
            connection,
            run=run,
            observations=[_observation(hz) for hz in frequencies],
            raster_tolerance_hz=6250.0,
        )
    return connection


def _read_csv(path: Path) -> list[dict[str, str]]:
    # utf-8-sig, matching what the exporter writes for Excel.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_export_writes_all_three_tables(tmp_path: Path) -> None:
    connection = _populated_database(tmp_path)
    result = export_survey(connection, tmp_path / "out")
    connection.close()

    assert result["run_count"] == 3
    assert result["observation_count"] == 6
    for name in ("runs", "observations", "frequencies"):
        assert (tmp_path / "out" / f"{name}.csv").is_file()


def test_frequencies_table_aggregates_a_signal_seen_in_several_runs(tmp_path: Path) -> None:
    """The point of the frequencies table: a signal detected in two
    overlapping captures must appear once, naming both runs, with the
    spread between their measured centres -- that spread is the evidence
    the two detections really are the same emitter."""
    connection = _populated_database(tmp_path)
    frequencies = collect_frequencies(connection)
    connection.close()

    multi_run = [row for row in frequencies if row["run_count"] > 1]
    assert len(multi_run) == 2
    for row in multi_run:
        assert row["measured_center_spread_hz"] == pytest.approx(300.0, abs=1.0)
        assert len(row["runs_seen_in"].split(",")) == 2
    # Most-corroborated frequencies come first.
    assert frequencies[0]["run_count"] >= frequencies[-1]["run_count"]


def test_export_can_be_limited_to_one_site(tmp_path: Path) -> None:
    connection = _populated_database(tmp_path)
    other = replace(
        _run_record("other_run", capture_start_utc="2026-08-22T09:00:00+00:00", capture_time_source="auxi"),
        site_id="home",
    )
    upsert_site(connection, SITE)
    import_survey_run(
        connection, run=other, observations=[_observation(866_100_000.0)], raster_tolerance_hz=6250.0
    )

    scoped = export_survey(connection, tmp_path / "scoped", site_id="park1")
    everything = export_survey(connection, tmp_path / "all")
    connection.close()

    assert scoped["run_count"] == 3
    assert everything["run_count"] == 4
    assert all(row["site_id"] == "park1" for row in _read_csv(tmp_path / "scoped" / "runs.csv"))


def test_observations_csv_carries_run_context_so_it_stands_alone(tmp_path: Path) -> None:
    connection = _populated_database(tmp_path)
    export_survey(connection, tmp_path / "out")
    connection.close()

    rows = _read_csv(tmp_path / "out" / "observations.csv")
    assert rows
    for row in rows:
        # No join needed to know when and where a detection came from.
        assert row["site_id"] == "park1"
        assert row["capture_start_utc"]
        assert row["survey_run_id"].startswith("park1_")
        assert float(row["measured_center_mhz"]) == pytest.approx(
            float(row["measured_center_hz"]) / 1e6
        )


def test_xlsx_export_reports_a_clear_error_when_openpyxl_is_missing(tmp_path: Path) -> None:
    connection = _populated_database(tmp_path)
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="openpyxl"):
            export_survey(connection, tmp_path / "out", write_xlsx=True)
    else:
        result = export_survey(connection, tmp_path / "out", write_xlsx=True)
        assert result["xlsx_path"] is not None
        assert Path(result["xlsx_path"]).is_file()
    finally:
        connection.close()
