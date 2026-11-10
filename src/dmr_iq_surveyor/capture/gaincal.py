"""Choose a fixed manual gain by measuring, not by trial and error.

Geolocation compares received levels between places, so the gain is set once
and never changed for a campaign. Two failures sit either side of a good
setting, and both bias the solver in the same direction -- "further away":

- **too much gain** clips or compresses the strongest signal, which then
  reads lower than it is. Near the transmitter, where the level matters most,
  that is exactly where it happens.
- **too little gain** lets the ADC's own noise, rather than the antenna's,
  set the noise floor. A weak carrier then falls under the detection gate
  sooner, and a non-detection is evidence that pushes a site away.

So the right setting is the *least* sensitive one that still gives the best
signal-to-noise ratio: everything below it costs sensitivity, everything above
it costs headroom and buys nothing. This module finds it by stepping the
receiver through a ladder of (LNA state, IF gain reduction) settings, holding
each for a fraction of a second, and measuring at each step:

- the ADC peak and the fraction of samples at full scale (clipping),
- the noise floor (median power spectral density across the capture),
- the in-channel SNR of each target frequency, when there is one.

Two selection rules, chosen by what the measurement can support:

`target_snr_plateau` -- a target carrier is heard clearly. Take the best SNR
any unclipped setting reached, keep every setting within `plateau_db` of it,
and choose the one with the most headroom. This compares SNR, a ratio, so it
needs no table of what each LNA state is worth in dB.

`noise_floor_knee` -- no target is heard here. Within one LNA state only, IF
gain reduction is an exact dB control: if the noise floor is set by the
antenna and front end, raising the reduction by D dB lowers the floor by D dB.
Where it lowers it by less, the ADC's noise has started to show. The most
sensitive unclipped setting is the reference; the chosen one is the largest
reduction whose floor stays within `degradation_db` of that 1:1 prediction.
This rule cannot compare LNA states, so it only ever chooses the IF reduction
at the LNA state the operator already uses, and says so.

The result is a *recommendation with its evidence*. Whether it is applied is
the caller's decision (the field service refuses to change gain inside a
campaign that already has measurements at another gain).

Memory is bounded by one dwell (`dwell_seconds * sample_rate` complex
samples), never by how many steps the ladder has.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from dmr_iq_surveyor.capture.device import DeviceSettings, IqDevice

METHOD_TARGET_SNR_PLATEAU = "target_snr_plateau"
METHOD_NOISE_FLOOR_KNEE = "noise_floor_knee"

STATUS_OK = "ok"
STATUS_ALL_CLIPPED = "all_clipped"
STATUS_NO_USABLE_STEP = "no_usable_step"

TARGET_HEARD = "heard"
TARGET_NOT_OBSERVED = "not_observed"
TARGET_NONE_GIVEN = "not_configured"

# CF32 samples from SoapySDRPlay are normalised to +/-1.0 full scale. A
# component at or above this is treated as clipped: real converters compress
# a little before the rail, and a measurement that is merely close to it is
# already one a stronger signal would push over.
CLIP_LEVEL = 0.98

DEFAULT_LNA_STATES: tuple[int, ...] = (0, 2, 4, 6, 8)
DEFAULT_IF_REDUCTIONS: tuple[float, ...] = (20.0, 26.0, 32.0, 38.0, 44.0, 50.0, 56.0, 59.0)


class CalibrationCancelled(Exception):
    """The operator stopped the calibration part-way."""


@dataclass(frozen=True, slots=True)
class GainCandidate:
    lna_state: int
    if_gain_reduction_db: float


@dataclass(slots=True)
class StepMeasurement:
    lna_state: int
    if_gain_reduction_db: float
    peak_dbfs: float | None = None
    clip_fraction: float | None = None
    noise_floor_db: float | None = None
    # Per target, keyed by the frequency in Hz as a string (JSON keys).
    target_snr_db: dict[str, float] = field(default_factory=dict)
    samples: int = 0
    error: str = ""

    @property
    def headroom_db(self) -> float | None:
        return None if self.peak_dbfs is None else -self.peak_dbfs

    @property
    def usable(self) -> bool:
        return (
            not self.error
            and self.peak_dbfs is not None
            and self.clip_fraction is not None
            and self.clip_fraction == 0.0
        )

    def best_target_snr_db(self) -> float | None:
        return max(self.target_snr_db.values()) if self.target_snr_db else None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["headroom_db"] = self.headroom_db
        payload["usable"] = self.usable
        return payload


@dataclass(slots=True)
class CalibrationSettings:
    lna_states: Sequence[int] = DEFAULT_LNA_STATES
    if_gain_reductions_db: Sequence[float] = DEFAULT_IF_REDUCTIONS
    # Discarded after every gain change, so the AGC-free front end and the
    # driver's buffers have settled on the new setting.
    settle_seconds: float = 0.3
    dwell_seconds: float = 0.5
    fft_size: int = 16_384
    channel_width_hz: float = 12_500.0
    # A target counts as heard when its best in-channel SNR reaches this.
    min_target_snr_db: float = 10.0
    plateau_db: float = 1.0
    degradation_db: float = 1.0
    # Below this, the report warns that a carrier this much stronger -- for
    # example the same one a few kilometres closer -- will clip.
    advisory_headroom_db: float = 30.0

    def validate(self) -> None:
        if not self.lna_states or not self.if_gain_reductions_db:
            raise ValueError("the gain ladder must have at least one LNA state and one IF value")
        if self.dwell_seconds <= 0 or self.settle_seconds < 0:
            raise ValueError("dwell_seconds must be positive and settle_seconds non-negative")
        if self.fft_size < 256:
            raise ValueError("fft_size must be at least 256")

    def ladder(self, current_lna_state: int | None = None) -> list[GainCandidate]:
        """Most sensitive first within each LNA state.

        The operator's current LNA state is always included, because the
        no-target rule can only ever choose within it.
        """
        states = list(dict.fromkeys(int(s) for s in self.lna_states))
        if current_lna_state is not None and int(current_lna_state) not in states:
            states.append(int(current_lna_state))
        reductions = sorted({float(v) for v in self.if_gain_reductions_db})
        return [GainCandidate(state, value) for state in sorted(states) for value in reductions]


@dataclass(slots=True)
class CalibrationResult:
    status: str
    method: str | None
    recommended: GainCandidate | None
    target_status: str
    reason: str
    warnings: list[str]
    steps: list[StepMeasurement]
    targets_hz: list[float]
    center_frequency_hz: float
    sample_rate_hz: float
    settings: CalibrationSettings
    started_utc: str
    finished_utc: str

    def to_dict(self) -> dict[str, Any]:
        chosen = None
        if self.recommended is not None:
            chosen = next(
                (
                    step.to_dict()
                    for step in self.steps
                    if step.lna_state == self.recommended.lna_state
                    and step.if_gain_reduction_db == self.recommended.if_gain_reduction_db
                ),
                None,
            )
        return {
            "status": self.status,
            "method": self.method,
            "recommended": asdict(self.recommended) if self.recommended else None,
            "recommended_step": chosen,
            "target_status": self.target_status,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "targets_hz": list(self.targets_hz),
            "center_frequency_hz": self.center_frequency_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "settings": {
                key: (list(value) if isinstance(value, (tuple, list)) else value)
                for key, value in asdict(self.settings).items()
            },
            "steps": [step.to_dict() for step in self.steps],
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
        }


# -- measurement -----------------------------------------------------------


def measure_block(
    samples: np.ndarray,
    *,
    sample_rate_hz: float,
    center_frequency_hz: float,
    targets_hz: Sequence[float],
    fft_size: int,
    channel_width_hz: float,
) -> dict[str, Any]:
    """Peak, clipping, noise floor and per-target SNR of one dwell."""
    block = np.asarray(samples, dtype=np.complex64)
    if block.size < fft_size:
        raise ValueError(f"dwell holds {block.size} samples, fewer than one {fft_size}-point FFT")
    components = block.view(np.float32)
    magnitude = np.abs(components)
    peak = float(magnitude.max())
    clipped = int(np.count_nonzero(magnitude >= CLIP_LEVEL))

    frames = block.size // fft_size
    window = np.hanning(fft_size).astype(np.float32)
    scale = float(np.sum(window**2))
    psd = np.zeros(fft_size, dtype=np.float64)
    for index in range(frames):
        frame = block[index * fft_size : (index + 1) * fft_size] * window
        spectrum = np.fft.fft(frame)
        psd += (spectrum.real**2 + spectrum.imag**2) / scale
    psd = np.fft.fftshift(psd / frames)
    bin_hz = sample_rate_hz / fft_size
    offsets = (np.arange(fft_size) - fft_size // 2) * bin_hz

    # The outer 10% each side is the anti-alias filter's roll-off, not the
    # receiver's noise floor; leaving it in would pull the median down.
    inner = np.abs(offsets) <= 0.4 * sample_rate_hz
    floor = float(np.median(psd[inner]))
    floor_db = 10.0 * math.log10(floor) if floor > 0 else float("-inf")

    snrs: dict[str, float] = {}
    for target in targets_hz:
        offset = float(target) - center_frequency_hz
        in_channel = np.abs(offsets - offset) <= channel_width_hz / 2.0
        if not np.any(in_channel) or abs(offset) > 0.4 * sample_rate_hz:
            continue
        channel_mean = float(np.mean(psd[in_channel]))
        if floor > 0 and channel_mean > 0:
            snrs[str(round(float(target)))] = round(
                10.0 * math.log10(channel_mean / floor), 2
            )

    return {
        "peak_dbfs": round(20.0 * math.log10(peak), 2) if peak > 0 else float("-inf"),
        "clip_fraction": clipped / float(components.size),
        "noise_floor_db": round(floor_db, 2),
        "target_snr_db": snrs,
        "samples": int(block.size),
    }


def _read_exact(
    device: IqDevice, frames: int, buffer: np.ndarray | None, cancelled: Callable[[], bool]
) -> np.ndarray:
    """Read `frames` samples into `buffer` (or discard them if it is None)."""
    filled = 0
    empty_reads = 0
    while filled < frames:
        if cancelled():
            raise CalibrationCancelled()
        chunk = np.asarray(device.read_stream_chunk(frames - filled))
        if chunk.size == 0:
            empty_reads += 1
            if empty_reads > 200:
                raise RuntimeError("the receiver delivered no samples during calibration")
            continue
        take = min(chunk.size, frames - filled)
        if buffer is not None:
            buffer[filled : filled + take] = chunk[:take]
        filled += take
    return buffer[:frames] if buffer is not None else np.empty(0, dtype=np.complex64)


def run_calibration(
    device: IqDevice,
    base: DeviceSettings,
    *,
    targets_hz: Sequence[float] = (),
    current_lna_state: int | None = None,
    settings: CalibrationSettings | None = None,
    progress: Callable[[int, int, StepMeasurement], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> CalibrationResult:
    """Step the receiver through the ladder and choose a setting.

    The device is opened once at the first step and retuned in place when it
    offers `set_gain()`; a device that does not is closed and reopened at each
    step, which is slower and otherwise identical. It is always closed on the
    way out, cancelled or not.
    """
    resolved = settings or CalibrationSettings()
    resolved.validate()
    ladder = resolved.ladder(current_lna_state)
    rate = float(base.sample_rate_hz)
    settle_frames = round(resolved.settle_seconds * rate)
    dwell_frames = max(resolved.fft_size, round(resolved.dwell_seconds * rate))
    buffer = np.empty(dwell_frames, dtype=np.complex64)
    started = datetime.now(UTC).isoformat()
    steps: list[StepMeasurement] = []
    opened = False

    def configured(candidate: GainCandidate) -> DeviceSettings:
        return DeviceSettings(
            driver=base.driver,
            sample_rate_hz=base.sample_rate_hz,
            center_frequency_hz=base.center_frequency_hz,
            if_gain_reduction_db=candidate.if_gain_reduction_db,
            lna_state=candidate.lna_state,
            agc=False,
            antenna=base.antenna,
            channel=base.channel,
            bandwidth_hz=base.bandwidth_hz,
            serial=base.serial,
        )

    try:
        for index, candidate in enumerate(ladder):
            if cancelled():
                raise CalibrationCancelled()
            step = StepMeasurement(candidate.lna_state, candidate.if_gain_reduction_db)
            try:
                retune = getattr(device, "set_gain", None)
                if opened and callable(retune):
                    retune(candidate.if_gain_reduction_db, candidate.lna_state)
                else:
                    if opened:
                        device.close()
                        opened = False
                    device.open(configured(candidate))
                    opened = True
                _read_exact(device, settle_frames, None, cancelled)
                block = _read_exact(device, dwell_frames, buffer, cancelled)
                measured = measure_block(
                    block,
                    sample_rate_hz=rate,
                    center_frequency_hz=float(base.center_frequency_hz),
                    targets_hz=targets_hz,
                    fft_size=resolved.fft_size,
                    channel_width_hz=resolved.channel_width_hz,
                )
                step.peak_dbfs = measured["peak_dbfs"]
                step.clip_fraction = measured["clip_fraction"]
                step.noise_floor_db = measured["noise_floor_db"]
                step.target_snr_db = measured["target_snr_db"]
                step.samples = measured["samples"]
            except CalibrationCancelled:
                raise
            except ValueError as exc:
                # An out-of-range gain element on this model: recorded, skipped.
                step.error = str(exc)
            steps.append(step)
            if progress is not None:
                progress(index + 1, len(ladder), step)
    finally:
        if opened:
            device.close()

    status, method, chosen, target_status, reason, warnings = choose_setting(
        steps,
        targets_given=bool(targets_hz),
        current_lna_state=current_lna_state,
        settings=resolved,
    )
    return CalibrationResult(
        status=status,
        method=method,
        recommended=chosen,
        target_status=target_status,
        reason=reason,
        warnings=warnings,
        steps=steps,
        targets_hz=[float(value) for value in targets_hz],
        center_frequency_hz=float(base.center_frequency_hz),
        sample_rate_hz=rate,
        settings=resolved,
        started_utc=started,
        finished_utc=datetime.now(UTC).isoformat(),
    )


# -- selection -------------------------------------------------------------


def choose_setting(
    steps: Sequence[StepMeasurement],
    *,
    targets_given: bool,
    current_lna_state: int | None,
    settings: CalibrationSettings,
) -> tuple[str, str | None, GainCandidate | None, str, str, list[str]]:
    """(status, method, recommended, target_status, reason, warnings)."""
    warnings: list[str] = []
    measured = [step for step in steps if not step.error and step.peak_dbfs is not None]
    if not measured:
        return (
            STATUS_NO_USABLE_STEP, None, None,
            TARGET_NONE_GIVEN if not targets_given else TARGET_NOT_OBSERVED,
            "no gain setting could be measured on this receiver",
            [step.error for step in steps if step.error][:3],
        )
    usable = [step for step in measured if step.usable]
    if not usable:
        least = max(measured, key=lambda s: (s.headroom_db or -1e9))
        return (
            STATUS_ALL_CLIPPED, None,
            GainCandidate(least.lna_state, least.if_gain_reduction_db),
            TARGET_NOT_OBSERVED if targets_given else TARGET_NONE_GIVEN,
            (
                "every setting tried clipped: a very strong signal is present here. Move away "
                "from it (or add attenuation) and calibrate again; the least sensitive setting "
                "is shown but should not be used as is"
            ),
            warnings,
        )

    best_snr = max(
        (snr for step in usable if (snr := step.best_target_snr_db()) is not None),
        default=None,
    )
    if best_snr is not None and best_snr >= settings.min_target_snr_db:
        plateau = [
            step for step in usable
            if (step.best_target_snr_db() or -1e9) >= best_snr - settings.plateau_db
        ]
        chosen = max(
            plateau,
            key=lambda s: (s.headroom_db or -1e9, s.if_gain_reduction_db, -s.lna_state),
        )
        reason = (
            f"the target was heard at up to {best_snr:.1f} dB SNR; this is the least sensitive "
            f"setting within {settings.plateau_db:g} dB of that, which leaves "
            f"{chosen.headroom_db:.1f} dB of headroom"
        )
        method = METHOD_TARGET_SNR_PLATEAU
        target_status = TARGET_HEARD
    else:
        target_status = TARGET_NOT_OBSERVED if targets_given else TARGET_NONE_GIVEN
        states = sorted({step.lna_state for step in usable})
        lna = current_lna_state if current_lna_state in states else states[len(states) // 2]
        if current_lna_state is not None and lna != current_lna_state:
            warnings.append(
                f"LNA state {current_lna_state} had no usable step; chose within LNA state {lna}"
            )
        rows = sorted(
            (step for step in usable if step.lna_state == lna),
            key=lambda s: s.if_gain_reduction_db,
        )
        reference = rows[0]
        chosen = reference
        for step in rows[1:]:
            predicted = (reference.noise_floor_db or 0.0) - (
                step.if_gain_reduction_db - reference.if_gain_reduction_db
            )
            degradation = (step.noise_floor_db or 0.0) - predicted
            if degradation <= settings.degradation_db:
                chosen = step
            else:
                break
        reason = (
            ("no target frequency was given" if not targets_given else
             "the target was not heard clearly here")
            + f"; at LNA state {lna}, IF gain reduction {chosen.if_gain_reduction_db:g} dB is the "
            f"least sensitive setting whose noise floor still follows the gain within "
            f"{settings.degradation_db:g} dB (headroom {chosen.headroom_db:.1f} dB). The LNA "
            "state itself was not chosen: without a signal there is nothing to compare it by"
        )
        method = METHOD_NOISE_FLOOR_KNEE
        if targets_given:
            warnings.append(
                "the target was not heard clearly here, so the setting is based on the noise "
                "floor only; calibrate again where the target is audible if you can"
            )

    if chosen.headroom_db is not None and chosen.headroom_db < settings.advisory_headroom_db:
        warnings.append(
            f"only {chosen.headroom_db:.1f} dB of headroom: a signal that much stronger than the "
            "strongest one here (e.g. the target a few km closer) will clip. Calibrate nearer "
            "the strongest place you expect to drive through"
        )
    return (
        STATUS_OK, method, GainCandidate(chosen.lna_state, chosen.if_gain_reduction_db),
        target_status, reason, warnings,
    )


__all__ = [
    "CLIP_LEVEL",
    "CalibrationCancelled",
    "CalibrationResult",
    "CalibrationSettings",
    "GainCandidate",
    "METHOD_NOISE_FLOOR_KNEE",
    "METHOD_TARGET_SNR_PLATEAU",
    "STATUS_ALL_CLIPPED",
    "STATUS_NO_USABLE_STEP",
    "STATUS_OK",
    "StepMeasurement",
    "choose_setting",
    "measure_block",
    "run_calibration",
]
