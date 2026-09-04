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


# Ledger cell for the adaptive mode. A measurement may span several of these;
# the cells exist only to record which road has already been measured, so no
# two measurements -- at any speed, on any day -- can cover the same ground
# twice. Small enough that even a 100 m measurement is two cells, large enough
# that a set of them stays small across a long drive.
DEFAULT_LEDGER_CELL_M = 50.0

# Bounds on how much road one adaptive measurement covers. Both are 150 m, and
# both were set by measurement after an argument said otherwise.
#
# THE FLOOR. It is tempting to shorten the span in a city -- shadow fading
# decorrelates over 10-50 m there, so 150 m looks wastefully conservative and
# a shorter span would buy more measurements over the same streets. That
# reasoning is wrong, and the way to see it is to ask the only question that
# matters about a credible region: does it contain the transmitter as often as
# it says? Simulating a city drive under CORRELATED fading (Gudmundson,
# exponential autocorrelation, sigma 6 dB, decorrelation 20 m), 20 trials per
# spacing, counting how often the 90% region actually contained the truth:
#
#     spacing   measurements   90% region contained the truth   median area
#       50 m        146                    60%                    151 km2
#       80 m         91                    80%                    218 km2
#      100 m         73                    80%                    261 km2
#      150 m         48                    90%                    370 km2
#
# A 90% region that contains the truth 60% of the time is not a smaller region,
# it is a false one. Measurements do not merely need a gap wider than the
# decorrelation distance; adjacent spans AVERAGE neighbouring ground, and the
# residual correlation between those averages accumulates across a whole drive.
# 150 m is where the arithmetic stops over-claiming. (The regions in that run
# all touched the analysed edge, so the absolute areas are inflated; it is the
# coverage column that carries the result. Twenty trials put roughly +/-10
# points of sampling error on each figure -- enough to separate 60% from 90%,
# not enough to separate 80% from 90%.)
#
# THE CEILING. Measured on the Route 471 corridor (6.9 km, 40-110 km/h), same
# road, same solver, 40 m location error throughout: a longer span costs
# measurements over the same road and the region grows with it -- 54 bins and
# 7.19 km2 at 150 m against 33 bins and 29.91 km2 at 250 m.
#
# So the span does not vary. What the adaptive mode is still for is the PITCH:
# a measurement every 150 m of road travelled, with the next one held back
# until that distance has been covered. The fixed grid cannot do that -- a bin
# ends when the receiver leaves a square, whoever it entered it, so it emits
# short bins at cell boundaries whose neighbours sit inside one correlation
# length. That is the 50 m row above, arriving by accident.
MIN_ADAPTIVE_BIN_M = 150.0
MAX_ADAPTIVE_BIN_M = 150.0


@dataclass(slots=True)
class BinVisit:
    """One bin being accumulated, held only while the receiver is inside it."""

    key: BinKey
    latitudes: list[float] = field(default_factory=list)
    longitudes: list[float] = field(default_factory=list)
    spectra: list[Any] = field(default_factory=list)
    started_utc: str | None = None
    # Every ledger cell this visit's windows fell in. Marked measured together
    # when the visit closes, so a measurement spanning 200 m of road claims all
    # 200 m of it and a later pass cannot measure part of it again.
    cells: set[BinKey] = field(default_factory=set)
    # Where the visit began, for deciding when it has covered enough road.
    origin: tuple[float, float] | None = None
    span_target_m: float = 0.0

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

    def travelled_m(self) -> float:
        """How far the receiver has come since this visit started."""
        if self.origin is None or not self.latitudes:
            return 0.0
        metres_per_degree = 111_320.0
        cos_lat = math.cos(math.radians(self.origin[0]))
        return math.hypot(
            (self.longitudes[-1] - self.origin[1]) * metres_per_degree * cos_lat,
            (self.latitudes[-1] - self.origin[0]) * metres_per_degree,
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
    spectrum kept in memory (0.43 MB each at the default 16384-point FFT,
    1.7 MB at 65536, so hundreds of megabytes across a drive), and
    replacing would silently discard the first pass. Skipping is the option that neither grows
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

    def mark_all_measured(self, keys: set[BinKey]) -> None:
        self._measured.update(keys)

    def note_revisit(self) -> None:
        self.revisited_windows += 1

    @property
    def measured_count(self) -> int:
        return len(self._measured)


def adaptive_bin_size_m(
    speed_ms: float,
    *,
    window_seconds: float,
    windows_per_bin: int,
    minimum_m: float = MIN_ADAPTIVE_BIN_M,
    maximum_m: float = MAX_ADAPTIVE_BIN_M,
) -> float:
    """How much road one measurement should cover, at this speed.

    The span is simply the road it takes to gather the wanted number of
    windows: crawling through a town that is a short stretch, and on an open
    road it is a long one, and either way the measurement is averaged over
    the same number of samples. Clamped at both ends because the physics does
    not care how fast the car is going -- below the floor two measurements are
    the same measurement, and above the ceiling one measurement is an average
    over ground it cannot be pinned to.
    """
    wanted = max(speed_ms, 0.0) * window_seconds * max(windows_per_bin, 1)
    return min(max(wanted, minimum_m), maximum_m)


__all__ = [
    "DEFAULT_BIN_SIZE_M",
    "DEFAULT_LEDGER_CELL_M",
    "MAX_ADAPTIVE_BIN_M",
    "MIN_ADAPTIVE_BIN_M",
    "adaptive_bin_size_m",
    "BinGrid",
    "BinKey",
    "BinVisit",
    "anchor_tag",
]
