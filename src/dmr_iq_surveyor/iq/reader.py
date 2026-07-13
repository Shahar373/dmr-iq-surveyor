from __future__ import annotations

from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from dmr_iq_surveyor.models import RecordingInfo


class UnsupportedSampleFormatError(ValueError):
    pass


def numpy_dtype_for_recording(info: RecordingInfo) -> np.dtype:
    fmt = info.fmt
    if fmt.effective_format_code == 1:
        mapping = {
            8: np.dtype("u1"),
            16: np.dtype("<i2"),
            32: np.dtype("<i4"),
        }
    elif fmt.effective_format_code == 3:
        mapping = {
            32: np.dtype("<f4"),
            64: np.dtype("<f8"),
        }
    else:
        raise UnsupportedSampleFormatError(
            f"Unsupported WAVE format code {fmt.effective_format_code}"
        )
    try:
        return mapping[fmt.bits_per_sample]
    except KeyError as exc:
        raise UnsupportedSampleFormatError(
            f"Unsupported {fmt.bits_per_sample}-bit sample format; 24-bit packed PCM is not supported yet"
        ) from exc


class IQMemmapReader:
    def __init__(self, info: RecordingInfo):
        self.info = info
        self.path = Path(info.path)
        self.dtype = numpy_dtype_for_recording(info)
        expected_block_align = self.dtype.itemsize * info.fmt.channels
        if expected_block_align != info.fmt.block_align:
            raise UnsupportedSampleFormatError(
                "fmt block_align does not match channels × sample width; packed or padded samples are unsupported"
            )
        self._map = np.memmap(
            self.path,
            mode="r",
            dtype=self.dtype,
            offset=info.data_offset_bytes,
            shape=(info.frame_count, info.fmt.channels),
            order="C",
        )

    @property
    def frame_count(self) -> int:
        return self.info.frame_count

    def read_channels(
        self, start_frame: int, frame_count: int, normalize: bool = True
    ) -> NDArray[np.float32 | np.float64]:
        if start_frame < 0 or frame_count < 0:
            raise ValueError("start_frame and frame_count must be non-negative")
        end = min(self.frame_count, start_frame + frame_count)
        if start_frame >= end:
            return np.empty((0, self.info.fmt.channels), dtype=np.float32)
        raw = np.asarray(self._map[start_frame:end])
        if not normalize:
            return raw.copy()
        fmt = self.info.fmt
        if fmt.effective_format_code == 3:
            return raw.astype(np.float64, copy=True)
        if fmt.bits_per_sample == 8:
            return ((raw.astype(np.float32) - 128.0) / 128.0).astype(np.float32)
        scale = float(2 ** (fmt.bits_per_sample - 1))
        return (raw.astype(np.float64) / scale).astype(np.float32)

    def read_complex(
        self, start_frame: int, frame_count: int
    ) -> NDArray[np.complex64]:
        channels = self.read_channels(start_frame, frame_count, normalize=True)
        if channels.shape[1] < 2:
            raise UnsupportedSampleFormatError("At least two channels are required for IQ")
        if self.info.iq_order == "IQ":
            i_values, q_values = channels[:, 0], channels[:, 1]
        else:
            q_values, i_values = channels[:, 0], channels[:, 1]
        return (i_values + 1j * q_values).astype(np.complex64)
