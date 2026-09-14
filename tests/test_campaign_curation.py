"""Assigning a historical run to a campaign, and what that costs.

The command exists because a database that has been in the field for months
holds runs nobody ever declared a campaign for, and the only honest way to
file them is for a person to say which ones and why. So the tests that matter
here are not the happy path -- they are the refusals, the dry run that must
leave the file untouched, and the promise that a failure part-way through
cannot leave a run in a campaign whose conclusions were drawn without it.

The recompute is the other half. Moving one run changes the reference gain and
the noise-floor median of every run in the target campaign and of every run
left unassigned, so both populations are rebuilt inside the same transaction
as the membership change. The solve cannot join that transaction -- it is
minutes of grid search -- so it is marked superseded instead, and a solve is
what lifts the mark.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app
from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.project.binding import clear_binding
from dmr_iq_surveyor.project.claim import write_claim
from dmr_iq_surveyor.survey.curation import superseded_analysis

runner = CliRunner()
ANALYZER = "p25_site_geolocation"

PROJECT_YAML = """
schema_version: 1
project_id: p25_central_il
label: "P25 central Israel"
analyzer: p25_site_geolocation
database: db.sqlite3
"""

CAMPAIGN_YAML = """
schema_version: 1
campaign_id: day1
project_id: p25_central_il
label: "Day 1"
"""


@pytest.fixture(autouse=True)
def _unbound() -> None:
    clear_binding()
    yield
    clear_binding()


def _project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "project.yaml"
    manifest.write_text(PROJECT_YAML, encoding="utf-8")
    return manifest


def _campaign(root: Path, *, campaign_id: str = "day1", status: str | None = None) -> Path:
    directory = root / "campaigns"
    directory.mkdir(parents=True, exist_ok=True)
    body = CAMPAIGN_YAML.replace("day1", campaign_id)
    if status is not None:
        body += f"status: {status}\n"
    path = directory / f"{campaign_id}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _add_run(
    connection: Any,
    run_id: str,
    *,
    capture_start_utc: str | None = "2026-08-01T10:00:00+00:00",
    campaign_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO survey_runs(survey_run_id, band_profile, source_path, source_basename,"
        " center_frequency_hz, sample_rate_hz, capture_start_utc, capture_time_source,"
        " requested_start_hz, requested_stop_hz, coverage_status, duration_seconds,"
        " analyzed_seconds, segment_count, occupancy_threshold_db, detection_settings_json,"
        " tool_version, imported_at, status, campaign_id, gps_source)"
        " VALUES (?, 'central_800', ?, ?, 868e6, 5e6, ?, ?, 866e6, 870e6, 'complete',"
        " 90.0, 30.0, 30, 8.0, '{}', '0.10.0', '2026-08-02T00:00:00+00:00', 'ok', ?, 'user')",
        (
            run_id,
            f"/tmp/{run_id}.wav",
            f"{run_id}.wav",
            capture_start_utc,
            "auxi" if capture_start_utc else "unknown",
            campaign_id,
        ),
    )


def _database(tmp_path: Path, runs: list[tuple[str, str | None, str | None]]) -> Path:
    """A claimed project database holding the given (id, capture time, campaign)."""
    path = tmp_path / "db.sqlite3"
    connection = connect_geo_database(path)
    try:
        for run_id, capture, campaign in runs:
            _add_run(connection, run_id, capture_start_utc=capture, campaign_id=campaign)
        connection.commit()
        write_claim(connection, project_id="p25_central_il", analyzer=ANALYZER)
    finally:
        connection.close()
    clear_binding()
    return path


def _workspace(
    tmp_path: Path,
    *,
    runs: list[tuple[str, str | None, str | None]] | None = None,
    status: str | None = None,
) -> tuple[Path, Path]:
    manifest = _project(tmp_path / "proj")
    _campaign(tmp_path / "proj", status=status)
    database = _database(
        tmp_path / "proj", runs if runs is not None else [("legacy1", "2026-08-01T10:00:00+00:00", None)]
    )
    return manifest, database


def _assign(manifest: Path, *args: str) -> Any:
    return runner.invoke(
        app,
        ["project", "campaign", "assign-runs", "--project", str(manifest),
         "--campaign-id", "day1", *args],
    )


def _campaign_of(database: Path, run_id: str) -> str | None:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT campaign_id FROM survey_runs WHERE survey_run_id = ?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else row[0]


def _counts(database: Path) -> tuple[int, int]:
    """(runs in day1, runs still unassigned)."""
    connection = sqlite3.connect(database)
    try:
        target = connection.execute(
            "SELECT COUNT(*) FROM survey_runs WHERE campaign_id = 'day1'"
        ).fetchone()[0]
        legacy = connection.execute(
            "SELECT COUNT(*) FROM survey_runs WHERE campaign_id IS NULL"
        ).fetchone()[0]
    finally:
        connection.close()
    return int(target), int(legacy)


def _audit(database: Path) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(
            "SELECT survey_run_id, previous_campaign_id, campaign_id, reason, tool_version"
            " FROM campaign_assignments ORDER BY campaign_assignment_id"
        ).fetchall()
    finally:
        connection.close()


def _fingerprint(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), path.stat().st_size


# -- selecting -----------------------------------------------------------------


def test_runs_are_selected_by_id(tmp_path: Path) -> None:
    manifest, database = _workspace(
        tmp_path,
        runs=[("a", "2026-08-01T10:00:00+00:00", None), ("b", "2026-08-01T11:00:00+00:00", None)],
    )

    result = _assign(manifest, "--run-id", "a", "--write", "--reason", "day one, coastal")

    assert result.exit_code == 0, result.output
    assert _campaign_of(database, "a") == "day1"
    assert _campaign_of(database, "b") is None


def test_runs_are_selected_by_a_time_range(tmp_path: Path) -> None:
    manifest, database = _workspace(
        tmp_path,
        runs=[
            ("before", "2026-07-31T23:59:59+00:00", None),
            ("inside", "2026-08-01T10:00:00+00:00", None),
            ("after", "2026-08-02T00:00:01+00:00", None),
        ],
    )

    result = _assign(
        manifest,
        "--since", "2026-08-01T00:00:00+00:00",
        "--until", "2026-08-02T00:00:00+00:00",
        "--write", "--reason", "the first day",
    )

    assert result.exit_code == 0, result.output
    assert _campaign_of(database, "inside") == "day1"
    assert _campaign_of(database, "before") is None
    assert _campaign_of(database, "after") is None


def test_the_range_is_half_open_at_both_ends(tmp_path: Path) -> None:
    """[since, until). Two adjacent ranges partition a day without overlapping."""
    manifest, database = _workspace(
        tmp_path,
        runs=[
            ("on_start", "2026-08-01T00:00:00+00:00", None),
            ("on_end", "2026-08-02T00:00:00+00:00", None),
        ],
    )

    _assign(
        manifest,
        "--since", "2026-08-01T00:00:00+00:00",
        "--until", "2026-08-02T00:00:00+00:00",
        "--write", "--reason", "boundaries",
    )

    assert _campaign_of(database, "on_start") == "day1", "the start bound is inclusive"
    assert _campaign_of(database, "on_end") is None, "the end bound is exclusive"


def test_a_run_without_a_capture_time_is_never_swept_in_by_a_range(tmp_path: Path) -> None:
    """An absent time is not a late one. A range cannot say where it falls."""
    manifest, database = _workspace(
        tmp_path,
        runs=[("dated", "2026-08-01T10:00:00+00:00", None), ("undated", None, None)],
    )

    result = _assign(
        manifest,
        "--since", "2026-08-01T00:00:00+00:00",
        "--until", "2026-08-02T00:00:00+00:00",
        "--write", "--reason", "the first day",
    )

    assert _campaign_of(database, "undated") is None
    assert _campaign_of(database, "dated") == "day1"
    assert "no capture time" in result.output.replace("\n", "")


def test_the_two_selection_methods_are_mutually_exclusive(tmp_path: Path) -> None:
    manifest, database = _workspace(tmp_path)
    before = _fingerprint(database)

    result = _assign(
        manifest, "--run-id", "legacy1",
        "--since", "2026-08-01T00:00:00+00:00", "--until", "2026-08-02T00:00:00+00:00",
        "--write", "--reason", "both",
    )

    assert result.exit_code == 1
    assert _fingerprint(database) == before


def test_a_range_needs_both_ends(tmp_path: Path) -> None:
    manifest, _ = _workspace(tmp_path)

    result = _assign(manifest, "--since", "2026-08-01T00:00:00+00:00", "--write", "--reason", "x")

    assert result.exit_code == 1
    assert "both ends" in result.output.replace("\n", "")


def test_selecting_nothing_at_all_is_refused(tmp_path: Path) -> None:
    """There is deliberately no --all."""
    manifest, _ = _workspace(tmp_path)

    result = _assign(manifest, "--write", "--reason", "everything")

    assert result.exit_code == 1
    assert "no --all" in result.output.replace("\n", "")


def test_a_bound_without_an_offset_is_refused(tmp_path: Path) -> None:
    """A naive bound is as likely local as UTC, and guessing shifts the window."""
    manifest, database = _workspace(tmp_path)
    before = _fingerprint(database)

    result = _assign(
        manifest, "--since", "2026-08-01T00:00:00", "--until", "2026-08-02T00:00:00+00:00",
        "--write", "--reason", "naive",
    )

    assert result.exit_code == 1
    assert "no UTC offset" in result.output.replace("\n", "")
    assert _fingerprint(database) == before


def test_an_inverted_range_is_refused(tmp_path: Path) -> None:
    manifest, _ = _workspace(tmp_path)

    result = _assign(
        manifest, "--since", "2026-08-02T00:00:00+00:00", "--until", "2026-08-01T00:00:00+00:00",
        "--write", "--reason", "backwards",
    )

    assert result.exit_code == 1


# -- the dry run ---------------------------------------------------------------


def test_the_dry_run_changes_not_one_byte(tmp_path: Path) -> None:
    """The whole point. Looking at what would move must not move anything."""
    manifest, database = _workspace(tmp_path)
    before_bytes = database.read_bytes()
    before_mtime = database.stat().st_mtime_ns

    result = _assign(manifest, "--run-id", "legacy1")

    assert result.exit_code == 0, result.output
    assert "Nothing was written" in result.output
    assert database.read_bytes() == before_bytes
    assert database.stat().st_mtime_ns == before_mtime
    assert _campaign_of(database, "legacy1") is None


def test_the_dry_run_names_the_runs_and_what_would_be_recomputed(tmp_path: Path) -> None:
    manifest, _ = _workspace(tmp_path)

    result = _assign(manifest, "--run-id", "legacy1")

    flat = result.output.replace("\n", "")
    assert "legacy1" in flat
    assert "p25_central_il" in flat
    assert "day1" in flat
    for expected in ("measurements", "stored solutions", "stored plans"):
        assert expected in flat, expected
    assert "untouched" in flat, "historical whole-database analysis is named as untouched"


def test_writing_without_a_reason_is_refused(tmp_path: Path) -> None:
    manifest, database = _workspace(tmp_path)
    before = _fingerprint(database)

    result = _assign(manifest, "--run-id", "legacy1", "--write")

    assert result.exit_code == 1
    assert "--reason is required" in result.output.replace("\n", "")
    assert _fingerprint(database) == before


# -- the refusals that come before any write -----------------------------------


def test_an_unknown_run_id_is_an_error(tmp_path: Path) -> None:
    manifest, database = _workspace(tmp_path)
    before = _fingerprint(database)

    result = _assign(manifest, "--run-id", "nope", "--write", "--reason", "typo")

    assert result.exit_code == 1
    assert "not in this database" in result.output.replace("\n", "")
    assert _fingerprint(database) == before


def test_an_unknown_run_id_refuses_the_whole_batch(tmp_path: Path) -> None:
    """One bad id does not let the good ones through: the selection was wrong."""
    manifest, database = _workspace(
        tmp_path, runs=[("good", "2026-08-01T10:00:00+00:00", None)]
    )

    result = _assign(
        manifest, "--run-id", "good", "--run-id", "nope", "--write", "--reason", "mixed"
    )

    assert result.exit_code == 1
    assert _campaign_of(database, "good") is None


def test_a_run_in_another_campaign_is_never_moved(tmp_path: Path) -> None:
    """There is no reassignment in this command, only assignment."""
    manifest, database = _workspace(
        tmp_path, runs=[("taken", "2026-08-01T10:00:00+00:00", "otherround")]
    )

    result = _assign(manifest, "--run-id", "taken", "--write", "--reason", "steal it")

    assert result.exit_code == 1
    assert "already belong to another campaign" in result.output.replace("\n", "")
    assert _campaign_of(database, "taken") == "otherround"


def test_a_missing_target_campaign_is_refused(tmp_path: Path) -> None:
    manifest = _project(tmp_path / "proj")
    database = _database(tmp_path / "proj", [("legacy1", "2026-08-01T10:00:00+00:00", None)])
    before = _fingerprint(database)

    result = _assign(manifest, "--run-id", "legacy1", "--write", "--reason", "no target")

    assert result.exit_code == 1
    assert _fingerprint(database) == before


def test_a_closed_campaign_refuses_new_membership(tmp_path: Path) -> None:
    """Closing says the evidence is final; adding stops afterwards contradicts it."""
    manifest, database = _workspace(tmp_path, status="closed")
    before = _fingerprint(database)

    result = _assign(manifest, "--run-id", "legacy1", "--write", "--reason", "too late")

    assert result.exit_code == 1
    assert "closed" in result.output.replace("\n", "")
    assert _fingerprint(database) == before
    assert _campaign_of(database, "legacy1") is None


def test_an_unclaimed_database_is_refused(tmp_path: Path) -> None:
    """project_meta is checked before the membership is read, let alone written."""
    manifest = _project(tmp_path / "proj")
    _campaign(tmp_path / "proj")
    path = tmp_path / "proj" / "db.sqlite3"
    connection = connect_geo_database(path)
    try:
        _add_run(connection, "legacy1")
        connection.commit()
    finally:
        connection.close()
    clear_binding()
    before = _fingerprint(path)

    result = _assign(manifest, "--run-id", "legacy1", "--write", "--reason", "unclaimed")

    assert result.exit_code == 1
    assert _fingerprint(path) == before


def test_a_database_claimed_by_another_project_is_refused(tmp_path: Path) -> None:
    manifest = _project(tmp_path / "proj")
    _campaign(tmp_path / "proj")
    path = tmp_path / "proj" / "db.sqlite3"
    connection = connect_geo_database(path)
    try:
        _add_run(connection, "legacy1")
        connection.commit()
        write_claim(connection, project_id="somebody_else", analyzer=ANALYZER)
    finally:
        connection.close()
    clear_binding()

    result = _assign(manifest, "--run-id", "legacy1", "--write", "--reason", "wrong project")

    assert result.exit_code == 1
    assert _campaign_of(path, "legacy1") is None


# -- counts, no-ops and the other campaigns ------------------------------------


def test_the_legacy_count_falls_and_the_target_rises_by_exactly_the_selection(
    tmp_path: Path,
) -> None:
    manifest, database = _workspace(
        tmp_path,
        runs=[
            ("a", "2026-08-01T10:00:00+00:00", None),
            ("b", "2026-08-01T11:00:00+00:00", None),
            ("c", "2026-08-01T12:00:00+00:00", None),
        ],
    )
    assert _counts(database) == (0, 3)

    _assign(manifest, "--run-id", "a", "--run-id", "b", "--write", "--reason", "two of three")

    assert _counts(database) == (2, 1)


def test_no_other_campaign_is_changed(tmp_path: Path) -> None:
    manifest, database = _workspace(
        tmp_path,
        runs=[
            ("mine", "2026-08-01T10:00:00+00:00", None),
            ("theirs", "2026-08-01T11:00:00+00:00", "otherround"),
        ],
    )

    _assign(manifest, "--run-id", "mine", "--write", "--reason", "only mine")

    assert _campaign_of(database, "theirs") == "otherround"
    connection = sqlite3.connect(database)
    try:
        other = connection.execute(
            "SELECT COUNT(*) FROM survey_runs WHERE campaign_id = 'otherround'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert other == 1


def test_a_run_already_in_the_target_is_a_reported_no_op(tmp_path: Path) -> None:
    manifest, database = _workspace(
        tmp_path, runs=[("already", "2026-08-01T10:00:00+00:00", "day1")]
    )

    result = _assign(manifest, "--run-id", "already", "--write", "--reason", "again")

    assert result.exit_code == 0, result.output
    assert "already in day1" in result.output.replace("\n", "")
    assert _campaign_of(database, "already") == "day1"


# -- the audit trail -----------------------------------------------------------


def test_every_real_move_is_audited_exactly_once(tmp_path: Path) -> None:
    manifest, database = _workspace(
        tmp_path,
        runs=[("a", "2026-08-01T10:00:00+00:00", None), ("b", "2026-08-01T11:00:00+00:00", None)],
    )

    _assign(manifest, "--run-id", "a", "--run-id", "b", "--write", "--reason", "day one")

    rows = _audit(database)
    assert len(rows) == 2
    assert {row[0] for row in rows} == {"a", "b"}
    for run_id, previous, target, reason, version in rows:
        assert previous is None, f"{run_id} came from no campaign"
        assert target == "day1"
        assert reason == "day one"
        assert version, "the tool version is recorded"


def test_a_no_op_writes_no_audit_row(tmp_path: Path) -> None:
    """An audit entry for a move that did not happen is a false record."""
    manifest, database = _workspace(
        tmp_path, runs=[("already", "2026-08-01T10:00:00+00:00", "day1")]
    )

    _assign(manifest, "--run-id", "already", "--write", "--reason", "again")

    assert _audit(database) == []


def test_re_running_the_same_assignment_does_not_double_the_audit(tmp_path: Path) -> None:
    manifest, database = _workspace(tmp_path)

    _assign(manifest, "--run-id", "legacy1", "--write", "--reason", "first")
    _assign(manifest, "--run-id", "legacy1", "--write", "--reason", "second")

    rows = _audit(database)
    assert len(rows) == 1, "the second run was a no-op and recorded nothing"
    assert rows[0][3] == "first"


def test_the_audit_records_the_utc_timestamp(tmp_path: Path) -> None:
    manifest, database = _workspace(tmp_path)

    _assign(manifest, "--run-id", "legacy1", "--write", "--reason", "when")

    connection = sqlite3.connect(database)
    try:
        stamp = connection.execute(
            "SELECT assigned_at FROM campaign_assignments"
        ).fetchone()[0]
    finally:
        connection.close()
    assert stamp.endswith("+00:00"), stamp


# -- the derived analysis ------------------------------------------------------


def test_the_target_analysis_is_marked_superseded(tmp_path: Path) -> None:
    """Membership changed, so the stored solve is no longer this round's answer."""
    manifest, database = _workspace(tmp_path)

    _assign(manifest, "--run-id", "legacy1", "--write", "--reason", "supersede")

    connection = connect_geo_database(database)
    try:
        mark = superseded_analysis(connection, "day1")
    finally:
        connection.close()
    clear_binding()
    assert mark is not None
    assert "assigned" in mark.reason


def test_a_solve_lifts_the_mark(tmp_path: Path) -> None:
    from dmr_iq_surveyor.geo.pipeline import solve_all_sites
    from dmr_iq_surveyor.survey.scope import CampaignScope

    manifest, database = _workspace(tmp_path)
    _assign(manifest, "--run-id", "legacy1", "--write", "--reason", "supersede")

    solve_all_sites(database_path=database, scope=CampaignScope("day1"))

    connection = connect_geo_database(database)
    try:
        assert superseded_analysis(connection, "day1") is None
    finally:
        connection.close()
    clear_binding()


def test_historical_whole_database_solutions_are_preserved(tmp_path: Path) -> None:
    """`campaign_id IS NULL` on a solution means it read the whole file. Untouched."""
    manifest, database = _workspace(tmp_path)
    connection = connect_geo_database(database)
    try:
        connection.execute(
            "INSERT INTO p25_systems(p25_system_id, wacn_hex, system_id_hex, label)"
            " VALUES (1, 'BEE00', '37D', 'test')"
        )
        connection.execute(
            "INSERT INTO p25_sites(p25_site_id, p25_system_id, rfss, site, site_key,"
            " observation_status) VALUES (1, 1, 1, 30, 'BEE00:37D:1:30', 'reference_only')"
        )
        connection.execute(
            "INSERT INTO geo_solutions(solve_batch_id, p25_site_id, solved_at, method,"
            " source_model, status, detection_count, non_detection_count, excluded_count,"
            " level_metric, tool_version, campaign_id)"
            " VALUES ('historic', 1, '2026-07-01T00:00:00+00:00', 'grid', 'single', 'ok',"
            " 2, 1, 0, 'snr_db', '0.10.0', NULL)"
        )
        connection.commit()
    finally:
        connection.close()
    clear_binding()

    _assign(manifest, "--run-id", "legacy1", "--write", "--reason", "keep history")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT campaign_id, status FROM geo_solutions WHERE solve_batch_id = 'historic'"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, "the historical solution was deleted"
    assert row["campaign_id"] is None, "it was relabelled"
    assert row["status"] == "ok", "it was rewritten"


def test_a_failed_recompute_rolls_the_whole_assignment_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of one transaction: no membership without its analysis."""
    manifest, database = _workspace(tmp_path)

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the rebuild fell over")

    monkeypatch.setattr("dmr_iq_surveyor.geo.pipeline.materialise_within", _explode)

    result = _assign(manifest, "--run-id", "legacy1", "--write", "--reason", "will fail")

    assert result.exit_code != 0
    assert _campaign_of(database, "legacy1") is None, "the run was left assigned"
    assert _audit(database) == [], "an audit row survived a rolled-back assignment"
    connection = connect_geo_database(database)
    try:
        assert superseded_analysis(connection, "day1") is None
    finally:
        connection.close()
    clear_binding()


def test_the_new_solution_uses_only_the_target_campaigns_runs(tmp_path: Path) -> None:
    """`input_run_ids_json` must not reach outside the round it is stamped with."""
    import json

    from fixtures.geo_scenario import Transmitter, build_database, seed_run

    from dmr_iq_surveyor.geo.pipeline import materialise_measurements, solve_all_sites
    from dmr_iq_surveyor.survey.scope import CampaignScope

    manifest = _project(tmp_path / "proj")
    _campaign(tmp_path / "proj")
    database = tmp_path / "proj" / "db.sqlite3"
    transmitters = [Transmitter(frequency_hz=866_012_500.0, latitude=32.08, longitude=34.78)]
    connection = build_database(database)
    try:
        for index, (lat, lon) in enumerate(
            [(32.05, 34.75), (32.10, 34.80), (32.07, 34.85), (32.12, 34.72)]
        ):
            seed_run(
                connection,
                run_id=f"r{index}",
                latitude=lat,
                longitude=lon,
                transmitters=transmitters,
                capture_start_utc=f"2026-08-01T1{index}:00:00+00:00",
            )
        connection.commit()
        write_claim(connection, project_id="p25_central_il", analyzer=ANALYZER)
    finally:
        connection.close()
    clear_binding()

    result = _assign(
        manifest, "--run-id", "r0", "--run-id", "r1", "--run-id", "r2",
        "--write", "--reason", "the first three stops",
    )
    assert result.exit_code == 0, result.output

    materialise_measurements(database_path=database, scope=CampaignScope("day1"))
    solve_all_sites(database_path=database, scope=CampaignScope("day1"))

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT input_run_ids_json FROM geo_solutions WHERE campaign_id = 'day1'"
        ).fetchall()
    finally:
        connection.close()
    assert rows, "the campaign solve stored nothing"
    used = {run_id for row in rows for run_id in json.loads(row["input_run_ids_json"])}
    assert used <= {"r0", "r1", "r2"}, f"the solve read a run outside the campaign: {used}"
    assert "r3" not in used


def test_the_reference_gain_is_redrawn_from_the_new_membership(tmp_path: Path) -> None:
    """A moved run changes the yardstick every run in the campaign is measured by."""
    from fixtures.geo_scenario import Transmitter, build_database, seed_run

    manifest = _project(tmp_path / "proj")
    _campaign(tmp_path / "proj")
    database = tmp_path / "proj" / "db.sqlite3"
    transmitters = [Transmitter(frequency_hz=866_012_500.0, latitude=32.08, longitude=34.78)]
    connection = build_database(database)
    try:
        seed_run(
            connection, run_id="normal", latitude=32.05, longitude=34.75,
            transmitters=transmitters, site_id="siteA", gain=40.0,
        )
        seed_run(
            connection, run_id="odd", latitude=32.10, longitude=34.80,
            transmitters=transmitters, site_id="siteB", gain=20.0,
        )
        connection.commit()
        write_claim(connection, project_id="p25_central_il", analyzer=ANALYZER)
    finally:
        connection.close()
    clear_binding()

    result = _assign(
        manifest, "--run-id", "normal", "--run-id", "odd",
        "--write", "--reason", "both stops",
    )

    assert result.exit_code == 0, result.output
    assert "mixed gain" in result.output.replace("\n", ""), (
        "a campaign assembled from stops at different gain must say so"
    )


def test_mixed_gain_is_a_warning_and_never_an_inferred_value(tmp_path: Path) -> None:
    manifest, _ = _workspace(
        tmp_path, runs=[("nogain", "2026-08-01T10:00:00+00:00", None)]
    )

    result = _assign(manifest, "--run-id", "nogain")

    flat = result.output.replace("\n", "")
    assert "not recorded" in flat
    assert "no value is inferred" in flat or "no recorded gain" in flat


# -- upgrading an existing database --------------------------------------------


def test_a_database_without_the_curation_tables_upgrades_in_place(tmp_path: Path) -> None:
    """An older database gains the tables by being opened, and loses no row."""
    path = tmp_path / "proj" / "db.sqlite3"
    path.parent.mkdir(parents=True)
    connection = connect_geo_database(path)
    try:
        _add_run(connection, "legacy1")
        connection.commit()
        write_claim(connection, project_id="p25_central_il", analyzer=ANALYZER)
        connection.execute("DROP TABLE campaign_assignments")
        connection.execute("DROP TABLE campaign_analysis_state")
        connection.commit()
    finally:
        connection.close()
    clear_binding()

    manifest = _project(tmp_path / "proj")
    _campaign(tmp_path / "proj")

    result = _assign(manifest, "--run-id", "legacy1", "--write", "--reason", "upgraded")

    assert result.exit_code == 0, result.output
    assert _campaign_of(path, "legacy1") == "day1"
    assert len(_audit(path)) == 1


def test_the_dry_run_does_not_migrate_an_older_database(tmp_path: Path) -> None:
    """A command documented as writing nothing must not be why a table appeared."""
    path = tmp_path / "proj" / "db.sqlite3"
    path.parent.mkdir(parents=True)
    connection = connect_geo_database(path)
    try:
        _add_run(connection, "legacy1")
        connection.commit()
        write_claim(connection, project_id="p25_central_il", analyzer=ANALYZER)
        connection.execute("DROP TABLE campaign_assignments")
        connection.execute("DROP TABLE campaign_analysis_state")
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    clear_binding()

    manifest = _project(tmp_path / "proj")
    _campaign(tmp_path / "proj")
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    result = _assign(manifest, "--run-id", "legacy1")

    assert result.exit_code == 0, result.output
    assert path.read_bytes() == before_bytes, "the dry run wrote the new tables"
    assert path.stat().st_mtime_ns == before_mtime
