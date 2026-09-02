"""The `geo` sub-app, and that mounting it left every earlier command alone."""

from __future__ import annotations

import json
from pathlib import Path

from fixtures.geo_scenario import SITE_CSV, Transmitter, build_database, seed_run
from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app

runner = CliRunner()

SITE30 = Transmitter(867_762_500.0, 32.050, 34.800, reference_level_db=25.0)
STOPS = [(32.045, 34.795), (32.056, 34.806), (32.041, 34.809), (32.020, 34.760), (31.95, 34.70)]


def _seed(tmp_path: Path) -> Path:
    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    for index, (latitude, longitude) in enumerate(STOPS):
        seed_run(
            connection,
            run_id=f"run_{index}",
            latitude=latitude,
            longitude=longitude,
            transmitters=[SITE30],
            capture_start_utc=f"2026-08-01T{9 + index:02d}:00:00+00:00",
        )
    connection.close()
    return database


def test_previous_commands_are_still_registered() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "inspect", "spectrum", "detect", "extract-channel", "decode-channel",
        "inventory-build", "targeted-decode", "inventory-import-log", "survey", "geo", "web",
    ):
        assert command in result.output


def test_import_sites_reports_the_ambiguity_it_found(tmp_path: Path) -> None:
    csv_path = tmp_path / "sites.csv"
    csv_path.write_text(SITE_CSV, encoding="utf-8")
    result = runner.invoke(
        app,
        ["geo", "import-sites", str(csv_path), "--database", str(tmp_path / "db.sqlite3")],
    )
    assert result.exit_code == 0
    assert "cannot be attributed to one site" in result.output
    assert "no control-channel frequency" in result.output


def test_import_sites_fails_cleanly_on_a_bad_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("nothing,useful\n1,2\n", encoding="utf-8")
    result = runner.invoke(
        app, ["geo", "import-sites", str(bad), "--database", str(tmp_path / "db.sqlite3")]
    )
    assert result.exit_code == 1
    assert "Reference import failed" in result.output


def test_measurements_then_solve_then_export(tmp_path: Path) -> None:
    database = _seed(tmp_path)

    measurements = runner.invoke(app, ["geo", "measurements", "--database", str(database)])
    assert measurements.exit_code == 0
    assert "Non-detections" in measurements.output

    solve = runner.invoke(
        app,
        [
            "geo", "solve",
            "--database", str(database),
            "--output", str(tmp_path / "out"),
            "--batch-id", "b1",
            "--resolution-m", "250",
            "--margin-m", "12000",
        ],
    )
    assert solve.exit_code == 0
    # The rendered table wraps to the terminal width, so the site key is
    # asserted against the report rather than the wrapped console output.
    assert "not a transmitter coordinate" in solve.output
    report = (tmp_path / "out" / "reports" / "geolocation_b1.md").read_text(encoding="utf-8")
    assert "BEE00:37D:1:30" in report

    sites = runner.invoke(app, ["geo", "sites", "--database", str(database)])
    assert sites.exit_code == 0
    assert "not attributable" in sites.output

    history = runner.invoke(
        app, ["geo", "history", "BEE00:37D:1:30", "--database", str(database)]
    )
    assert history.exit_code == 0
    assert "b1" in history.output

    export = runner.invoke(
        app, ["geo", "export", str(tmp_path / "map.geojson"), "--database", str(database)]
    )
    assert export.exit_code == 0
    payload = json.loads((tmp_path / "map.geojson").read_text(encoding="utf-8"))
    assert payload["type"] == "FeatureCollection"
    assert payload["features"]


def test_history_of_an_unknown_site_fails_clearly(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    result = runner.invoke(app, ["geo", "history", "NOPE:0:0:0", "--database", str(database)])
    assert result.exit_code == 1
    assert "Unknown site key" in result.output


def test_measurements_rejects_an_unknown_level_metric(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    result = runner.invoke(
        app, ["geo", "measurements", "--database", str(database), "--level-metric", "dbm"]
    )
    assert result.exit_code == 1
    assert "level_metric" in result.output
