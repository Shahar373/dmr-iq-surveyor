"""Gain calibration against a simulated receiver chain.

The simulated radio (`fixtures/simulated_receiver.py`) has the three
properties the selection rules are built on: front-end noise that follows the
gain, an ADC with its own fixed noise that shows once the gain is low enough,
and a hard rail at full scale.
"""

from __future__ import annotations

import pytest
from fixtures.simulated_receiver import CENTER, RATE, TARGET, SimulatedReceiver

from dmr_iq_surveyor.capture.device import DeviceSettings
from dmr_iq_surveyor.capture.gaincal import (
    METHOD_NOISE_FLOOR_KNEE,
    METHOD_TARGET_SNR_PLATEAU,
    STATUS_ALL_CLIPPED,
    STATUS_OK,
    CalibrationCancelled,
    CalibrationSettings,
    measure_block,
    run_calibration,
)


def _base() -> DeviceSettings:
    return DeviceSettings(sample_rate_hz=RATE, center_frequency_hz=CENTER, if_gain_reduction_db=40)


def _settings(**overrides) -> CalibrationSettings:
    defaults = {
        "lna_states": (0, 4, 8),
        "if_gain_reductions_db": (20, 26, 32, 38, 44, 50, 56, 59),
        "settle_seconds": 0.01,
        "dwell_seconds": 0.1,
        "fft_size": 4096,
    }
    defaults.update(overrides)
    return CalibrationSettings(**defaults)


def test_measure_block_reports_clipping_and_target_snr() -> None:
    receiver = SimulatedReceiver(signal_db=-75.0)
    receiver.open(DeviceSettings(sample_rate_hz=RATE, center_frequency_hz=CENTER,
                                 if_gain_reduction_db=40, lna_state=0))
    block = receiver.read_stream_chunk(65_536)
    measured = measure_block(block, sample_rate_hz=RATE, center_frequency_hz=CENTER,
                             targets_hz=[TARGET], fft_size=4096, channel_width_hz=12_500)
    assert measured["clip_fraction"] == 0.0
    assert measured["target_snr_db"][str(int(TARGET))] > 20.0
    assert measured["peak_dbfs"] < 0.0


def test_heard_target_picks_the_least_sensitive_setting_on_the_snr_plateau() -> None:
    receiver = SimulatedReceiver(signal_db=-75.0)
    seen: list[int] = []
    result = run_calibration(
        receiver, _base(), targets_hz=[TARGET], current_lna_state=4,
        settings=_settings(), progress=lambda done, total, step: seen.append(done),
    )
    assert result.status == STATUS_OK
    assert result.method == METHOD_TARGET_SNR_PLATEAU
    assert seen == list(range(1, 25)), "progress must be reported once per step"
    assert receiver.opens == 1 and receiver.retunes == 23, "retuned in place, not reopened"
    assert receiver.closed

    chosen = result.recommended
    usable = [s for s in result.steps if s.usable]
    best = max(s.best_target_snr_db() for s in usable)
    step = next(s for s in result.steps
                if (s.lna_state, s.if_gain_reduction_db)
                == (chosen.lna_state, chosen.if_gain_reduction_db))
    assert step.usable
    assert step.best_target_snr_db() >= best - 1.0
    # More headroom than every other setting on the plateau.
    plateau = [s for s in usable if s.best_target_snr_db() >= best - 1.0]
    assert step.headroom_db == max(s.headroom_db for s in plateau)
    # And not simply the most sensitive setting tried.
    assert (chosen.lna_state, chosen.if_gain_reduction_db) != (0, 20.0)


def test_clipping_settings_are_never_chosen() -> None:
    # Strong enough that the most sensitive settings clip.
    receiver = SimulatedReceiver(signal_db=-60.0)
    result = run_calibration(receiver, _base(), targets_hz=[TARGET], current_lna_state=0,
                             settings=_settings())
    assert any(not s.usable for s in result.steps), "the fixture must actually clip somewhere"
    chosen = result.recommended
    step = next(s for s in result.steps
                if (s.lna_state, s.if_gain_reduction_db)
                == (chosen.lna_state, chosen.if_gain_reduction_db))
    assert step.usable and step.clip_fraction == 0.0


def test_no_target_uses_the_noise_floor_knee_at_the_current_lna_state() -> None:
    receiver = SimulatedReceiver(signal_db=None)
    result = run_calibration(receiver, _base(), targets_hz=[TARGET], current_lna_state=4,
                             settings=_settings())
    assert result.status == STATUS_OK
    assert result.method == METHOD_NOISE_FLOOR_KNEE
    assert result.target_status == "not_observed"
    assert result.recommended.lna_state == 4
    # Gain at LNA 4 is 56 - IFGR; front-end noise at the ADC is -87 + 56 - IFGR
    # against an ADC floor of -72, so the floor stops following the gain once
    # IFGR passes roughly 59 - ... i.e. well before the bottom of the ladder.
    assert 20.0 < result.recommended.if_gain_reduction_db < 59.0
    assert any("noise floor only" in warning for warning in result.warnings)


def test_everything_clipping_is_reported_not_recommended_silently() -> None:
    receiver = SimulatedReceiver(signal_db=-20.0)
    result = run_calibration(receiver, _base(), targets_hz=[TARGET], current_lna_state=0,
                             settings=_settings(lna_states=(0,), if_gain_reductions_db=(20, 30)))
    assert result.status == STATUS_ALL_CLIPPED
    assert "clipped" in result.reason


def test_out_of_range_steps_are_recorded_and_skipped() -> None:
    receiver = SimulatedReceiver(signal_db=-75.0)
    result = run_calibration(receiver, _base(), targets_hz=[TARGET], current_lna_state=0,
                             settings=_settings(lna_states=(0, 12)))
    bad = [s for s in result.steps if s.lna_state == 12]
    assert bad and all(s.error for s in bad)
    assert result.status == STATUS_OK and result.recommended.lna_state == 0


def test_cancel_stops_and_releases_the_receiver() -> None:
    receiver = SimulatedReceiver()
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 5

    with pytest.raises(CalibrationCancelled):
        run_calibration(receiver, _base(), targets_hz=[TARGET], settings=_settings(),
                        cancelled=cancelled)
    assert receiver.closed
