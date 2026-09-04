"""Spatial binning for a moving survey.

A drive produces a measurement every second or so. Feeding those to the
solver one by one would be wrong in a way that looks right: the solver
multiplies likelihoods, so a thousand samples along a road would shrink a
credible region as if they were a thousand independent constraints. They are
not. Shadow fading -- the thing `sigma_db` models -- decorrelates over
roughly 10-50 m in a city and 100-200 m in suburbs, so consecutive samples
14 m apart are very nearly the same measurement. Treated as independent, a
20-minute drive would collapse the region by a factor near 240 in area,
almost all of it fabricated.

Binning fixes that, and fixes a second thing at the same time. Averaging the
samples that fall in one bin removes fast (multipath) fading, which at
868 MHz swings the level by many dB over a fraction of a metre; drive-test
practice is to average over about 40 wavelengths, which is 14 m here, and a
bin is comfortably larger than that. What comes out is the local mean --
exactly the quantity the path-loss model is written in terms of.

So a drive becomes a sequence of virtual stops on a grid, one measurement
each, and the estimator needs no change at all to consume it.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

from dmr_iq_surveyor.geo.model import LocalProjection

# Bin edge. Above the ~100-200 m suburban decorrelation distance for shadow
# fading, so two bins are close to independent, and far above the 14 m needed
# to average out fast fading. Smaller would readmit the correlation this
# exists to remove; much larger would throw away geometry the drive paid for.
DEFAULT_BIN_SIZE_M = 150.0


@dataclass(frozen=True, slots=True)
class BinKey:
    """Integer grid coordinates in the campaign's local plane."""

    x: int
    y: int

    def run_id(self, anchor_tag: str) -> str:
        """The virtual stop's id: the grid square, not the drive.

        Keyed by position rather than by session so that driving the same
        street again lands on the SAME id and replaces that measurement
        instead of adding a second, near-identical one beside it. Two
        measurements 20 m apart are not two constraints -- that is the
        correlation this module exists to prevent -- and giving each drive
        its own ids would smuggle it back in through the front door.

        The anchor tag is part of the id because a grid index only means
        something relative to the origin it was measured from. Ids built on
        different anchors are different ids, so they can never silently
        overwrite one another.
        """
        return f"live_{anchor_tag}_{self.x:+06d}_{self.y:+06d}"


@dataclass(slots=True)
class BinVisit:
    """One bin being accumulated, held only while the receiver is inside it."""

    key: BinKey
    latitudes: list[float] = field(default_factory=list)
    longitudes: list[float] = field(default_factory=list)
    spectra: list[Any] = field(default_factory=list)
    started_utc: str | None = None

    @property
    def window_count(self) -> int:
        return len(self.spectra)

    def centroid(self) -> tuple[float, float]:
        """Where the bin's measurements were actually taken.

        The bin centre would be a guess; this is the mean of the positions
        that produced the samples, which is where the level was measured.
        """
        return (
            sum(self.latitudes) / len(self.latitudes),
            sum(self.longitudes) / len(self.longitudes),
        )

    def spread_m(self) -> float:
        """Largest distance from the centroid to any contributing position.

        Reported rather than assumed: a bin whose samples cluster in one
        corner is not the same measurement as one crossed end to end, and a
        reader of the result should be able to tell them apart.
        """
        if len(self.latitudes) < 2:
            return 0.0
        centre_lat, centre_lon = self.centroid()
        metres_per_degree = 111_320.0
        cos_lat = math.cos(math.radians(centre_lat))
        return max(
            math.hypot(
                (lon - centre_lon) * metres_per_degree * cos_lat,
                (lat - centre_lat) * metres_per_degree,
            )
            for lat, lon in zip(self.latitudes, self.longitudes, strict=True)
        )


def anchor_tag(latitude: float, longitude: float) -> str:
    """A short, stable fingerprint of a grid origin.

    Carried in every virtual stop's id so that a campaign whose anchor moved
    -- a different site profile, a reconfigured app -- produces visibly
    different ids rather than colliding with the old grid's.
    """
    digest = hashlib.sha256(f"{latitude:.6f},{longitude:.6f}".encode()).hexdigest()
    return digest[:8]


class BinGrid:
    """Assigns positions to bins, and remembers which have been measured.

    A bin is measured ONCE. Driving back down the same road re-enters bins
    that already have a measurement, and those windows are counted and
    dropped rather than merged in: merging would need every earlier
    spectrum kept in memory (about 1.8 MB each at a 65536-point FFT, so
    hundreds of megabytes across a drive), and replacing would silently
    discard the first pass. Skipping is the option that neither grows
    without bound nor quietly loses data, and the count is reported so the
    operator can see how much of the drive retraced itself.
    """

    def __init__(
        self,
        anchor_latitude: float,
        anchor_longitude: float,
        *,
        bin_size_m: float = DEFAULT_BIN_SIZE_M,
    ):
        if bin_size_m <= 0:
            raise ValueError("bin_size_m must be positive")
        # The anchor is an explicit campaign constant, never "wherever this
        # drive happened to start". A grid built from the first fix would
        # give every drive its own coordinate frame, so the same street
        # would carry different bin indices on different days and the
        # duplicate measurements this class exists to prevent would all be
        # written as distinct evidence.
        self.anchor = (float(anchor_latitude), float(anchor_longitude))
        self.tag = anchor_tag(anchor_latitude, anchor_longitude)
        self.projection = LocalProjection(anchor_latitude, anchor_longitude)
        self.bin_size_m = float(bin_size_m)
        self._measured: set[BinKey] = set()
        self.revisited_windows = 0

    def key_for(self, latitude: float, longitude: float) -> BinKey:
        east, north = self.projection.to_local(latitude, longitude)
        return BinKey(
            x=math.floor(float(east) / self.bin_size_m),
            y=math.floor(float(north) / self.bin_size_m),
        )

    def already_measured(self, key: BinKey) -> bool:
        return key in self._measured

    def mark_measured(self, key: BinKey) -> None:
        self._measured.add(key)

    def note_revisit(self) -> None:
        self.revisited_windows += 1

    @property
    def measured_count(self) -> int:
        return len(self._measured)


__all__ = [
    "DEFAULT_BIN_SIZE_M",
    "BinGrid",
    "BinKey",
    "BinVisit",
    "anchor_tag",
]
