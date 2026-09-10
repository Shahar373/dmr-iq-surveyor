"""The digest has to say how each receiver setting is known.

A gain the radio reported back, a gain it was only asked for, and a gain the
site profile declared before the campaign started are three different claims.
A page of text that renders them identically invites the reader to treat a
declaration as a measurement, which is the mistake the provenance column
exists to make impossible.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from dmr_iq_surveyor.survey.profiles import SiteProfile
from dmr_iq_surveyor.survey.provenance import (
    declared_bucket,
    hardware_from_capture_manifest,
    hardware_provenance,
    requested_bucket,
    with_declared,
)
from dmr_iq_surveyor.survey.store import (
    SurveyRunRecord,
    connect_survey_database,
    import_survey_run,
    upsert_site,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "campaign_digest.py"

SITE = SiteProfile(
    site_id="home",
    label="Home",
    latitude=32.05,
    longitude=34.79,
    gain_mode="manual",
    gain=40.0,
    lna_state=2,
)


def _record(run_id: str, **overrides) -> SurveyRunRecord:
    fields = {
        "survey_run_id": run_id,
        "site_id": SITE.site_id,
        "band_profile": "central_800",
        "source_path": f"/tmp/{run_id}.wav",
        "source_sha256": None,
        "center_frequency_hz": 868_000_000.0,
        "sample_rate_hz": 5_000_000.0,
        "capture_start_utc": "2026-09-01T09:00:00+00:00",
        "capture_time_source": "auxi",
        "requested_start_hz": 866_000_000.0,
        "requested_stop_hz": 870_000_000.0,
        "usable_low_hz": 866_100_000.0,
        "usable_high_hz": 869_900_000.0,
        "coverage_status": "complete",
        "duration_seconds": 90.0,
        "analyzed_seconds": 30.0,
        "segment_count": 30,
        "occupancy_threshold_db": 8.0,
        "detection_settings": {"scan_step_hz": 6250.0},
        "tool_version": "0.10.0",
    }
    fields.update(overrides)
    return SurveyRunRecord(**fields)


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "campaign.sqlite3"
    connection = connect_survey_database(path)
    try:
        upsert_site(connection, SITE)
        read_back = with_declared(
            hardware_from_capture_manifest(
                {
                    "wav_path": "/tmp/applied_stop.wav",
                    "settings": {
                        "if_gain_reduction_db": 40.0,
                        "lna_state": 2,
                        "driver": "sdrplay",
                    },
                    "device_settings_applied": {"gains": {"IFGR": 25.0, "RFGR": 2.0}},
                }
            ),
            declared_bucket(SITE),
        )
        for run_id, campaign, hardware in (
            ("applied_stop", "day1", read_back),
            (
                "requested_stop",
                "day1",
                hardware_provenance(
                    requested=requested_bucket(if_gain_reduction_db=26.0, lna_state=8)
                ),
            ),
            # A run that observed nothing but recorded the profile it was
            # taken under.
            ("declared_stop", "day2", hardware_provenance(declared=declared_bucket(SITE))),
            # Written before the column existed: nothing at all, so the
            # mutable `sites` row is the only thing a reader can fall back on.
            ("legacy_stop", None, None),
        ):
            import_survey_run(
                connection,
                run=_record(run_id, campaign_id=campaign, hardware=hardware),
                observations=[],
                raster_tolerance_hz=6250.0,
            )
    finally:
        connection.close()
    return path


def _digest(database: Path, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--database", str(database), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_every_gain_says_whether_it_was_measured_asked_for_or_declared(tmp_path: Path) -> None:
    output = _digest(_database(tmp_path))

    # Three stops, three different gains, so the comparability warning fires.
    assert "IF GAIN VARIES ACROSS STOPS" in output
    assert "25.0 dB IFGR (applied)" in output
    assert "26.0 dB IFGR (requested)" in output
    assert "40.0 dB IFGR (declared)" in output
    # The one run with nothing of its own falls back to the shared row,
    # and says so rather than passing it off as the run's declaration.
    assert "40.0 dB IFGR (declared, from the site row)" in output


def test_the_campaign_breakdown_names_unassigned_runs_as_such(tmp_path: Path) -> None:
    output = _digest(_database(tmp_path))

    assert "campaigns" in output
    assert "day1 (2)" in output
    assert "day2 (1)" in output
    assert "unassigned (1)" in output


def test_a_campaign_filter_narrows_the_digest_to_that_round(tmp_path: Path) -> None:
    output = _digest(_database(tmp_path), "--campaign", "day1")

    assert "campaign day1" in output
    assert "stops total          2" in output
    assert "day1 (2)" in output
    assert "unassigned" not in output
    # The stop that only had the shared row is not in this round at all, so
    # neither is its fallback reading.
    assert "from the site row" not in output


def test_an_unknown_campaign_reports_an_empty_round_rather_than_everything(
    tmp_path: Path,
) -> None:
    output = _digest(_database(tmp_path), "--campaign", "day9")

    assert "stops total          0" in output
    assert "25.0 dB IFGR" not in output


def test_a_campaign_run_now_narrows_every_section(tmp_path: Path) -> None:
    """This replaces a test that asserted the opposite.

    The digest used to skip evidence, solutions and the plan under
    `--campaign` and say it was skipping them, because those three sections
    read the whole database and printing them under a heading naming one
    round would invite every number to be read as that round's. They are
    genuinely scoped now -- measurements by joining `survey_runs`, solutions
    and plans by the campaign their solve was scoped to -- so the sections
    are shown, and the message saying they could not be is gone.

    The old assertion is preserved by inversion: the notice must NOT appear,
    so this fails if the skip block ever comes back without the scoping.
    """
    output = _digest(_database(tmp_path), "--campaign", "day1")

    assert "campaign day1" in output
    assert "WHAT COUNTED AS EVIDENCE" in output
    assert "WHAT THE SOLVER CONCLUDED" in output
    assert "NOT SHOWN FOR A SINGLE CAMPAIGN" not in output
    assert "not campaign-scoped yet" not in output


def test_a_campaign_with_no_solve_of_its_own_says_so_rather_than_borrowing_one(
    tmp_path: Path,
) -> None:
    """A batch solved without `--campaign` read every run in the file.
    Showing its numbers under one round's heading is exactly the
    mislabelling the old skip block existed to prevent."""
    output = _digest(_database(tmp_path), "--campaign", "day1")

    assert "nothing solved for campaign day1" in output
    assert "no plan for campaign day1" in output


def test_without_a_campaign_the_whole_file_is_reported_as_before(tmp_path: Path) -> None:
    output = _digest(_database(tmp_path))

    assert "WHAT COUNTED AS EVIDENCE" in output
    assert "WHAT THE SOLVER CONCLUDED" in output
    assert "NOT SHOWN FOR A SINGLE CAMPAIGN" not in output


def test_a_row_with_junk_inside_a_valid_blob_does_not_bring_the_digest_down(
    tmp_path: Path,
) -> None:
    """Structure alone is not enough.

    A blob can be valid JSON, carry every bucket as a mapping, and still hold
    a list where a gain belongs. Counting or formatting that raises, so the
    whole page dies over one row. It has to read as not recorded instead --
    a report about a campaign must never be the thing that fails.
    """
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE survey_runs SET hardware_json = ? WHERE survey_run_id = 'applied_stop'",
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "applied",
                        "identity": {},
                        # Valid JSON, valid buckets, and unusable: a list is not
                        # a gain, and `Counter` cannot even hold one.
                        "applied": {"gains": {"IFGR": [], "RFGR": {"nested": 1}}},
                        "requested": {"lna_state": [1, 2]},
                        "declared": {},
                    }
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    output = _digest(database)

    assert "WHAT WAS COLLECTED" in output
    assert "stops total          4" in output
    # The unusable blob reads as though the run recorded nothing, so the run
    # falls back to the shared row and says that is where the number came
    # from. It is not passed off as the read-back the blob claimed to hold.
    assert "(declared, from the site row): 2 stop(s)" in output
    assert "25.0 dB IFGR (applied)" not in output
    # The rows that are fine still read normally.
    assert "26.0 dB IFGR (requested)" in output


# -- the whole digest, scoped end to end -------------------------------------


def _geo_database(tmp_path: Path) -> Path:
    """A real geo database holding two rounds, each solved for itself.

    Built with the shared geo fixture rather than by hand, so the rows the
    digest reads are the rows the pipeline actually writes.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fixtures.geo_scenario import Transmitter, build_database, fast_solve_settings, seed_run

    from dmr_iq_surveyor.geo.pipeline import materialise_measurements, solve_all_sites
    from dmr_iq_surveyor.survey.scope import CampaignScope

    transmitter = Transmitter(
        867_762_500.0, 32.050, 34.800, reference_level_db=25.0, path_loss_exponent=3.4
    )
    path = tmp_path / "geo.sqlite3"
    connection = build_database(path)
    try:
        for index, (latitude, longitude) in enumerate(
            [(32.070, 34.770), (32.075, 34.775), (32.080, 34.790)]
        ):
            seed_run(
                connection,
                run_id=f"day1_{index}",
                latitude=latitude,
                longitude=longitude,
                transmitters=[transmitter],
                site_id=f"d1_{index}",
                campaign_id="day1",
            )
        for index, (latitude, longitude) in enumerate(
            [(32.030, 34.820), (32.035, 34.825), (32.040, 34.830)]
        ):
            seed_run(
                connection,
                run_id=f"day2_{index}",
                latitude=latitude,
                longitude=longitude,
                transmitters=[transmitter],
                site_id=f"d2_{index}",
                campaign_id="day2",
            )
    finally:
        connection.close()

    materialise_measurements(database_path=path)
    solve_all_sites(
        database_path=path,
        settings=fast_solve_settings(),
        solve_batch_id="b_day1",
        scope=CampaignScope("day1"),
    )
    solve_all_sites(
        database_path=path,
        settings=fast_solve_settings(),
        solve_batch_id="b_day2",
        scope=CampaignScope("day2"),
    )
    return path


def _evidence_counts(output: str) -> tuple[int, int]:
    """The `usable N detection(s), M non-detection(s)` line, as numbers."""
    for line in output.splitlines():
        if line.strip().startswith("usable"):
            parts = line.replace("(s)", "").split()
            return int(parts[1]), int(parts[3])
    raise AssertionError(f"no usable line in:\n{output}")


def test_the_digest_never_mixes_two_campaigns_end_to_end(tmp_path: Path) -> None:
    """The no-mixing proof at the level an operator actually reads.

    Each round's evidence counts are strictly smaller than the whole file's,
    and the two rounds' counts add up to it. If any section leaked, one of
    those two facts would break.
    """
    path = _geo_database(tmp_path)

    whole = _evidence_counts(_digest(path))
    first = _evidence_counts(_digest(path, "--campaign", "day1"))
    second = _evidence_counts(_digest(path, "--campaign", "day2"))

    assert first[0] > 0 and second[0] > 0
    assert first[0] + second[0] == whole[0]
    assert first[1] + second[1] == whole[1]
    assert first < whole


def test_each_round_reports_its_own_solve_and_plan(tmp_path: Path) -> None:
    path = _geo_database(tmp_path)

    first = _digest(path, "--campaign", "day1")
    second = _digest(path, "--campaign", "day2")

    assert "WHAT THE SOLVER CONCLUDED" in first
    assert "nothing solved for campaign" not in first
    assert "no plan for campaign" not in second
    # Each names its own batch and not the other's.
    assert "b_day1" in first and "b_day2" not in first
    assert "b_day2" in second and "b_day1" not in second


def test_an_unassigned_run_reaches_the_whole_file_digest_only(tmp_path: Path) -> None:
    path = _geo_database(tmp_path)
    from fixtures.geo_scenario import Transmitter, seed_run

    from dmr_iq_surveyor.geo.pipeline import materialise_measurements
    from dmr_iq_surveyor.geo.store import connect_geo_database

    connection = connect_geo_database(path)
    try:
        seed_run(
            connection,
            run_id="legacy",
            latitude=32.06,
            longitude=34.76,
            transmitters=[
                Transmitter(
                    867_762_500.0, 32.050, 34.800, reference_level_db=25.0,
                    path_loss_exponent=3.4,
                )
            ],
            site_id="legacy_stop",
            campaign_id=None,
        )
    finally:
        connection.close()
    materialise_measurements(database_path=path)

    # The unassigned stop's evidence is counted by the whole-file digest and
    # by neither round's -- it was not taken under either.
    before = _evidence_counts(_digest(path, "--campaign", "day1"))
    whole = _evidence_counts(_digest(path))
    second = _evidence_counts(_digest(path, "--campaign", "day2"))

    assert whole[0] > before[0] + second[0]
    assert "unassigned" in _digest(path)
