from __future__ import annotations

from dataclasses import replace

from dmr_iq_surveyor.decode.core import ExtractionSettings


_PROFILE_RATES = {
    "10m": 10_000_000,
    "5m": 5_000_000,
    "500k": 500_000,
    "250k": 250_000,
    "62k5": 62_500,
}


def available_profiles() -> dict[str, int]:
    return dict(_PROFILE_RATES)


def extraction_profile(
    name: str,
    input_sample_rate_hz: int,
    *,
    chunk_frames: int | None = None,
) -> ExtractionSettings:
    normalized = name.strip().lower()
    if normalized == "auto":
        matches = [
            profile
            for profile, rate in _PROFILE_RATES.items()
            if rate == int(input_sample_rate_hz)
        ]
        if not matches:
            supported = ", ".join(
                f"{profile}={rate:,}" for profile, rate in _PROFILE_RATES.items()
            )
            raise ValueError(
                f"No automatic extraction profile for {input_sample_rate_hz:,} S/s; "
                f"supported profiles: {supported}"
            )
        normalized = matches[0]
    if normalized not in _PROFILE_RATES:
        choices = ", ".join(["auto", *_PROFILE_RATES])
        raise ValueError(
            f"Unknown extraction profile {name!r}; choose one of: {choices}"
        )
    expected = _PROFILE_RATES[normalized]
    if int(input_sample_rate_hz) != expected:
        raise ValueError(
            f"Extraction profile {normalized} requires {expected:,} S/s, "
            f"but the recording is {input_sample_rate_hz:,} S/s"
        )

    if normalized == "10m":
        settings = ExtractionSettings()
    elif normalized == "5m":
        settings = ExtractionSettings(
            chunk_frames=1_000_000,
            first_decimation=10,
            second_decimation=5,
            first_filter_taps=401,
            second_filter_taps=401,
            channel_filter_taps=161,
            first_cutoff_hz=200_000.0,
            second_cutoff_hz=40_000.0,
            channel_lowpass_hz=7_500.0,
        )
    elif normalized == "500k":
        settings = ExtractionSettings(
            chunk_frames=500_000,
            first_decimation=5,
            second_decimation=1,
            first_filter_taps=401,
            second_filter_taps=161,
            channel_filter_taps=161,
            first_cutoff_hz=40_000.0,
            second_cutoff_hz=40_000.0,
            channel_lowpass_hz=7_500.0,
        )
    elif normalized == "250k":
        settings = ExtractionSettings(
            chunk_frames=250_000,
            first_decimation=5,
            second_decimation=1,
            first_filter_taps=401,
            second_filter_taps=161,
            channel_filter_taps=161,
            first_cutoff_hz=20_000.0,
            second_cutoff_hz=20_000.0,
            channel_lowpass_hz=7_500.0,
        )
    else:
        settings = ExtractionSettings(
            chunk_frames=62_500,
            first_decimation=1,
            second_decimation=1,
            first_filter_taps=161,
            second_filter_taps=161,
            channel_filter_taps=161,
            first_cutoff_hz=20_000.0,
            second_cutoff_hz=20_000.0,
            channel_lowpass_hz=7_500.0,
        )
    if chunk_frames is not None:
        settings = replace(settings, chunk_frames=int(chunk_frames))
    settings.validate(int(input_sample_rate_hz))
    return settings


__all__ = ["available_profiles", "extraction_profile"]
