from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np


def load_series(
    path: Path,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    data = np.genfromtxt(
        path,
        delimiter=",",
        names=True,
        dtype=None,
        encoding="utf-8",
    )
    names = list(data.dtype.names or ())
    if len(names) < 4 or names[0] != "frequency_hz":
        raise ValueError(f"Unexpected spectrum CSV schema: {path}")
    return (
        np.asarray(data[names[0]], dtype=np.float64),
        np.asarray(data[names[1]], dtype=np.float64),
        np.asarray(data[names[2]], dtype=bool),
        np.asarray(data[names[3]], dtype=bool),
    )


def load_spectrum(spectrum_dir: Path) -> dict[str, Any]:
    average = load_series(spectrum_dir / "average_spectrum.csv")
    percentile = load_series(spectrum_dir / "percentile_spectrum.csv")
    noise = load_series(spectrum_dir / "noise_floor.csv")
    occupancy = load_series(spectrum_dir / "occupancy.csv")
    frequency = average[0]
    for candidate_axis in (percentile[0], noise[0], occupancy[0]):
        if len(candidate_axis) != len(frequency):
            raise ValueError("Spectrum CSV frequency axes do not match")
        if not np.allclose(candidate_axis, frequency, atol=0.01):
            raise ValueError("Spectrum CSV frequency axes do not match")
    report = json.loads(
        (spectrum_dir / "spectrum_report.json").read_text(
            encoding="utf-8"
        )
    )
    waterfall = np.load(
        spectrum_dir / "waterfall.npy",
        mmap_mode="r",
    )
    axes = np.load(spectrum_dir / "waterfall_axes.npz")
    return {
        "frequency_hz": frequency,
        "average_db": average[1],
        "percentile_db": percentile[1],
        "noise_db": noise[1],
        "occupancy_pct": occupancy[1],
        "edge_mask": average[2],
        "dc_mask": average[3],
        "waterfall_db": waterfall,
        "waterfall_frequency_hz": axes["frequency_hz"],
        "waterfall_time_s": axes["time_s"],
        "report": report,
    }
