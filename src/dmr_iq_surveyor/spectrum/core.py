from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterator

import numpy as np
from numpy.typing import NDArray


@dataclass(slots=True)
class SpectrumSettings:
    fft_size: int = 65_536
    window: str = "hann"
    overlap_ratio: float = 0.5
    waterfall_time_bins: int = 500
    waterfall_frequency_bins: int = 8_192
    edge_exclusion_hz: float = 150_000.0
    dc_exclusion_hz: float = 10_000.0
    percentile: float = 95.0
    percentile_max_frames: int = 256
    local_noise_window_hz: float = 200_000.0
    occupancy_threshold_db: float = 8.0

    def validate(self) -> None:
        if self.fft_size < 16 or self.fft_size & (self.fft_size - 1):
            raise ValueError("fft_size must be a power of two and at least 16")
        if not 0.0 <= self.overlap_ratio < 1.0:
            raise ValueError("overlap_ratio must be in [0, 1)")
        if self.window not in {"hann", "rectangular"}:
            raise ValueError("window must be 'hann' or 'rectangular'")
        if self.waterfall_time_bins < 1 or self.waterfall_frequency_bins < 1:
            raise ValueError("waterfall bin counts must be positive")
        if not 0.0 <= self.percentile <= 100.0:
            raise ValueError("percentile must be in [0, 100]")
        if self.percentile_max_frames < 1:
            raise ValueError("percentile_max_frames must be positive")
        if self.local_noise_window_hz <= 0:
            raise ValueError("local_noise_window_hz must be positive")

    def to_dict(self) -> dict[str, float | int | str]:
        return asdict(self)


def fft_step(fft_size: int, overlap_ratio: float) -> int:
    step = int(round(fft_size * (1.0 - overlap_ratio)))
    if step < 1:
        raise ValueError("overlap leaves an FFT step smaller than one sample")
    return step


def fft_frame_count(total_frames: int, fft_size: int, overlap_ratio: float) -> int:
    if total_frames < fft_size:
        return 0
    step = fft_step(fft_size, overlap_ratio)
    return 1 + (total_frames - fft_size) // step


def iter_fft_starts(
    total_frames: int, fft_size: int, overlap_ratio: float
) -> Iterator[int]:
    count = fft_frame_count(total_frames, fft_size, overlap_ratio)
    step = fft_step(fft_size, overlap_ratio)
    for index in range(count):
        yield index * step


def frequency_axis_hz(
    center_frequency_hz: float, sample_rate_hz: float, fft_size: int
) -> NDArray[np.float64]:
    offsets = np.fft.fftshift(np.fft.fftfreq(fft_size, d=1.0 / sample_rate_hz))
    return center_frequency_hz + offsets


def build_window(name: str, fft_size: int) -> NDArray[np.float64]:
    if name == "hann":
        return np.hanning(fft_size).astype(np.float64)
    if name == "rectangular":
        return np.ones(fft_size, dtype=np.float64)
    raise ValueError(f"Unsupported window: {name}")


def periodogram_power_density(
    samples: NDArray[np.complexfloating],
    window: NDArray[np.float64],
    sample_rate_hz: float,
) -> NDArray[np.float64]:
    if samples.ndim != 1 or samples.size != window.size:
        raise ValueError("samples and window must be one-dimensional arrays of equal length")
    window_energy = float(np.sum(window * window))
    if window_energy <= 0:
        raise ValueError("window has zero energy")
    transformed = np.fft.fftshift(np.fft.fft(samples * window))
    return (np.abs(transformed) ** 2 / (sample_rate_hz * window_energy)).astype(np.float64)


def power_to_db(power: NDArray[np.floating]) -> NDArray[np.float64]:
    tiny = np.finfo(np.float64).tiny
    return 10.0 * np.log10(np.maximum(power.astype(np.float64, copy=False), tiny))


def local_noise_floor_db(
    spectrum_db: NDArray[np.floating], bins_per_window: int
) -> NDArray[np.float64]:
    values = spectrum_db.astype(np.float64, copy=False)
    if values.ndim != 1:
        raise ValueError("spectrum_db must be one-dimensional")
    width = max(1, int(bins_per_window))
    starts = np.arange(0, values.size, width, dtype=np.int64)
    centers = np.empty(starts.size, dtype=np.float64)
    medians = np.empty(starts.size, dtype=np.float64)
    for index, start in enumerate(starts):
        end = min(values.size, int(start) + width)
        centers[index] = (float(start) + float(end - 1)) / 2.0
        medians[index] = float(np.median(values[int(start):end]))
    if medians.size == 1:
        return np.full(values.size, medians[0], dtype=np.float64)
    return np.interp(np.arange(values.size, dtype=np.float64), centers, medians)


def reduce_frequency_bins(
    power: NDArray[np.floating], output_bins: int
) -> tuple[NDArray[np.float64], NDArray[np.int64], NDArray[np.int64]]:
    if power.ndim != 1:
        raise ValueError("power must be one-dimensional")
    output_bins = min(max(1, int(output_bins)), power.size)
    edges = np.linspace(0, power.size, output_bins + 1, dtype=np.int64)
    starts = edges[:-1]
    widths = np.diff(edges)
    sums = np.add.reduceat(power.astype(np.float64, copy=False), starts)
    return sums / widths, starts, widths


def percentile_frame_indices(total_fft_frames: int, maximum: int) -> NDArray[np.int64]:
    count = min(maximum, total_fft_frames)
    if count <= 0:
        return np.empty(0, dtype=np.int64)
    if count == total_fft_frames:
        return np.arange(total_fft_frames, dtype=np.int64)
    return np.unique(np.linspace(0, total_fft_frames - 1, count, dtype=np.int64))
