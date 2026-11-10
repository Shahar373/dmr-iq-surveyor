"""A simulated SDR front end for gain-calibration tests.

Three properties and nothing else: front-end noise that follows the gain, an
ADC with its own fixed noise floor, and a hard rail at full scale. None of the
numbers is an SDRplay's; they only have to put the knee and the clipping point
somewhere a gain ladder can find them.
"""

from __future__ import annotations

import math

import numpy as np

from dmr_iq_surveyor.capture.device import DeviceSettings

RATE = 1_000_000.0
CENTER = 469_250_000.0
TARGET = 469_350_000.0


class SimulatedReceiver:
    """Gain in dB = 80 - 6*LNA - IFGR; ADC noise fixed; clipping at 1.0."""

    def __init__(
        self,
        *,
        signal_db: float | None = -75.0,
        noise_db: float = -95.0,
        adc_noise_db: float = -72.0,
        seed: int = 1,
    ) -> None:
        self.signal_db = signal_db
        self.noise_db = noise_db
        self.adc_noise_db = adc_noise_db
        self.rng = np.random.default_rng(seed)
        self.gain_db = 0.0
        self.noise_figure_db = 0.0
        self.phase = 0.0
        self.opens = 0
        self.closed = True
        self.retunes = 0

    def _apply(self, ifgr: float, lna: int) -> None:
        if not 20 <= ifgr <= 59 or not 0 <= lna <= 9:
            raise ValueError("out of range")
        self.gain_db = 80.0 - 6.0 * lna - ifgr
        # More LNA reduction, worse noise figure: why the LNA state matters.
        self.noise_figure_db = 2.0 * lna

    def open(self, settings: DeviceSettings) -> None:
        self._apply(float(settings.if_gain_reduction_db), int(settings.lna_state))
        self.opens += 1
        self.closed = False

    def set_gain(self, ifgr: float, lna: int) -> None:
        self._apply(ifgr, lna)
        self.retunes += 1

    def read_stream_chunk(self, max_frames: int) -> np.ndarray:
        frames = min(max_frames, 65_536)
        gain = 10 ** (self.gain_db / 10)
        noise_power = 10 ** ((self.noise_db + self.noise_figure_db) / 10) * gain
        adc_power = 10 ** (self.adc_noise_db / 10)
        sigma = math.sqrt((noise_power + adc_power) / 2)
        out = self.rng.normal(scale=sigma, size=frames) + 1j * self.rng.normal(
            scale=sigma, size=frames
        )
        if self.signal_db is not None:
            amplitude = math.sqrt(10 ** (self.signal_db / 10) * gain)
            step = 2 * math.pi * (TARGET - CENTER) / RATE
            phases = self.phase + step * np.arange(frames)
            self.phase = float((phases[-1] + step) % (2 * math.pi))
            out = out + amplitude * np.exp(1j * phases)
        out = np.clip(out.real, -1, 1) + 1j * np.clip(out.imag, -1, 1)
        return out.astype(np.complex64)

    def close(self) -> None:
        self.closed = True
