from __future__ import annotations

from typing import Any

from dmr_iq_surveyor.decode.core import ExtractionSettings
from dmr_iq_surveyor.decode.dsd import (
    DecoderSettings,
    parse_dsd_fme_log,
    run_decoder_profiles,
)


def run_channel_extraction(
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    from dmr_iq_surveyor.decode.extract import (
        run_channel_extraction as implementation,
    )

    return implementation(*args, **kwargs)


def run_decode_batch(
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    from dmr_iq_surveyor.decode.batch import (
        run_decode_batch as implementation,
    )

    return implementation(*args, **kwargs)


__all__ = [
    "DecoderSettings",
    "ExtractionSettings",
    "parse_dsd_fme_log",
    "run_channel_extraction",
    "run_decode_batch",
    "run_decoder_profiles",
]
