"""Phase 0.5 stabilization: the geolocation output states its own maturity.

The estimator (geo/solver.py) is generic and knows nothing about P25 or
868 MHz; these tests pin only that GEOLOCATION_MATURITY and VALIDATION_STATUS
are surfaced, additively, on every solve report, markdown report, CLI
output and the field app's /api/state -- never that a particular band is
hard-coded into the solver itself.
"""

from __future__ import annotations

from pathlib import Path

from fixtures.geo_scenario import Transmitter, build_database, seed_run
from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app
from dmr_iq_surveyor.geo.pipeline import VALIDATION_NOTE, solve_all_sites
from dmr_iq_surveyor.geo.report import render_solution_markdown
from dmr_iq_surveyor.geo.solver import GEOLOCATION_MATURITY, VALIDATION_STATUS
from dmr_iq_surveyor.web.service import FieldService, FieldSettings

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


def test_solver_maturity_constants_are_generic_not_band_specific() -> None:
    # Nothing in the solver's own vocabulary names a band or protocol.
    assert GEOLOCATION_MATURITY == "experimental"
    assert VALIDATION_STATUS == "unvalidated"
    assert "868" not in GEOLOCATION_MATURITY
    assert "868" not in VALIDATION_STATUS
    assert "P25" not in GEOLOCATION_MATURITY
    assert "P25" not in VALIDATION_STATUS


def test_solve_report_states_maturity_additively(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    report = solve_all_sites(database_path=database, output_root=tmp_path / "out")
    assert report["geolocation_maturity"] == GEOLOCATION_MATURITY
    assert report["validation_status"] == VALIDATION_STATUS
    assert report["validation_note"] == VALIDATION_NOTE
    assert "868" in VALIDATION_NOTE  # the domain context lives in the note, not the constant

    markdown = (tmp_path / "out" / "reports" / f"geolocation_{report['solve_batch_id']}.md").read_text(
        encoding="utf-8"
    )
    assert "Geolocation maturity: experimental" in markdown
    assert VALIDATION_NOTE in markdown


def test_markdown_report_omits_the_maturity_line_when_not_given() -> None:
    # Additive: an existing caller that does not pass the new keywords sees
    # output unchanged, not a line reading "None".
    markdown = render_solution_markdown(
        solve_batch_id="b1",
        solutions=[],
        measurement_summary={},
        settings={},
    )
    assert "Geolocation maturity" not in markdown


def test_geo_solve_cli_prints_the_maturity_line(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    result = runner.invoke(
        app,
        ["geo", "solve", "--database", str(database), "--output", str(tmp_path / "out")],
    )
    assert result.exit_code == 0
    # The console line wraps to the terminal width in CliRunner's default
    # (narrow) capture, so only the short, unwrappable prefix is checked
    # against the console; the full note is checked against the report.
    assert "Geolocation maturity: experimental" in result.output
    (report,) = (tmp_path / "out" / "reports").glob("geolocation_*.md")
    assert VALIDATION_NOTE in report.read_text(encoding="utf-8")


def test_geo_sites_cli_prints_the_maturity_line(tmp_path: Path) -> None:
    database = _seed(tmp_path)
    result = runner.invoke(app, ["geo", "sites", "--database", str(database)])
    assert result.exit_code == 0
    assert "Geolocation maturity: experimental" in result.output


def test_field_app_state_reports_geolocation_maturity(tmp_path: Path) -> None:
    # database_path and recordings_dir default to repo-relative paths
    # independently of output_root -- all three must be pointed at tmp_path,
    # or FieldService.state() touches the real runs/ directory of whichever
    # checkout the tests happen to run from.
    settings = FieldSettings(
        output_root=tmp_path,
        database_path=tmp_path / "inventory.sqlite3",
        recordings_dir=tmp_path / "recordings",
    )
    service = FieldService(settings)
    state = service.state()
    assert state["geolocation"]["maturity"] == GEOLOCATION_MATURITY
    assert state["geolocation"]["validation_note"] == VALIDATION_NOTE
