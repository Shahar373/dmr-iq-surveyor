from __future__ import annotations

import csv
from pathlib import Path

import yaml

from dmr_iq_surveyor.batch import run_batch_inspection
from test_metadata import create_riff


def test_batch_inspection_creates_shared_summary(tmp_path: Path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    create_riff(first, rf64=True)
    create_riff(second, rf64=False)
    output = tmp_path / "batch-output"
    config = tmp_path / "recordings.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "project": {"name": "test batch", "output_root": str(output)},
                "inspection": {
                    "statistics_window_frames": 4,
                    "diagnostic_plot_frames": 4,
                    "compute_sha256": False,
                },
                "recordings": [
                    {"id": "first", "path": str(first)},
                    {"id": "second", "path": str(second)},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_batch_inspection(config)

    assert result["consistency"]["successful_recordings"] == 2
    assert result["consistency"]["same_center_frequency"] is True
    assert result["consistency"]["same_sample_rate"] is True
    assert (output / "batch_summary.csv").is_file()
    assert (output / "batch_summary.json").is_file()
    assert (output / "batch_report.md").is_file()
    assert (output / "recordings" / "first" / "report.md").is_file()
    assert (output / "recordings" / "second" / "report.md").is_file()

    with (output / "batch_summary.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["recording_id"] for row in rows] == ["first", "second"]
    assert all(row["status"] == "ok" for row in rows)


def test_batch_reports_missing_file_without_losing_other_results(tmp_path: Path):
    present = tmp_path / "present.wav"
    create_riff(present, rf64=True)
    output = tmp_path / "batch-output"
    config = tmp_path / "recordings.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "project": {"output_root": str(output)},
                "inspection": {
                    "statistics_window_frames": 4,
                    "diagnostic_plot_frames": 4,
                },
                "recordings": [
                    {"id": "present", "path": str(present)},
                    {"id": "missing", "path": str(tmp_path / "missing.wav")},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_batch_inspection(config)

    assert result["consistency"]["successful_recordings"] == 1
    assert result["consistency"]["failed_recordings"] == 1
    statuses = {row["recording_id"]: row["status"] for row in result["rows"]}
    assert statuses == {"present": "ok", "missing": "failed"}


def test_batch_report_handles_missing_center_frequency(tmp_path: Path):
    source = tmp_path / "recording_without_frequency.wav"
    create_riff(source, rf64=True, include_auxi=False)
    output = tmp_path / "batch-output"
    config = tmp_path / "recordings.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "project": {"output_root": str(output)},
                "inspection": {
                    "statistics_window_frames": 4,
                    "diagnostic_plot_frames": 4,
                },
                "recordings": [{"id": "missing-center", "path": str(source)}],
            }
        ),
        encoding="utf-8",
    )

    result = run_batch_inspection(config)

    assert result["consistency"]["successful_recordings"] == 1
    assert result["consistency"]["center_frequencies_hz"] == []
    row = result["rows"][0]
    assert row["center_frequency_hz"] is None
    assert row["center_frequency_source"] == "missing"
    report = (output / "batch_report.md").read_text(encoding="utf-8")
    assert "| `missing-center` | ok | - |" in report


def test_batch_uses_filename_center_frequency_fallback(tmp_path: Path):
    source = tmp_path / "SDRconnect_IQ_20260713_150256_163671500HZ.wav"
    create_riff(source, rf64=True, include_auxi=False)
    output = tmp_path / "batch-output"
    config = tmp_path / "recordings.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "project": {"output_root": str(output)},
                "inspection": {
                    "statistics_window_frames": 4,
                    "diagnostic_plot_frames": 4,
                },
                "recordings": [{"id": "filename-center", "path": str(source)}],
            }
        ),
        encoding="utf-8",
    )

    result = run_batch_inspection(config)

    row = result["rows"][0]
    assert row["center_frequency_hz"] == 163_671_500
    assert row["center_frequency_source"] == "filename"
    assert result["consistency"]["center_frequencies_hz"] == [163_671_500]
