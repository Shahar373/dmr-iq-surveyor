from __future__ import annotations

from dataclasses import asdict, dataclass
from math import gcd
from typing import Iterable

import numpy as np
from numpy.typing import NDArray
from scipy.signal import firwin, oaconvolve, resample_poly


@dataclass(slots=True)
class ExtractionSettings:
    chunk_frames: int = 1_000_000
    first_decimation: int = 10
    second_decimation: int = 10
    first_filter_taps: int = 401
    second_filter_taps: int = 801
    channel_filter_taps: int = 161
    first_cutoff_hz: float = 400_000.0
    second_cutoff_hz: float = 40_000.0
    channel_lowpass_hz: float = 7_500.0
    output_rate_hz: int = 48_000
    trim_seconds: float = 0.005
    normalization_percentile: float = 99.5
    output_peak_fraction: float = 0.90
    preview_samples: int = 100_000

    def validate(self, input_sample_rate_hz: int) -> int:
        if self.chunk_frames < 1:
            raise ValueError("chunk_frames must be positive")
        for name in (
            "first_decimation",
            "second_decimation",
            "first_filter_taps",
            "second_filter_taps",
            "channel_filter_taps",
            "output_rate_hz",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")
        if self.first_filter_taps % 2 == 0 or self.second_filter_taps % 2 == 0:
            raise ValueError("decimation FIR tap counts must be odd")
        if self.channel_filter_taps % 2 == 0:
            raise ValueError("channel_filter_taps must be odd")
        if not 0.0 < self.output_peak_fraction <= 1.0:
            raise ValueError("output_peak_fraction must be in (0, 1]")
        if not 0.0 < self.normalization_percentile <= 100.0:
            raise ValueError("normalization_percentile must be in (0, 100]")
        total_decimation = self.first_decimation * self.second_decimation
        if input_sample_rate_hz % total_decimation:
            raise ValueError(
                "input sample rate must be divisible by first_decimation × second_decimation"
            )
        intermediate_rate = input_sample_rate_hz // total_decimation
        first_rate = input_sample_rate_hz // self.first_decimation
        if self.first_cutoff_hz >= first_rate / 2:
            raise ValueError("first_cutoff_hz must be below the first-stage output Nyquist")
        if self.second_cutoff_hz >= intermediate_rate / 2:
            raise ValueError("second_cutoff_hz must be below the intermediate Nyquist")
        if self.channel_lowpass_hz >= intermediate_rate / 2:
            raise ValueError("channel_lowpass_hz must be below the intermediate Nyquist")
        return intermediate_rate

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class StreamingFIRDecimator:
    """Causal overlap-save FIR filtering followed by phase-continuous decimation."""

    def __init__(self, taps: NDArray[np.float64], factor: int):
        if factor < 1:
            raise ValueError("factor must be positive")
        self.taps = np.asarray(taps, dtype=np.float64)
        self.factor = int(factor)
        self._history = np.zeros(len(self.taps) - 1, dtype=np.complex64)
        self._input_count = 0

    def process(
        self, samples: NDArray[np.complex64 | np.complex128]
    ) -> NDArray[np.complex64]:
        values = np.asarray(samples, dtype=np.complex64)
        combined = np.concatenate((self._history, values))
        convolved = oaconvolve(combined, self.taps, mode="full")
        offset = len(self.taps) - 1
        filtered = convolved[offset : offset + len(values)]
        if len(self._history):
            self._history = combined[-len(self._history) :].astype(
                np.complex64, copy=False
            )
        start = (-self._input_count) % self.factor
        self._input_count += len(values)
        return filtered[start:: self.factor].astype(np.complex64, copy=False)


class StreamingComplexFilter:
    """Causal overlap-save FIR filter for complex baseband samples."""

    def __init__(self, taps: NDArray[np.float64]):
        self.taps = np.asarray(taps, dtype=np.float64)
        self._history = np.zeros(len(self.taps) - 1, dtype=np.complex64)

    def process(
        self, samples: NDArray[np.complex64 | np.complex128]
    ) -> NDArray[np.complex64]:
        values = np.asarray(samples, dtype=np.complex64)
        combined = np.concatenate((self._history, values))
        convolved = oaconvolve(combined, self.taps, mode="full")
        offset = len(self.taps) - 1
        filtered = convolved[offset : offset + len(values)]
        if len(self._history):
            self._history = combined[-len(self._history) :].astype(
                np.complex64, copy=False
            )
        return filtered.astype(np.complex64, copy=False)


class StreamingMixer:
    def __init__(self, frequency_offset_hz: float, sample_rate_hz: float):
        self.phase = 0.0
        self.phase_step = (
            -2.0 * np.pi * float(frequency_offset_hz) / float(sample_rate_hz)
        )

    def process(
        self, samples: NDArray[np.complex64 | np.complex128]
    ) -> NDArray[np.complex64]:
        values = np.asarray(samples, dtype=np.complex64)
        indexes = np.arange(len(values), dtype=np.float64)
        oscillator = np.exp(1j * (self.phase + self.phase_step * indexes))
        self.phase = float(
            (self.phase + self.phase_step * len(values)) % (2.0 * np.pi)
        )
        return (values * oscillator).astype(np.complex64, copy=False)


class StreamingFMDiscriminator:
    def __init__(self):
        self._previous: np.complex64 | None = None

    def process(
        self, samples: NDArray[np.complex64 | np.complex128]
    ) -> NDArray[np.float32]:
        values = np.asarray(samples, dtype=np.complex64)
        if len(values) == 0:
            return np.empty(0, dtype=np.float32)
        if self._previous is None:
            pairs = values[1:] * np.conj(values[:-1])
        else:
            extended = np.empty(len(values) + 1, dtype=np.complex64)
            extended[0] = self._previous
            extended[1:] = values
            pairs = extended[1:] * np.conj(extended[:-1])
        self._previous = values[-1]
        return np.angle(pairs).astype(np.float32)


def design_filters(
    input_sample_rate_hz: int, settings: ExtractionSettings
) -> dict[str, NDArray[np.float64]]:
    intermediate_rate = settings.validate(input_sample_rate_hz)
    first_rate = input_sample_rate_hz / settings.first_decimation
    return {
        "first": firwin(
            settings.first_filter_taps,
            settings.first_cutoff_hz,
            fs=float(input_sample_rate_hz),
            window="hamming",
        ),
        "second": firwin(
            settings.second_filter_taps,
            settings.second_cutoff_hz,
            fs=float(first_rate),
            window="hamming",
        ),
        "channel": firwin(
            settings.channel_filter_taps,
            settings.channel_lowpass_hz,
            fs=float(intermediate_rate),
            window=("kaiser", 7.5),
        ),
    }


def resample_discriminator(
    discriminator: NDArray[np.float32 | np.float64],
    input_rate_hz: int,
    output_rate_hz: int,
) -> NDArray[np.float32]:
    common = gcd(int(input_rate_hz), int(output_rate_hz))
    up = int(output_rate_hz) // common
    down = int(input_rate_hz) // common
    output = resample_poly(
        np.asarray(discriminator, dtype=np.float64),
        up,
        down,
        window=("kaiser", 7.5),
    )
    return output.astype(np.float32)


def normalize_pcm16(
    samples: NDArray[np.float32 | np.float64],
    percentile: float,
    peak_fraction: float,
) -> tuple[NDArray[np.int16], dict[str, float | int | bool]]:
    """Center and normalize discriminator audio without PCM16 clipping.

    The percentile reference keeps normal signals at a useful level, while a
    hard peak-safe scale cap prevents any sample from exceeding the requested
    PCM16 peak fraction. The report records whether the peak cap reduced the
    percentile-derived scale.
    """

    values = np.asarray(samples, dtype=np.float64)
    if len(values) == 0:
        return np.empty(0, dtype=np.int16), {
            "center_removed": 0.0,
            "normalization_reference": 0.0,
            "absolute_peak_reference": 0.0,
            "percentile_scale": 0.0,
            "peak_safe_scale": 0.0,
            "scale": 0.0,
            "limiter_applied": False,
            "would_clip_without_limiter": 0,
            "clipped_samples": 0,
            "peak_pcm": 0,
        }
    center = float(np.median(values))
    centered = values - center
    absolute = np.abs(centered)
    percentile_reference = float(np.percentile(absolute, percentile))
    peak_reference = float(np.max(absolute))
    target = 32767.0 * float(peak_fraction)
    percentile_scale = (
        target / percentile_reference if percentile_reference > 1e-12 else 0.0
    )
    peak_safe_scale = target / peak_reference if peak_reference > 1e-12 else 0.0
    scale = min(percentile_scale, peak_safe_scale)
    would_clip = int(
        np.count_nonzero(
            (centered * percentile_scale > 32767.0)
            | (centered * percentile_scale < -32768.0)
        )
    )
    scaled = centered * scale
    pcm = np.rint(scaled).astype(np.int16)
    peak_pcm = int(np.max(np.abs(pcm.astype(np.int32)))) if len(pcm) else 0
    return pcm, {
        "center_removed": center,
        "normalization_reference": percentile_reference,
        "absolute_peak_reference": peak_reference,
        "percentile_scale": float(percentile_scale),
        "peak_safe_scale": float(peak_safe_scale),
        "scale": float(scale),
        "limiter_applied": bool(peak_safe_scale < percentile_scale),
        "would_clip_without_limiter": would_clip,
        "clipped_samples": 0,
        "peak_pcm": peak_pcm,
    }


def process_complex_chunks(
    chunks: Iterable[NDArray[np.complex64]],
    *,
    input_sample_rate_hz: int,
    frequency_offset_hz: float,
    settings: ExtractionSettings,
) -> tuple[NDArray[np.float32], dict[str, object], NDArray[np.complex64]]:
    intermediate_rate = settings.validate(input_sample_rate_hz)
    filters = design_filters(input_sample_rate_hz, settings)
    mixer = StreamingMixer(frequency_offset_hz, input_sample_rate_hz)
    stage1 = StreamingFIRDecimator(
        filters["first"], settings.first_decimation
    )
    stage2 = StreamingFIRDecimator(
        filters["second"], settings.second_decimation
    )
    channel_filter = StreamingComplexFilter(filters["channel"])
    discriminator = StreamingFMDiscriminator()
    discriminator_parts: list[NDArray[np.float32]] = []
    preview_parts: list[NDArray[np.complex64]] = []
    preview_count = 0
    input_count = 0
    intermediate_count = 0

    for chunk in chunks:
        values = np.asarray(chunk, dtype=np.complex64)
        input_count += len(values)
        mixed = mixer.process(values)
        reduced = stage1.process(mixed)
        reduced = stage2.process(reduced)
        filtered = channel_filter.process(reduced)
        intermediate_count += len(filtered)
        if preview_count < settings.preview_samples:
            remaining = settings.preview_samples - preview_count
            preview = filtered[:remaining].copy()
            preview_parts.append(preview)
            preview_count += len(preview)
        audio = discriminator.process(filtered)
        if len(audio):
            discriminator_parts.append(audio)

    discriminator_100k = (
        np.concatenate(discriminator_parts)
        if discriminator_parts
        else np.empty(0, dtype=np.float32)
    )
    trim = int(round(settings.trim_seconds * intermediate_rate))
    if trim > 0 and len(discriminator_100k) > 2 * trim:
        discriminator_100k = discriminator_100k[trim:-trim]
    output = resample_discriminator(
        discriminator_100k,
        intermediate_rate,
        settings.output_rate_hz,
    )
    preview = (
        np.concatenate(preview_parts)
        if preview_parts
        else np.empty(0, dtype=np.complex64)
    )
    metrics: dict[str, object] = {
        "input_sample_count": input_count,
        "intermediate_sample_count": intermediate_count,
        "discriminator_sample_count_before_resample": len(
            discriminator_100k
        ),
        "output_sample_count": len(output),
        "intermediate_rate_hz": intermediate_rate,
        "output_rate_hz": settings.output_rate_hz,
        "frequency_offset_hz": float(frequency_offset_hz),
        "trim_samples_each_side_intermediate": trim,
        "filter_taps": {
            "first": len(filters["first"]),
            "second": len(filters["second"]),
            "channel": len(filters["channel"]),
        },
    }
    return output, metrics, preview
