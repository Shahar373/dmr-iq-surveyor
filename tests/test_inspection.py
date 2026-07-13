from pathlib import Path

from dmr_iq_surveyor.inspection import run_inspection
from test_metadata import create_riff


def test_full_inspection_outputs(tmp_path: Path):
    source = tmp_path / "recording.wav"
    create_riff(source, rf64=True)
    output = tmp_path / "result"

    result = run_inspection(
        source,
        output,
        statistics_window_frames=4,
        diagnostic_plot_frames=4,
        compute_sha256=True,
    )

    assert result["recording"]["container"] == "RF64"
    for filename in [
        "recording_info.json",
        "sample_statistics.json",
        "chunk_map.csv",
        "diagnostic_time.png",
        "diagnostic_iq.png",
        "report.md",
        "manifest.json",
    ]:
        assert (output / filename).is_file()
