"""Per-stop common-mode level offsets: the receiver's contribution, separated.

The method compares levels between places, so anything that shifts a whole
stop's levels together corrupts it: a re-seated or re-oriented antenna, local
interference raising the noise floor, front-end compression next to a strong
site. (Receiver gain itself largely cancels in an SNR, since signal and noise
scale together -- which is why gain is tracked separately, as a flag.)

Such an effect is separable from geometry precisely because it is *common*.
The propagation model already predicts how strong each site should be at each
stop; if every site at one stop comes out 6 dB below its prediction while the
rest of the campaign fits, the stop is the thing that differs, not the
geometry. The median residual across sites at a stop is that offset.

Two limits are enforced rather than assumed away:

* **Identifiability.** With fewer than `min_sites` detections at a stop, a
  "common" offset cannot be told apart from one site's model error, so no
  offset is estimated and the stop is reported `not_estimable`.
* **Gauge.** Adding a constant to every stop's offset and the same constant
  to every site's reference level is an identical fit. The offsets are
  therefore centred on zero, and only *relative* differences between stops
  mean anything.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

STATUS_ESTIMATED = "estimated"
STATUS_NOT_ESTIMABLE = "not_estimable"
STATUS_WITHIN_NOISE = "within_noise"
# A large offset whose sites disagree among themselves is not a common-mode
# effect at all -- it is ordinary model misfit at one stop, and correcting it
# would push a real geometry around to suit a fitting error.
STATUS_INCONSISTENT = "inconsistent"


@dataclass(slots=True)
class CommonModeSettings:
    # Below this many detected sites at a stop, a shared offset cannot be
    # distinguished from one site's model error.
    min_sites: int = 3
    # Offsets smaller than this are ordinary scatter, not a receiver effect.
    # A real one -- an antenna knocked out of position, local interference --
    # is several dB; the grid and the quantised path-loss exponent leave
    # residual structure of order a dB or two on their own.
    min_offset_db: float = 3.0
    # "Common" is the whole claim, so it has to be tested. In a genuine
    # common-mode shift every site moves together, leaving scatter about the
    # offset much smaller than the offset itself. A large median residual with
    # large disagreement between sites is model misfit at one stop, not the
    # receiver, and correcting it would bend real geometry to suit a fit error.
    max_scatter_ratio: float = 0.5
    enabled: bool = True

    def validate(self) -> None:
        if self.min_sites < 2:
            raise ValueError("min_sites must be at least 2")
        if self.min_offset_db < 0:
            raise ValueError("min_offset_db must be non-negative")
        if self.max_scatter_ratio <= 0:
            raise ValueError("max_scatter_ratio must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StopOffset:
    survey_run_id: str
    status: str
    offset_db: float
    site_count: int
    scatter_db: float
    applied: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_offsets(
    residuals_by_run: dict[str, list[float]], settings: CommonModeSettings | None = None
) -> dict[str, StopOffset]:
    """Estimate one common-mode offset per stop from per-site residuals.

    `residuals_by_run` maps a survey run id to the measured-minus-predicted
    level of each site detected at that stop, taken from a first solving pass.
    """
    resolved = settings or CommonModeSettings()
    resolved.validate()

    raw: dict[str, float] = {}
    offsets: dict[str, StopOffset] = {}
    for run_id, residuals in residuals_by_run.items():
        values = np.asarray([value for value in residuals if np.isfinite(value)], dtype=float)
        if values.size < resolved.min_sites:
            offsets[run_id] = StopOffset(
                survey_run_id=run_id,
                status=STATUS_NOT_ESTIMABLE,
                offset_db=0.0,
                site_count=int(values.size),
                scatter_db=float(np.std(values)) if values.size else 0.0,
                applied=False,
                reason=(
                    f"{values.size} site(s) detected here; at least {resolved.min_sites} are "
                    "needed before a shared offset can be told apart from one site's model error"
                ),
            )
            continue
        # Median, not mean: one badly-fitting site must not drag the estimate.
        raw[run_id] = float(np.median(values))
        offsets[run_id] = StopOffset(
            survey_run_id=run_id,
            status=STATUS_ESTIMATED,
            offset_db=raw[run_id],
            site_count=int(values.size),
            scatter_db=float(np.median(np.abs(values - raw[run_id]))),
            applied=False,
            reason="",
        )

    if not raw:
        return offsets

    # Gauge fix: only differences between stops are identifiable.
    centre = float(np.median(list(raw.values())))
    for run_id, offset in offsets.items():
        if offset.status != STATUS_ESTIMATED:
            continue
        offset.offset_db = raw[run_id] - centre
        if abs(offset.offset_db) < resolved.min_offset_db:
            offset.status = STATUS_WITHIN_NOISE
            offset.reason = (
                f"{offset.offset_db:+.1f} dB from the campaign median, below the "
                f"{resolved.min_offset_db:.1f} dB worth reacting to"
            )
            offset.offset_db = 0.0
        elif offset.scatter_db > resolved.max_scatter_ratio * abs(offset.offset_db):
            offset.status = STATUS_INCONSISTENT
            offset.reason = (
                f"the sites here disagree by {offset.scatter_db:.1f} dB about a "
                f"{offset.offset_db:+.1f} dB shift, so it is not a shift they share; this looks "
                "like model misfit at one stop rather than the receiver"
            )
            offset.offset_db = 0.0
        else:
            offset.applied = resolved.enabled
            offset.reason = (
                f"every site heard at this stop sits {offset.offset_db:+.1f} dB from its "
                f"predicted level, agreeing to within {offset.scatter_db:.1f} dB across "
                f"{offset.site_count} site(s); the stop differs from the campaign, not the geometry. "
                "The magnitude is a lower bound: the first fitting pass already absorbed part of it"
            )
    return offsets


def residuals_by_run(solutions: list[dict[str, Any]]) -> dict[str, list[float]]:
    """Collect per-stop residuals from a first pass over every site."""
    collected: dict[str, list[float]] = {}
    for solution in solutions:
        if solution.get("status") not in ("ok", "unbounded_region", "weak_geometry"):
            continue
        for residual in solution.get("residuals") or []:
            if not residual.get("detected"):
                continue
            value = residual.get("residual_db")
            run_id = residual.get("survey_run_id")
            if value is None or not run_id:
                continue
            collected.setdefault(str(run_id), []).append(float(value))
    return collected


def summarise(offsets: dict[str, StopOffset]) -> dict[str, Any]:
    applied = [offset for offset in offsets.values() if offset.applied]
    return {
        "stops": len(offsets),
        "estimated": sum(1 for o in offsets.values() if o.status == STATUS_ESTIMATED),
        "within_noise": sum(1 for o in offsets.values() if o.status == STATUS_WITHIN_NOISE),
        "inconsistent": sum(1 for o in offsets.values() if o.status == STATUS_INCONSISTENT),
        "not_estimable": sum(1 for o in offsets.values() if o.status == STATUS_NOT_ESTIMABLE),
        "applied": len(applied),
        "largest_offset_db": (
            max((abs(o.offset_db) for o in offsets.values()), default=0.0)
        ),
        "offsets": {run_id: offset.to_dict() for run_id, offset in sorted(offsets.items())},
    }


__all__ = [
    "STATUS_ESTIMATED",
    "STATUS_INCONSISTENT",
    "STATUS_NOT_ESTIMABLE",
    "STATUS_WITHIN_NOISE",
    "CommonModeSettings",
    "StopOffset",
    "estimate_offsets",
    "residuals_by_run",
    "summarise",
]
