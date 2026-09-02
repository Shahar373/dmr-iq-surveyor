"""Turn stored survey observations into site-level geolocation measurements.

This is the only place where discovery meets reference data, and it happens
strictly after the fact: `survey run` has already measured and stored what
was on the air with no frequency list in hand, and this module then asks
"which registry site, if any, does each of those frequencies belong to".
Nothing here can influence what the detector looks for.

Three outcomes matter and are kept distinct, because collapsing them would
manufacture confidence that is not there:

* a **detection** -- the channel was found, with a level;
* a **non-detection** -- the frequency was inside this run's *measured*
  usable passband and nothing was found there. Real, constraining evidence;
* **not covered** -- the frequency was outside the measured passband. Not
  evidence at all. We did not look.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

ATTRIBUTION_DECODED = "decoded"
ATTRIBUTION_INFERRED_UNIQUE = "inferred_unique"
ATTRIBUTION_AMBIGUOUS_REUSE = "ambiguous_reuse"
ATTRIBUTION_FREQUENCY_UNKNOWN = "frequency_unknown"

USABILITY_USABLE = "usable"
USABILITY_NOT_COVERED = "not_covered"
USABILITY_NO_POSITION = "no_position"
USABILITY_AMBIGUOUS = "ambiguous"
# A detection whose level cannot be trusted as a measurement of distance:
# it sits where the receiver's own response distorts it, so keeping it would
# feed the solver a number that says more about the radio than the transmitter.
USABILITY_LEVEL_UNRELIABLE = "level_unreliable"
# The receiver's LO/DC artifact sits at the tuner centre and is present at
# every stop with a level set by the radio, not by any transmitter. Matching
# it to a site would inject a constant, confident, wrong measurement into
# every single stop -- the worst possible failure for this method.
USABILITY_RECEIVER_ARTIFACT = "receiver_artifact"
# A site with more than one control channel on record must contribute at
# most ONE measurement per stop: two channels of one site measured at one
# place are not independent evidence, and treating them as such would
# double-weight that site and shrink its region on fabricated information.
USABILITY_SUPERSEDED_CHANNEL = "superseded_channel"
# The whole survey run was barred from geolocation -- see
# `geo.store.exclude_run`, used when a capture was truncated or unhealthy.
USABILITY_RUN_EXCLUDED = "run_excluded"

LEVEL_METRIC_AVERAGE_SNR = "snr_db"
LEVEL_METRIC_P95_SNR = "p95_snr_db"

# Which detector threshold censors a non-detection, per level metric. The
# Phase 3 detector accepts a candidate on a conjunction of criteria, so no
# single number is the exact decision boundary; the matching SNR threshold
# is the closest honest stand-in and is stored with every row so a solution
# can always be read against the threshold that produced it.
_CENSOR_SETTING = {
    LEVEL_METRIC_AVERAGE_SNR: "min_average_channel_snr_db",
    LEVEL_METRIC_P95_SNR: "min_p95_channel_snr_db",
}

POSITION_SOURCE_RUN_GPS = "run_gps"
POSITION_SOURCE_SITE_PROFILE = "site_profile"
POSITION_SOURCE_MISSING = "missing"


@dataclass(slots=True)
class MeasurementSettings:
    level_metric: str = LEVEL_METRIC_AVERAGE_SNR
    # How far a measured centre may sit from the registry frequency and
    # still be the same channel. 6.25 kHz matches the band profile's own
    # comparison tolerance and the P25 raster.
    frequency_tolerance_hz: float = 6250.0
    # Extra margin held back from the measured usable passband edges. The
    # roll-off measurement is a heuristic, so a frequency sitting exactly on
    # the edge is better called "not covered" than counted as a real
    # non-detection.
    passband_guard_hz: float = 25_000.0

    def validate(self) -> None:
        if self.level_metric not in _CENSOR_SETTING:
            raise ValueError(
                f"level_metric must be one of {sorted(_CENSOR_SETTING)}, got {self.level_metric!r}"
            )
        if self.frequency_tolerance_hz < 0:
            raise ValueError("frequency_tolerance_hz must be non-negative")
        if self.passband_guard_hz < 0:
            raise ValueError("passband_guard_hz must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve_position(
    run: dict[str, Any], site_row: dict[str, Any] | None
) -> tuple[float | None, float | None, str, float | None]:
    """Where the receiver was for this run.

    The run's own GPS fix wins over the site profile's fixed coordinates:
    one profile is routinely reused across a mobile session (see
    `survey run --site-id`), so the profile's coordinates can describe a
    different place entirely.
    """
    if run.get("gps_latitude") is not None and run.get("gps_longitude") is not None:
        return (
            float(run["gps_latitude"]),
            float(run["gps_longitude"]),
            POSITION_SOURCE_RUN_GPS,
            run.get("gps_accuracy_m"),
        )
    if (
        site_row is not None
        and site_row.get("latitude") is not None
        and site_row.get("longitude") is not None
    ):
        return (
            float(site_row["latitude"]),
            float(site_row["longitude"]),
            POSITION_SOURCE_SITE_PROFILE,
            None,
        )
    return None, None, POSITION_SOURCE_MISSING, None


def _covered(run: dict[str, Any], frequency_hz: float, guard_hz: float) -> bool:
    low, high = run.get("usable_low_hz"), run.get("usable_high_hz")
    if low is None or high is None:
        return False
    return float(low) + guard_hz <= frequency_hz <= float(high) - guard_hz


def _nearest_observation(
    observations: list[dict[str, Any]], frequency_hz: float, tolerance_hz: float
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_error = float("inf")
    for observation in observations:
        error = abs(float(observation["measured_center_hz"]) - frequency_hz)
        if error <= tolerance_hz and error < best_error:
            best, best_error = observation, error
    return best


def build_run_measurements(
    connection: sqlite3.Connection,
    survey_run_id: str,
    settings: MeasurementSettings | None = None,
) -> list[dict[str, Any]]:
    """Derive one measurement row per (registry channel) for one survey run."""
    resolved = settings or MeasurementSettings()
    resolved.validate()

    run_row = connection.execute(
        "SELECT * FROM survey_runs WHERE survey_run_id = ?", (survey_run_id,)
    ).fetchone()
    if run_row is None:
        raise ValueError(f"Unknown survey run: {survey_run_id}")
    run = dict(run_row)

    site_row = connection.execute(
        "SELECT * FROM sites WHERE site_id = ?", (run["site_id"],)
    ).fetchone()
    site = dict(site_row) if site_row is not None else None

    run_excluded = connection.execute(
        "SELECT reason FROM geo_run_exclusions WHERE survey_run_id = ?", (survey_run_id,)
    ).fetchone()

    latitude, longitude, position_source, accuracy = _resolve_position(run, site)
    detection_settings = json.loads(run["detection_settings_json"] or "{}")
    censor_level = float(detection_settings.get(_CENSOR_SETTING[resolved.level_metric], 0.0))

    observations = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM rf_observations WHERE survey_run_id = ?", (survey_run_id,)
        )
    ]
    channels = [
        dict(row)
        for row in connection.execute(
            """
            SELECT
                c.p25_site_id, c.frequency_hz, s.site_key,
                (
                    SELECT COUNT(DISTINCT o.p25_site_id)
                    FROM p25_site_channels o
                    WHERE o.frequency_hz = c.frequency_hz AND o.role = c.role
                ) AS sharing_site_count,
                (
                    SELECT GROUP_CONCAT(other.site_key, ', ')
                    FROM p25_site_channels o
                    JOIN p25_sites other ON other.p25_site_id = o.p25_site_id
                    WHERE o.frequency_hz = c.frequency_hz AND o.role = c.role
                ) AS sharing_site_keys
            FROM p25_site_channels c
            JOIN p25_sites s ON s.p25_site_id = c.p25_site_id
            ORDER BY c.frequency_hz
            """
        )
    ]

    base_flags: list[str] = []
    if site is not None and site.get("gain") is None:
        base_flags.append("not_gain_comparable")
    if run.get("coverage_status") == "partial":
        base_flags.append("partial_coverage")
    if run.get("capture_time_source") == "unknown":
        base_flags.append("capture_time_unknown")
    if position_source == POSITION_SOURCE_SITE_PROFILE:
        base_flags.append("position_from_site_profile")

    rows: list[dict[str, Any]] = []
    for channel in channels:
        frequency_hz = float(channel["frequency_hz"])
        shared = int(channel["sharing_site_count"] or 1)
        flags = list(base_flags)

        if shared > 1:
            attribution = ATTRIBUTION_AMBIGUOUS_REUSE
            attribution_detail = (
                f"{frequency_hz / 1e6:.6f} MHz is on record for {shared} sites "
                f"({channel['sharing_site_keys']}); a level measured on it is a mixture"
            )
        else:
            attribution = ATTRIBUTION_INFERRED_UNIQUE
            attribution_detail = (
                "attributed by frequency alone: no control-channel decode confirmed the "
                "RFSS/Site of the transmitter actually heard here"
            )

        observation = _nearest_observation(
            observations, frequency_hz, resolved.frequency_tolerance_hz
        )
        detected = observation is not None
        covered = _covered(run, frequency_hz, resolved.passband_guard_hz)

        if run_excluded is not None:
            usability = USABILITY_RUN_EXCLUDED
            exclusion = str(run_excluded["reason"])
        elif attribution == ATTRIBUTION_AMBIGUOUS_REUSE:
            usability = USABILITY_AMBIGUOUS
            exclusion = "frequency shared by more than one site"
        elif latitude is None:
            usability = USABILITY_NO_POSITION
            exclusion = "the survey run has no coordinates, so this cannot be placed on a map"
        elif not covered and not detected:
            usability = USABILITY_NOT_COVERED
            exclusion = (
                "outside the run's measured usable passband; nothing was looked at here, "
                "which is not the same as having looked and heard nothing"
            )
        else:
            usability = USABILITY_USABLE
            exclusion = ""
            if not covered and detected:
                # Detected outside the measured passband. The detection is
                # real, but the roll-off understates its level by an amount
                # nothing here can bound, and the solver reads level as
                # distance -- so it would place the site further away, with
                # confidence. Recorded, not used.
                usability = USABILITY_LEVEL_UNRELIABLE
                exclusion = (
                    "detected outside the run's measured usable passband, where the receiver "
                    "roll-off understates the level by an unknown amount"
                )
                flags.append("detected_outside_measured_passband")

        if observation is not None:
            if observation.get("edge_warning"):
                flags.append("edge_warning")
            if observation.get("dc_warning"):
                flags.append("dc_warning")
            if usability == USABILITY_USABLE and observation.get("dc_warning"):
                usability = USABILITY_RECEIVER_ARTIFACT
                exclusion = (
                    "this detection sits on the receiver's own DC/LO artifact, whose level is a "
                    "property of the radio rather than of any transmitter"
                )
            # `edge_warning` is deliberately NOT an exclusion. It marks a fixed
            # 150 kHz margin from the recording's Nyquist edges
            # (`SpectrumSettings.edge_exclusion_hz`), which is an absolute
            # width rather than anything measured: at a 200 kS/s rate it covers
            # the entire band and would exclude every detection in the run. The
            # measured usable passband above is the honest edge test, and it
            # already runs. This stays a flag.

        rows.append(
            {
                "p25_site_id": int(channel["p25_site_id"]),
                "site_key": channel["site_key"],
                "frequency_hz": frequency_hz,
                "latitude": latitude,
                "longitude": longitude,
                "position_source": position_source,
                "position_accuracy_m": accuracy,
                "detected": detected,
                "level_db": (
                    float(observation[resolved.level_metric]) if observation is not None else None
                ),
                "level_metric": resolved.level_metric,
                "censor_level_db": censor_level,
                "power_unit": (
                    observation["power_unit"] if observation is not None else "dbfs_per_hz"
                ),
                "calibrated": bool(observation["calibrated"]) if observation is not None else False,
                "attribution": attribution,
                "attribution_detail": attribution_detail,
                "usability": usability,
                "exclusion_reason": exclusion,
                "quality_flags": flags,
                "measured_center_hz": (
                    float(observation["measured_center_hz"]) if observation is not None else None
                ),
                "frequency_error_hz": (
                    float(observation["measured_center_hz"]) - frequency_hz
                    if observation is not None
                    else None
                ),
                "occupancy_pct": (
                    float(observation["occupancy_pct"]) if observation is not None else None
                ),
                "persistence": (
                    float(observation["persistence"]) if observation is not None else None
                ),
                "capture_start_utc": run.get("capture_start_utc"),
                "site_id": run.get("site_id"),
            }
        )
    return _keep_one_channel_per_site(rows)


def _keep_one_channel_per_site(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Leave at most one USABLE measurement per site per run.

    Two control channels of one site, measured from one place at one moment,
    are two views of the same transmitter -- not independent evidence. The
    solver multiplies likelihood terms, so letting both through would
    double-weight that site and shrink its credible region on information
    that was never there. The strongest detection wins (or, with no
    detection, the first usable non-detection); the rest are kept as rows,
    marked superseded, so nothing is silently dropped.
    """
    best: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row["usability"] != USABILITY_USABLE:
            continue
        site_id = row["p25_site_id"]
        incumbent = best.get(site_id)
        if incumbent is None:
            best[site_id] = row
            continue
        # Prefer a detection over a non-detection, then the stronger level.
        candidate_key = (row["detected"], row["level_db"] if row["level_db"] is not None else -1e9)
        incumbent_key = (
            incumbent["detected"],
            incumbent["level_db"] if incumbent["level_db"] is not None else -1e9,
        )
        if candidate_key > incumbent_key:
            best[site_id] = row

    for row in rows:
        if row["usability"] != USABILITY_USABLE:
            continue
        if best.get(row["p25_site_id"]) is not row:
            row["usability"] = USABILITY_SUPERSEDED_CHANNEL
            row["exclusion_reason"] = (
                "this site has more than one control channel on record; another channel already "
                "carries this stop's measurement, and two channels of one site at one place are "
                "not independent evidence"
            )
    return rows


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [row for row in rows if row["usability"] == USABILITY_USABLE]
    return {
        "total": len(rows),
        "usable": len(usable),
        "detections": sum(1 for row in usable if row["detected"]),
        "non_detections": sum(1 for row in usable if not row["detected"]),
        "not_covered": sum(1 for row in rows if row["usability"] == USABILITY_NOT_COVERED),
        "ambiguous": sum(1 for row in rows if row["usability"] == USABILITY_AMBIGUOUS),
        "no_position": sum(1 for row in rows if row["usability"] == USABILITY_NO_POSITION),
        "run_excluded": sum(1 for row in rows if row["usability"] == USABILITY_RUN_EXCLUDED),
        "level_unreliable": sum(
            1 for row in rows if row["usability"] == USABILITY_LEVEL_UNRELIABLE
        ),
        "receiver_artifact": sum(
            1 for row in rows if row["usability"] == USABILITY_RECEIVER_ARTIFACT
        ),
        "superseded_channel": sum(
            1 for row in rows if row["usability"] == USABILITY_SUPERSEDED_CHANNEL
        ),
    }


__all__ = [
    "ATTRIBUTION_AMBIGUOUS_REUSE",
    "ATTRIBUTION_DECODED",
    "ATTRIBUTION_FREQUENCY_UNKNOWN",
    "ATTRIBUTION_INFERRED_UNIQUE",
    "LEVEL_METRIC_AVERAGE_SNR",
    "LEVEL_METRIC_P95_SNR",
    "POSITION_SOURCE_MISSING",
    "POSITION_SOURCE_RUN_GPS",
    "POSITION_SOURCE_SITE_PROFILE",
    "USABILITY_AMBIGUOUS",
    "USABILITY_LEVEL_UNRELIABLE",
    "USABILITY_NOT_COVERED",
    "USABILITY_NO_POSITION",
    "USABILITY_RECEIVER_ARTIFACT",
    "USABILITY_RUN_EXCLUDED",
    "USABILITY_SUPERSEDED_CHANNEL",
    "USABILITY_USABLE",
    "MeasurementSettings",
    "build_run_measurements",
    "summarise",
]
