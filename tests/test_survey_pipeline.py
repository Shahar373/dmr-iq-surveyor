from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fixtures.synthetic import SyntheticTone, write_synthetic_iq_wav
from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app
from dmr_iq_surveyor.spectrum.core import SpectrumSettings
from dmr_iq_surveyor.survey.pipeline import run_comparison, run_survey
from dmr_iq_surveyor.survey.profiles import BandProfile, SiteProfile
from dmr_iq_surveyor.survey.store import connect_survey_database, fetch_survey_table

SAMPLE_RATE_HZ = 200_000
CENTER_HZ = 868_000_000


def _band_profile() -> BandProfile:
    return BandProfile(
        name="test_band",
        label="test",
        start_frequency_hz=867_800_000.0,
        stop_frequency_hz=868_200_000.0,
        raster_spacings_hz=[12500.0, 6250.0],
        detection_overrides={
            "scan_step_hz": 6250.0,
            "integration_width_hz": 12500.0,
            "min_p95_channel_snr_db": 9.0,
            "min_average_channel_snr_db": 4.0,
            "min_equivalent_width_hz": 1500.0,
            "min_width_90_hz": 1000.0,
            "max_width_90_hz": 13000.0,
            "merge_tolerance_hz": 4000.0,
            "passband_warning_low_hz": 866_000_000.0,
            "passband_warning_high_hz": 870_000_000.0,
        },
        segment_seconds=1.0,
        segment_stride_seconds=1.0,
        max_segments=10,
    )


def _write_fixture(path: Path, *, amplitude: float, capture_start: datetime, extra_tone: bool = False) -> None:
    tones = [SyntheticTone(offset_hz=50_000.0, amplitude=amplitude)]
    if extra_tone:
        tones.append(SyntheticTone(offset_hz=-70_000.0, amplitude=0.35))
    write_synthetic_iq_wav(
        path,
        sample_rate_hz=SAMPLE_RATE_HZ,
        center_frequency_hz=CENTER_HZ,
        duration_seconds=6.0,
        tones=tones,
        capture_start_utc=capture_start,
    )


def test_spectrum_settings_has_no_segment_fields() -> None:
    """Segmentation lives entirely in survey/discovery.py, not
    spectrum/core.py -- run_spectrum() must stay byte-identical to its
    pre-Phase-6 behaviour. This guards against a future change threading
    segment parameters into SpectrumSettings by accident."""
    fields = set(SpectrumSettings.__dataclass_fields__)
    assert "segment_seconds" not in fields
    assert "segment_stride_seconds" not in fields


def test_run_survey_writes_expected_artifacts_and_records_peak_rss(tmp_path: Path) -> None:
    wav = tmp_path / "recording.wav"
    _write_fixture(wav, amplitude=0.35, capture_start=datetime(2026, 8, 1, tzinfo=UTC))
    db = tmp_path / "db.sqlite3"
    output = tmp_path / "run1"

    summary = run_survey(
        wav,
        output,
        band=_band_profile(),
        site=SiteProfile(site_id="home", label="Home"),
        run_id="r1",
        database_path=db,
        spectrum_fft_size=4096,
    )
    assert summary["observation_count"] >= 1
    assert summary["peak_rss_bytes"] > 0

    assert (output / "run.json").is_file()
    assert (output / "reports" / "report.json").is_file()
    assert (output / "reports" / "report.md").is_file()
    assert (output / "logs" / "survey.log").is_file()

    manifest = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert manifest["peak_rss_bytes"] > 0
    assert manifest["survey_run_id"] == "r1"

    log_text = (output / "logs" / "survey.log").read_text(encoding="utf-8")
    assert "FAILED" not in log_text
    assert "candidate found" in log_text


def test_run_survey_is_idempotent(tmp_path: Path) -> None:
    wav = tmp_path / "recording.wav"
    _write_fixture(wav, amplitude=0.35, capture_start=datetime(2026, 8, 1, tzinfo=UTC))
    db = tmp_path / "db.sqlite3"
    band = _band_profile()
    site = SiteProfile(site_id="home", label="Home")

    run_survey(wav, tmp_path / "a", band=band, site=site, run_id="r1", database_path=db, spectrum_fft_size=4096)
    connection = connect_survey_database(db)
    first_count = len(fetch_survey_table(connection, "rf_observations"))
    connection.close()

    run_survey(wav, tmp_path / "b", band=band, site=site, run_id="r1", database_path=db, spectrum_fft_size=4096)
    connection = connect_survey_database(db)
    second_count = len(fetch_survey_table(connection, "rf_observations"))
    assert len(fetch_survey_table(connection, "survey_runs")) == 1
    connection.close()
    assert first_count == second_count


def test_run_survey_then_compare_end_to_end(tmp_path: Path) -> None:
    wav1 = tmp_path / "r1.wav"
    wav2 = tmp_path / "r2.wav"
    _write_fixture(wav1, amplitude=0.35, capture_start=datetime(2026, 8, 1, tzinfo=UTC))
    _write_fixture(wav2, amplitude=0.6, capture_start=datetime(2026, 8, 10, tzinfo=UTC), extra_tone=True)
    db = tmp_path / "db.sqlite3"
    band = _band_profile()
    site = SiteProfile(site_id="home", label="Home")

    run_survey(wav1, tmp_path / "run1", band=band, site=site, run_id="r1", database_path=db, spectrum_fft_size=4096)
    run_survey(wav2, tmp_path / "run2", band=band, site=site, run_id="r2", database_path=db, spectrum_fft_size=4096)

    report = run_comparison(
        tmp_path / "compare", baseline_run_id="r1", target_run_id="r2", database_path=db, tolerances_from=band
    )
    assert report["status_counts"].get("NEW", 0) >= 1
    assert (tmp_path / "compare" / "reports" / "comparison_r1_r2.json").is_file()
    assert (tmp_path / "compare" / "reports" / "comparison_r1_r2.md").is_file()


def test_survey_cli_run_list_show_compare(tmp_path: Path, monkeypatch) -> None:
    """Exercises the actual Typer CLI end to end, including the pre-existing
    (non-Phase-6) `--help` surface remaining intact."""
    runner = CliRunner()

    top_level = runner.invoke(app, ["--help"])
    assert top_level.exit_code == 0
    assert "inspect" in top_level.output
    assert "survey" in top_level.output

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config" / "bands").mkdir(parents=True)
    (tmp_path / "config" / "sites").mkdir(parents=True)
    band_yaml = (
        "name: cli_band\nlabel: cli\nstart_frequency_hz: 867800000\nstop_frequency_hz: 868200000\n"
        "raster_spacings_hz: [12500, 6250]\n"
        "detection:\n"
        "  scan_step_hz: 6250\n  integration_width_hz: 12500\n"
        "  min_p95_channel_snr_db: 9.0\n  min_average_channel_snr_db: 4.0\n"
        "  min_equivalent_width_hz: 1500\n  min_width_90_hz: 1000\n"
        "  max_width_90_hz: 13000\n  merge_tolerance_hz: 4000\n"
        "  passband_warning_low_hz: 866000000\n  passband_warning_high_hz: 870000000\n"
        "segment_seconds: 1.0\nsegment_stride_seconds: 1.0\nmax_segments: 10\n"
    )
    (tmp_path / "config" / "bands" / "cli_band.yaml").write_text(band_yaml, encoding="utf-8")
    (tmp_path / "config" / "sites" / "cli_site.yaml").write_text(
        "site_id: cli_site\nlabel: CLI site\n", encoding="utf-8"
    )

    wav1 = tmp_path / "r1.wav"
    wav2 = tmp_path / "r2.wav"
    _write_fixture(wav1, amplitude=0.35, capture_start=datetime(2026, 8, 1, tzinfo=UTC))
    _write_fixture(wav2, amplitude=0.6, capture_start=datetime(2026, 8, 10, tzinfo=UTC), extra_tone=True)
    db = tmp_path / "db.sqlite3"

    result_run1 = runner.invoke(
        app,
        [
            "survey", "run", str(wav1),
            "--band", "cli_band", "--site", "cli_site",
            "--run-id", "cli_r1", "--database", str(db),
            "--output", "runs/cli_r1", "--fft-size", "4096",
        ],
    )
    assert result_run1.exit_code == 0, result_run1.output

    result_run2 = runner.invoke(
        app,
        [
            "survey", "run", str(wav2),
            "--band", "cli_band", "--site", "cli_site",
            "--run-id", "cli_r2", "--database", str(db),
            "--output", "runs/cli_r2", "--fft-size", "4096",
        ],
    )
    assert result_run2.exit_code == 0, result_run2.output

    result_list = runner.invoke(app, ["survey", "list", "--database", str(db)])
    assert result_list.exit_code == 0
    assert "cli_r1" in result_list.output
    assert "cli_r2" in result_list.output

    result_show = runner.invoke(app, ["survey", "show", "cli_r2", "--database", str(db)])
    assert result_show.exit_code == 0
    assert "cli_r2" in result_show.output

    result_compare = runner.invoke(
        app,
        ["survey", "compare", "cli_r1", "cli_r2", "--database", str(db), "--band", "cli_band"],
    )
    assert result_compare.exit_code == 0
    assert "NEW" in result_compare.output
