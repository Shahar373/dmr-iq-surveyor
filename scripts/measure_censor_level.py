#!/usr/bin/env python3
"""Measure `excess_snr_db` near the detection gate through the real survey path.

Synthesises a carrier plus white noise at a sweep of true S/N values, reduces
it with `accumulate_segment_spectrum` (shared by live and offline survey) and
detects with `observations_from_segments`, both unchanged. For each level it
prints how often the channel was detected, the measured `excess_snr_db`, the
same excess without per-bin clipping, and the converted censor level that
`geo.measurements.censor_level_for` gives non-detections.

    python scripts/measure_censor_level.py                       # live window, quiet band
    python scripts/measure_censor_level.py --mode contiguous --fft 16384 --frames 151
    python scripts/measure_censor_level.py --busy-db 15          # crowded neighbours

Results are recorded in docs/phase8-fixed-emitter-geolocation.md (Stage 3.1).
The sample rate is reduced from the field's 5 MS/s, keeping the FFT bin width
(1.25 MS/s / 4096 = 5 MS/s / 16384), so a run takes seconds, not minutes.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
from pathlib import Path

import numpy as np

from dmr_iq_surveyor.detect.features import feature_at
from dmr_iq_surveyor.geo.measurements import LEVEL_METRIC_EXCESS_SNR, censor_level_for
from dmr_iq_surveyor.spectrum.core import SpectrumSettings
from dmr_iq_surveyor.survey.discovery import (
    accumulate_segment_spectrum,
    observations_from_segments,
)
from dmr_iq_surveyor.survey.profiles import load_band_profile

CENTER_HZ = 867_406_250.0
CHANNEL_OFFSET_HZ = 150_000.0


def flat_carrier(rng: np.random.Generator, count: int, sample_rate: float, width_hz: float):
    """Unit-power complex noise with a flat-topped, raised-cosine-edged spectrum
    about `width_hz` wide: a continuous digital carrier without spectral lines."""
    spectrum = rng.standard_normal(count) + 1j * rng.standard_normal(count)
    magnitude = np.abs(np.fft.fftfreq(count, 1.0 / sample_rate))
    flat, roll = width_hz * 0.4, width_hz * 0.2
    shape = np.where(
        magnitude <= flat,
        1.0,
        np.where(
            magnitude <= flat + roll,
            0.5 * (1.0 + np.cos(np.pi * (magnitude - flat) / roll)),
            0.0,
        ),
    )
    signal = np.fft.ifft(spectrum * shape)
    return signal / np.sqrt(np.mean(np.abs(signal) ** 2))


def c4fm_carrier(rng: np.random.Generator, count: int, sample_rate: float, width_hz: float):
    """A crude 4FSK carrier (4800 baud, 600 Hz per level). Its spectral lines
    sometimes trip the detector's spur rule; kept for comparison only."""
    del width_hz
    samples_per_symbol = sample_rate / 4800.0
    symbol_count = math.ceil(count / samples_per_symbol) + 4
    symbols = rng.choice([-3.0, -1.0, 1.0, 3.0], size=symbol_count)
    frequency = np.repeat(symbols, round(samples_per_symbol))[: count + 64]
    taps = np.exp(-0.5 * (np.arange(-24, 25) / (samples_per_symbol * 0.35)) ** 2)
    frequency = np.convolve(frequency, taps / taps.sum(), mode="same")[:count] * 600.0
    return np.exp(2j * np.pi * np.cumsum(frequency) / sample_rate)


CARRIERS = {"flat": flat_carrier, "c4fm": c4fm_carrier}


def make_frames(args, rng, true_snr_db: float, window_hz: float) -> list[np.ndarray]:
    """FFT-length frames. `true_snr_db` is the true mean excess PSD over the
    channel's integration window relative to the noise PSD, which is what
    `excess_snr_db` is meant to estimate. Noise power is 1."""
    carrier = CARRIERS[args.carrier]
    if args.mode == "spread":  # live: independent frames spread over a window
        hop, total = args.fft, args.frames * args.fft
    else:  # offline: contiguous, 50 % overlap
        hop = args.fft // 2
        total = args.fft + (args.frames - 1) * hop
    time = np.arange(total) / args.sample_rate
    noise = (rng.standard_normal(total) + 1j * rng.standard_normal(total)) / math.sqrt(2.0)

    def tone(snr_db: float, offset_hz: float) -> np.ndarray:
        power = 10.0 ** (snr_db / 10.0) * window_hz / args.sample_rate
        return (
            math.sqrt(power)
            * carrier(rng, total, args.sample_rate, args.width_hz)
            * np.exp(2j * np.pi * offset_hz * time)
        )

    samples = noise + tone(true_snr_db, CHANNEL_OFFSET_HZ)
    if args.busy_db is not None:
        # Carriers every 12.5 kHz across +-100 kHz, except the channel under
        # test and its immediate neighbours: they lift the median noise floor.
        for step in range(-8, 9):
            if abs(step) > 1:
                samples = samples + tone(args.busy_db, CHANNEL_OFFSET_HZ + step * 12_500.0)
    samples = samples.astype(np.complex64)
    return [samples[index * hop : index * hop + args.fft] for index in range(args.frames)]


def measure(args) -> tuple[float, list[dict]]:
    profile = load_band_profile(args.band)
    profile = dataclasses.replace(
        profile,
        start_frequency_hz=CENTER_HZ - args.sample_rate * 0.45,
        stop_frequency_hz=CENTER_HZ + args.sample_rate * 0.45,
    )
    detection = profile.detection_settings()
    spectrum_settings = SpectrumSettings(fft_size=args.fft, overlap_ratio=0.5)
    censor = censor_level_for(dataclasses.asdict(detection), LEVEL_METRIC_EXCESS_SNR)
    half_width = detection.integration_width_hz / 2.0 + detection.merge_tolerance_hz
    resolution = args.sample_rate / args.fft
    window_hz = (round(2.0 * half_width / resolution) + 1) * resolution
    target = CENTER_HZ + CHANNEL_OFFSET_HZ

    rng = np.random.default_rng(args.seed)
    rows = []
    for true_db in np.arange(args.low_db, args.high_db + 1e-9, args.step_db):
        for _ in range(args.trials):
            frames = make_frames(args, rng, float(true_db), window_hz)
            spectrum = accumulate_segment_spectrum(
                iter(frames),
                fft_count=len(frames),
                sample_rate_hz=args.sample_rate,
                center_frequency_hz=CENTER_HZ,
                nominal_low_hz=CENTER_HZ - args.sample_rate / 2.0,
                nominal_high_hz=CENTER_HZ + args.sample_rate / 2.0,
                settings=spectrum_settings,
            )
            data = spectrum.as_data()
            feature = feature_at(data, target, detection)
            inside = np.abs(data["frequency_hz"] - target) <= half_width
            average = 10.0 ** (data["average_db"][inside].astype(float) / 10.0)
            noise = 10.0 ** (data["noise_db"][inside].astype(float) / 10.0)
            unclipped = float(np.mean(average - noise) / np.mean(noise))
            found = observations_from_segments(
                [spectrum],
                band_profile=profile,
                center_frequency_hz=CENTER_HZ,
                sample_rate_hz=args.sample_rate,
                spectrum_settings=spectrum_settings,
            )["observations"]
            rows.append(
                {
                    "true_db": round(float(true_db), 3),
                    "average_snr_db": feature["average_snr_db"],
                    "p95_snr_db": feature["p95_snr_db"],
                    "excess_db": feature["average_excess_snr_db"],
                    "unclipped_excess_db": (
                        10.0 * math.log10(unclipped) if unclipped > 0.0 else -60.0
                    ),
                    "clipped_bin_fraction": float(np.mean(average < noise)),
                    "detected": any(
                        abs(item.measured_center_hz - target) <= 6250.0 for item in found
                    ),
                    "average_gate": (
                        feature["average_snr_db"] >= detection.min_average_channel_snr_db
                    ),
                    "p95_gate": feature["p95_snr_db"] >= detection.min_p95_channel_snr_db,
                }
            )
    return censor, rows


def report(censor: float, rows: list[dict]) -> None:
    print(f"converted censor level (excess_snr_db): {censor:.2f} dB")
    print(
        " true   n  P(det) P(avg) P(p95) | avg_snr p95_snr | excess unclipped  clip  clipped%"
    )
    by_level: dict[float, list[dict]] = {}
    for row in rows:
        by_level.setdefault(row["true_db"], []).append(row)
    for true_db, group in sorted(by_level.items()):

        def mean(key: str, group: list[dict] = group) -> float:
            return float(np.mean([row[key] for row in group]))

        print(
            f"{true_db:5.1f} {len(group):3d}  {mean('detected'):5.2f}  "
            f"{mean('average_gate'):5.2f}  {mean('p95_gate'):5.2f}  | "
            f"{mean('average_snr_db'):6.2f}  {mean('p95_snr_db'):6.2f}  | "
            f"{mean('excess_db'):6.2f}  {mean('unclipped_excess_db'):6.2f}  "
            f"{mean('excess_db') - mean('unclipped_excess_db'):6.3f}  "
            f"{100.0 * mean('clipped_bin_fraction'):5.1f}"
        )
    at_gate = [row for row in rows if abs(row["average_snr_db"] - 4.0) <= 0.25]
    if at_gate:
        print(
            f"at the (S+N)/N gate (4.0 +- 0.25 dB, {len(at_gate)} windows): excess "
            f"{np.mean([row['excess_db'] for row in at_gate]):.2f} dB, unclipped "
            f"{np.mean([row['unclipped_excess_db'] for row in at_gate]):.2f} dB, "
            f"censor level {censor:.2f} dB"
        )
    excess = np.array([row["excess_db"] for row in rows])
    detected = np.array([row["detected"] for row in rows], dtype=float)
    print("measured excess (0.5 dB bins) -> P(detected)")
    for low in np.arange(np.floor(excess.min()), np.ceil(excess.max()) + 0.5, 0.5):
        selected = (excess >= low) & (excess < low + 0.5)
        if selected.any():
            print(
                f"  [{low:5.1f}, {low + 0.5:5.1f})  n={int(selected.sum()):3d}  "
                f"P={detected[selected].mean():.2f}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--band", default="config/bands/central_800_narrow.yaml")
    parser.add_argument(
        "--mode", choices=["spread", "contiguous"], default="spread",
        help="spread: live drive window (independent frames); contiguous: offline segment",
    )
    parser.add_argument("--sample-rate", type=float, default=1_250_000.0)
    parser.add_argument("--fft", type=int, default=4096)
    parser.add_argument("--frames", type=int, default=24, help="FFT frames per window")
    parser.add_argument("--carrier", choices=sorted(CARRIERS), default="flat")
    parser.add_argument("--width-hz", type=float, default=8000.0)
    parser.add_argument("--busy-db", type=float, default=None, help="neighbour carriers at this S/N")
    parser.add_argument("--low-db", type=float, default=-3.0)
    parser.add_argument("--high-db", type=float, default=5.0)
    parser.add_argument("--step-db", type=float, default=0.5)
    parser.add_argument("--trials", type=int, default=30)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path, help="write every window as JSON")
    args = parser.parse_args()
    censor, rows = measure(args)
    report(censor, rows)
    if args.output is not None:
        args.output.write_text(json.dumps({"args": vars(args) | {"output": str(args.output)},
                                           "censor_level_db": censor, "rows": rows}))


if __name__ == "__main__":
    main()
