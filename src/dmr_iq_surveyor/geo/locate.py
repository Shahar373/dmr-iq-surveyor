"""Phase 8 "Locate": a frequency becomes a target with its own tuning and campaign.

The field app's Locate flow starts from one number, the carrier frequency, and
derives everything a drive needs from it: the emitter it is solved as, where
the receiver is tuned, a band profile for that tuning, and the campaign its
measurements are written into, together with the gain that campaign is
measured at. This module owns those rules and their storage. It opens no
device and runs no capture; `web/service.py` composes it with what already
does (`docs/phase8-field-app-locate.md`, section 4).

Discovery before reference still holds. The target frequency decides where the
receiver is tuned, which every capture has to decide somehow. The generated
band profile covers a 500 kHz window around it with the same detection
thresholds as every narrowband profile, so the detector still reports whatever
is in that window. The target is matched to observations only afterwards, by
`materialise_emitter_measurements`, exactly as from the command line.

Storage is additive: one new table, `locate_targets`, beside the emitter
tables. Nothing existing is altered.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from dmr_iq_surveyor.geo.emitters import (
    DEFAULT_TOLERANCE_HZ,
    EMISSION_CONTINUOUS,
    EmitterError,
    connect_emitter_database,
    list_emitters,
    upsert_emitter,
)
from dmr_iq_surveyor.survey.profiles import load_band_profile
from dmr_iq_surveyor.survey.provenance import normalise_campaign_id

# -- tuning ------------------------------------------------------------------

# 2 MS/s is what the 469 MHz trial was sized for (`config/bands/uhf_469_narrow.yaml`)
# and what a Pi writes without overflows. Nothing is recorded in a drive, so a
# wider capture would buy only CPU load.
LOCATE_SAMPLE_RATE_HZ = 2_000_000.0
# The tuner sits this far below the target, so the receiver's own DC/LO spike
# at the tuned centre does not land on it. Same offset as `uhf_469_narrow`
# (centre 469.250 MHz for a 469.350 MHz carrier).
LOCATE_CENTRE_OFFSET_HZ = 100_000.0
# Half-width of the generated band profile around the target.
LOCATE_WINDOW_HALF_WIDTH_HZ = 250_000.0
# What a capture measures reliably: the inner 80 % of its span
# (`FieldService.calibration_targets` uses the same bound), and nothing within
# one 12.5 kHz channel of the DC spike.
_USABLE_FRACTION_OF_RATE = 0.4
_DC_GUARD_HZ = 12_500.0

CAMPAIGN_PREFIX = "loc-"

# Detection, segmentation and comparison values of `config/bands/uhf_469_narrow.yaml`,
# which are themselves those of `central_800_narrow.yaml`: a 12.5 kHz narrowband
# carrier looks the same to the detector at any of these frequencies. Kept here
# rather than read from `config/` because the field service may run with a
# profile directory that does not hold that file; a test pins the two equal.
_DETECTION = {
    "scan_step_hz": 6250,
    "integration_width_hz": 12500,
    "min_p95_channel_snr_db": 9.0,
    "min_average_channel_snr_db": 4.0,
    "min_equivalent_width_hz": 3000,
    "min_width_90_hz": 5000,
    "max_width_90_hz": 13000,
    "max_peak_to_channel_mean_db": 13.0,
    "min_fill_ratio": 0.20,
    "fill_threshold_db": 6.0,
    "merge_tolerance_hz": 4000,
}
_COMPARISON = {
    "frequency_tolerance_hz": 6250,
    "snr_delta_db": 3.0,
    "occupancy_delta_pct": 10.0,
    "persistence_delta": 0.25,
    "analyzed_seconds_ratio_limit": 4.0,
}
_SEGMENTATION = {
    "segment_seconds": 1.0,
    "segment_stride_seconds": 5.0,
    "max_segments": 40,
}

# Channel bandwidths a target may be declared with. 12.5 kHz is what every
# profile here was built and measured for. A wider channel scales the
# detector's widths, scan step and merge tolerance with it: measured on
# synthetic carriers, a 40 kHz-wide signal under the 12.5 kHz profile was
# split into two observations 14 kHz either side of its centre, outside the
# match tolerance, and so recorded as *not heard* right beside the transmitter.
CHANNEL_BANDWIDTHS_HZ = (12_500.0, 25_000.0, 50_000.0)
DEFAULT_CHANNEL_BANDWIDTH_HZ = 12_500.0

_KEY_SLUG = re.compile(r"[^a-z0-9._-]+")


class LocateError(ValueError):
    """A target that cannot be prepared as asked. The message says why."""


def target_key(frequency_hz: float) -> str:
    """The emitter key a frequency is declared under: 469.35 MHz -> `f469_350000`.

    Whole hertz, so 164.5375 MHz and 164.53750 MHz are one target, and the
    same frequency always resumes the same one.
    """
    hertz = _whole_hertz(frequency_hz)
    return f"f{hertz // 1_000_000}_{hertz % 1_000_000:06d}"


def auto_tuning(frequency_hz: float) -> tuple[float, float]:
    """(centre, sample rate) for a target: 100 kHz below it, at 2 MS/s."""
    hertz = _whole_hertz(frequency_hz)
    centre = round((hertz - LOCATE_CENTRE_OFFSET_HZ) / 1_000.0) * 1_000.0
    return float(centre), LOCATE_SAMPLE_RATE_HZ


def check_tuning(frequency_hz: float, centre_hz: float, sample_rate_hz: float) -> None:
    """Refuse a tuning that would not measure the whole profile window.

    The window is what the generated band profile asks the detector to
    cover; if part of it falls outside what the capture measures well, its
    runs would report partial coverage and a non-detection there would mean
    "not looked at" rather than "not heard".
    """
    if not math.isfinite(centre_hz) or centre_hz <= 0:
        raise LocateError("the centre frequency must be a positive number of Hz")
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise LocateError("the sample rate must be a positive number of samples per second")
    usable = _USABLE_FRACTION_OF_RATE * sample_rate_hz
    low = frequency_hz - LOCATE_WINDOW_HALF_WIDTH_HZ
    high = frequency_hz + LOCATE_WINDOW_HALF_WIDTH_HZ
    if low < centre_hz - usable or high > centre_hz + usable:
        raise LocateError(
            f"a capture centred at {centre_hz / 1e6:.6f} MHz at {sample_rate_hz / 1e6:g} MS/s "
            f"measures {(centre_hz - usable) / 1e6:.6f}-{(centre_hz + usable) / 1e6:.6f} MHz "
            f"reliably, which does not cover {low / 1e6:.6f}-{high / 1e6:.6f} MHz around the "
            "target; move the centre closer or raise the sample rate"
        )
    if abs(frequency_hz - centre_hz) < _DC_GUARD_HZ:
        raise LocateError(
            "the target would sit on the receiver's DC spike at the tuned centre; "
            f"keep the centre at least {_DC_GUARD_HZ / 1e3:g} kHz away from it"
        )


def check_bandwidth(bandwidth_hz: float) -> float:
    value = float(bandwidth_hz)
    if value not in CHANNEL_BANDWIDTHS_HZ:
        raise LocateError(
            "the channel bandwidth must be one of "
            + ", ".join(f"{choice / 1e3:g} kHz" for choice in CHANNEL_BANDWIDTHS_HZ)
        )
    return value


def default_tolerance_hz(bandwidth_hz: float) -> float:
    """How far a measured centre may sit from the target: half a channel."""
    return float(bandwidth_hz) / 2.0


def band_profile_mapping(
    key: str, frequency_hz: float, bandwidth_hz: float = DEFAULT_CHANNEL_BANDWIDTH_HZ
) -> dict[str, Any]:
    """The band profile a target's drives are analysed with.

    At 12.5 kHz it is exactly the `uhf_469_narrow` thresholds. A wider
    channel scales every width-like value by the same factor and leaves the
    SNR gates alone: the detector should judge a 25 kHz carrier by the same
    signal-to-noise as a 12.5 kHz one, only over its own width.
    """
    hertz = _whole_hertz(frequency_hz)
    bandwidth = check_bandwidth(bandwidth_hz)
    scale = bandwidth / DEFAULT_CHANNEL_BANDWIDTH_HZ
    low = int(hertz - LOCATE_WINDOW_HALF_WIDTH_HZ)
    high = int(hertz + LOCATE_WINDOW_HALF_WIDTH_HZ)
    detection = dict(_DETECTION)
    comparison = dict(_COMPARISON)
    rasters = [12500, 6250]
    if scale != 1.0:
        for name in ("scan_step_hz", "integration_width_hz", "min_equivalent_width_hz",
                     "min_width_90_hz", "max_width_90_hz", "merge_tolerance_hz"):
            detection[name] = round(_DETECTION[name] * scale)
        comparison["frequency_tolerance_hz"] = round(
            _COMPARISON["frequency_tolerance_hz"] * scale
        )
        rasters = [int(bandwidth), int(bandwidth / 2)]
    return {
        "name": profile_name(key, bandwidth),
        "label": (
            f"Locate {hertz / 1e6:.6f} MHz, {bandwidth / 1e3:g} kHz channel: "
            f"{low / 1e6:.6f}-{high / 1e6:.6f} MHz, generated by the field app"
        ),
        "start_frequency_hz": low,
        "stop_frequency_hz": high,
        "raster_spacings_hz": rasters,
        "detection": detection,
        **_SEGMENTATION,
        "usable_passband_rolloff_db": 3.0,
        "comparison": comparison,
    }


def profile_name(key: str, bandwidth_hz: float = DEFAULT_CHANNEL_BANDWIDTH_HZ) -> str:
    """`target_<key>`, plus `_bw25k` / `_bw50k` for a wider channel."""
    bandwidth = float(bandwidth_hz)
    if bandwidth == DEFAULT_CHANNEL_BANDWIDTH_HZ:
        return f"target_{key}"
    return f"target_{key}_bw{bandwidth / 1e3:g}k"


def write_band_profile(
    output_root: str | Path,
    key: str,
    frequency_hz: float,
    bandwidth_hz: float = DEFAULT_CHANNEL_BANDWIDTH_HZ,
) -> Path:
    """Write a target's band profile once, and return its path.

    An existing file is kept as it is. Runs already measured name it, and a
    later build that tuned the thresholds must not change what an open
    campaign is analysed with halfway through.
    """
    directory = Path(output_root).expanduser().resolve() / "bands"
    path = directory / f"{profile_name(key, bandwidth_hz)}.yaml"
    if not path.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        header = (
            "# Written by the field app's Locate flow (geo/locate.py). It says only\n"
            "# where to look and how to threshold what is found: no frequency list,\n"
            "# no protocol. Do not edit: runs already measured name this file.\n"
        )
        text = header + yaml.safe_dump(
            band_profile_mapping(key, frequency_hz, bandwidth_hz),
            sort_keys=False, allow_unicode=True,
        )
        temporary = path.with_suffix(".yaml.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    load_band_profile(path)  # refuse here, not at the first drive
    return path


def campaign_id_for(key: str, generation: int) -> str:
    """`loc-<key>`, then `loc-<key>-2`, `-3`... for each "start over"."""
    slug = _KEY_SLUG.sub("-", key.strip().lower()).strip("-._") or "target"
    suffix = "" if generation <= 1 else f"-{generation}"
    campaign = f"{CAMPAIGN_PREFIX}{slug[: 64 - len(CAMPAIGN_PREFIX) - len(suffix)]}{suffix}"
    resolved = normalise_campaign_id(campaign)
    assert resolved is not None
    return resolved


# -- storage -----------------------------------------------------------------

LOCATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS locate_targets (
    campaign_id TEXT PRIMARY KEY,
    emitter_key TEXT NOT NULL,
    generation INTEGER NOT NULL,
    frequency_hz REAL NOT NULL,
    center_frequency_hz REAL NOT NULL,
    sample_rate_hz REAL NOT NULL,
    channel_bandwidth_hz REAL NOT NULL DEFAULT 12500,
    tuning_source TEXT NOT NULL,
    band_profile_path TEXT NOT NULL,
    if_gain_reduction_db REAL NOT NULL,
    lna_state INTEGER NOT NULL,
    gain_source TEXT NOT NULL,
    calibrated_at_utc TEXT,
    calibration_path TEXT,
    created_at_utc TEXT NOT NULL,
    selected_at_utc TEXT NOT NULL,
    UNIQUE(emitter_key, generation)
);
CREATE INDEX IF NOT EXISTS idx_locate_targets_emitter
    ON locate_targets(emitter_key, generation);
"""

TUNING_AUTO = "auto"
TUNING_OPERATOR = "operator"
# The gain in force when the campaign was opened. It is stored at once, so a
# campaign driven without calibrating still resumes at the gain it was
# measured with, rather than at whatever the service started with next time.
GAIN_IN_FORCE = "in_force_at_creation"
GAIN_CALIBRATED = "calibration"


def connect_locate_database(path: str | Path) -> sqlite3.Connection:
    """The emitter schema (and through it Phase 7's) plus `locate_targets`."""
    connection = connect_emitter_database(path)
    connection.executescript(LOCATE_SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(locate_targets)")}
    if "channel_bandwidth_hz" not in columns:
        connection.execute(
            "ALTER TABLE locate_targets ADD COLUMN channel_bandwidth_hz REAL NOT NULL "
            "DEFAULT 12500"
        )
    connection.commit()
    return connection


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _whole_hertz(frequency_hz: float) -> int:
    value = float(frequency_hz)
    if not math.isfinite(value) or value <= 0:
        raise LocateError("the target frequency must be a positive number of Hz")
    return round(value)


def campaign_run_count(connection: sqlite3.Connection, campaign_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM survey_runs WHERE campaign_id = ?", (campaign_id,)
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _emitter_at(connection: sqlite3.Connection, hertz: int) -> dict[str, Any] | None:
    for emitter in list_emitters(connection):
        if abs(float(emitter["frequency_hz"]) - hertz) <= 1.0:
            return emitter
    return None


def _resolve_emitter(
    connection: sqlite3.Connection,
    frequency_hz: float,
    payload: dict[str, Any],
    *,
    implied_tolerance_hz: float | None,
    emitter_key: str | None = None,
) -> dict[str, Any]:
    """Declare the target's emitter, or reuse the one already at this frequency.

    Two declared emitters within tolerance of each other mark every
    observation between them `ambiguous`, so neither could ever be solved.
    An emitter declared from the command line at exactly this frequency is
    therefore reused under its own key; one *near* it is refused, by name,
    rather than declared beside it.

    `implied_tolerance_hz` is half the channel bandwidth when one was asked
    for; an explicit `tolerance_hz` in the payload still wins.
    """
    hertz = _whole_hertz(frequency_hz)
    existing = _emitter_at(connection, hertz)
    if payload.get("tolerance_hz") is not None:
        tolerance = float(payload["tolerance_hz"])
    elif implied_tolerance_hz is not None:
        tolerance = float(implied_tolerance_hz)
    elif existing is not None:
        tolerance = float(existing["tolerance_hz"])
    else:
        tolerance = DEFAULT_TOLERANCE_HZ
    for emitter in list_emitters(connection):
        if existing is not None and emitter["emitter_id"] == existing["emitter_id"]:
            continue
        reach = max(tolerance, float(emitter["tolerance_hz"]))
        if abs(float(emitter["frequency_hz"]) - hertz) <= reach:
            raise LocateError(
                f"emitter {emitter['emitter_key']!r} is already declared at "
                f"{float(emitter['frequency_hz']) / 1e6:.6f} MHz, within {reach:g} Hz of "
                "this target. Two targets that close would make every observation between "
                "them ambiguous; pick that one, or remove it first (`geo emitter remove`)"
            )

    def given(name: str, fallback: Any) -> Any:
        return payload[name] if payload.get(name) is not None else fallback

    base = existing or {
        "emitter_key": emitter_key or target_key(hertz),
        "frequency_hz": float(hertz),
        "label": "",
        "emission": EMISSION_CONTINUOUS,
        "truth_latitude": None,
        "truth_longitude": None,
        "truth_source": "",
        "notes": "",
    }
    # A known position, for validation only: absent keeps the stored one,
    # `{}` clears it, and anything else replaces it whole (upsert_emitter
    # refuses half a position or one with no source).
    truth = payload.get("truth")
    latitude, longitude, source = (
        base["truth_latitude"], base["truth_longitude"], str(base["truth_source"])
    )
    if truth is not None:
        if not isinstance(truth, dict):
            raise LocateError("truth must be an object with latitude, longitude and source")
        try:
            latitude = None if truth.get("latitude") is None else float(truth["latitude"])
            longitude = None if truth.get("longitude") is None else float(truth["longitude"])
        except (TypeError, ValueError) as exc:
            raise LocateError("the known position must be two numbers") from exc
        source = str(truth.get("source") or "")
    try:
        return upsert_emitter(
            connection,
            emitter_key=str(base["emitter_key"]),
            frequency_hz=float(base["frequency_hz"]),
            label=str(given("label", base["label"])),
            tolerance_hz=tolerance,
            emission=str(given("emission", base["emission"])),
            truth_latitude=latitude,
            truth_longitude=longitude,
            truth_source=source,
            notes=str(base["notes"]),
        )
    except (EmitterError, TypeError, ValueError) as exc:
        raise LocateError(str(exc)) from exc


def _latest_row(connection: sqlite3.Connection, emitter_key: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM locate_targets WHERE emitter_key = ? ORDER BY generation DESC LIMIT 1",
        (emitter_key,),
    ).fetchone()
    return dict(row) if row is not None else None


def _refuse_change_with_runs(
    connection: sqlite3.Connection, campaign: str, what: str, was: str
) -> None:
    runs = campaign_run_count(connection, campaign)
    if runs:
        raise LocateError(
            f"campaign {campaign!r} already holds {runs} run(s) measured at {was}; levels "
            f"measured with another {what} would not be comparable. Start over to use a "
            f"new {what}"
        )


def prepare_target(
    connection: sqlite3.Connection,
    *,
    frequency_hz: float,
    output_root: str | Path,
    gain_in_force: tuple[float, int],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Declare (or resume) the target for `frequency_hz` and its campaign.

    `payload` may carry `label`, `emission`, `tolerance_hz`, `truth`
    (`{latitude, longitude, source}`, or `{}` to clear it),
    `channel_bandwidth_hz` (12500, 25000 or 50000), `center_frequency_hz` /
    `sample_rate_hz` (an operator tuning), and `start_over` (open the next
    campaign for this target instead of resuming the latest one).

    A resumed campaign keeps its tuning, bandwidth and gain. A different
    tuning or bandwidth is accepted only while the campaign holds no runs:
    levels measured through two of either are not comparable. Every such
    refusal is made before anything is written.
    """
    given = dict(payload or {})
    hertz = _whole_hertz(frequency_hz)
    requested_bandwidth = (
        check_bandwidth(given["channel_bandwidth_hz"])
        if given.get("channel_bandwidth_hz") is not None else None
    )
    start_over = bool(given.get("start_over"))
    wants_tuning = (
        given.get("center_frequency_hz") is not None or given.get("sample_rate_hz") is not None
    )

    known = _emitter_at(connection, hertz)
    if known is not None:
        latest = _latest_row(connection, str(known["emitter_key"]))
        key_for_new = str(known["emitter_key"])
    else:
        # The emitter may have been removed (`geo emitter remove`) after
        # Locate opened a campaign for it. Its campaigns and runs stay; the
        # frequency resumes the latest of them, under the key it had, rather
        # than colliding with it as a "new" generation 1.
        orphan = connection.execute(
            "SELECT emitter_key FROM locate_targets WHERE ABS(frequency_hz - ?) <= 1.0 "
            "ORDER BY created_at_utc DESC, generation DESC LIMIT 1",
            (float(hertz),),
        ).fetchone()
        key_for_new = str(orphan[0]) if orphan is not None else target_key(hertz)
        latest = _latest_row(connection, key_for_new) if orphan is not None else None
    resuming = latest is not None and not start_over
    frequency = float(known["frequency_hz"]) if known else float(hertz)

    if resuming:
        assert latest is not None
        campaign = str(latest["campaign_id"])
        bandwidth = float(latest["channel_bandwidth_hz"])
        centre = float(given.get("center_frequency_hz") or latest["center_frequency_hz"])
        rate = float(given.get("sample_rate_hz") or latest["sample_rate_hz"])
        tuning_changed = (centre, rate) != (
            float(latest["center_frequency_hz"]), float(latest["sample_rate_hz"])
        )
        bandwidth_changed = (
            requested_bandwidth is not None and requested_bandwidth != bandwidth
        )
        if tuning_changed:
            _refuse_change_with_runs(
                connection, campaign, "tuning",
                f"{float(latest['center_frequency_hz']) / 1e6:.6f} MHz, "
                f"{float(latest['sample_rate_hz']) / 1e6:g} MS/s",
            )
            check_tuning(frequency, centre, rate)
        if bandwidth_changed:
            _refuse_change_with_runs(
                connection, campaign, "channel bandwidth", f"{bandwidth / 1e3:g} kHz"
            )
            bandwidth = float(requested_bandwidth)
    else:
        generation = 1 if latest is None else int(latest["generation"]) + 1
        # Starting over keeps the target's bandwidth unless another is asked for.
        bandwidth = requested_bandwidth or (
            float(latest["channel_bandwidth_hz"]) if latest else DEFAULT_CHANNEL_BANDWIDTH_HZ
        )
        auto_centre, auto_rate = auto_tuning(frequency)
        centre = float(given.get("center_frequency_hz") or auto_centre)
        rate = float(given.get("sample_rate_hz") or auto_rate)
        check_tuning(frequency, centre, rate)
        campaign = campaign_id_for(key_for_new, generation)
        if campaign_run_count(connection, campaign):
            # A campaign id the operator used by hand for something else.
            raise LocateError(
                f"campaign {campaign!r} already holds runs that this target did not write; "
                "rename that campaign's runs or remove this target's key clash first"
            )

    # Every refusal above is made; from here on, write.
    #
    # The emitter's tolerance follows the bandwidth only where the bandwidth
    # is new: a new emitter, or a campaign whose bandwidth differs from the
    # one before it. Re-saving the same bandwidth must not overwrite a
    # tolerance set on purpose (`geo emitter add --tolerance-hz`).
    previous_bandwidth = (
        float(latest["channel_bandwidth_hz"]) if latest else DEFAULT_CHANNEL_BANDWIDTH_HZ
    )
    emitter = _resolve_emitter(
        connection, frequency, given,
        emitter_key=key_for_new,
        implied_tolerance_hz=(
            default_tolerance_hz(bandwidth)
            if known is None or bandwidth != previous_bandwidth else None
        ),
    )
    key = str(emitter["emitter_key"])
    profile = write_band_profile(output_root, key, frequency, bandwidth)
    now = _now()
    if resuming:
        connection.execute(
            """
            UPDATE locate_targets
            SET center_frequency_hz = ?, sample_rate_hz = ?, channel_bandwidth_hz = ?,
                band_profile_path = ?, selected_at_utc = ?,
                tuning_source = CASE WHEN ? THEN ? ELSE tuning_source END
            WHERE campaign_id = ?
            """,
            (centre, rate, bandwidth, str(profile), now, int(tuning_changed),
             TUNING_OPERATOR, campaign),
        )
    else:
        connection.execute(
            """
            INSERT INTO locate_targets(
                campaign_id, emitter_key, generation, frequency_hz, center_frequency_hz,
                sample_rate_hz, channel_bandwidth_hz, tuning_source, band_profile_path,
                if_gain_reduction_db, lna_state, gain_source, calibrated_at_utc,
                calibration_path, created_at_utc, selected_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                campaign, key, generation, frequency, centre, rate, bandwidth,
                TUNING_OPERATOR if wants_tuning else TUNING_AUTO, str(profile),
                float(gain_in_force[0]), int(gain_in_force[1]), GAIN_IN_FORCE, now, now,
            ),
        )
    connection.commit()
    return describe_target(connection, campaign)


def record_target_gain(
    connection: sqlite3.Connection,
    campaign_id: str,
    *,
    if_gain_reduction_db: float,
    lna_state: int,
    calibration_path: str | None,
) -> None:
    """Store a calibrated gain against a target's campaign.

    The caller has already decided the gain may change (the campaign holds
    no runs, `FieldService._apply_calibration`); this only remembers it so a
    restart resumes the campaign at the same gain.
    """
    connection.execute(
        """
        UPDATE locate_targets
        SET if_gain_reduction_db = ?, lna_state = ?, gain_source = ?,
            calibrated_at_utc = ?, calibration_path = ?
        WHERE campaign_id = ?
        """,
        (
            float(if_gain_reduction_db), int(lna_state), GAIN_CALIBRATED, _now(),
            calibration_path, campaign_id,
        ),
    )
    connection.commit()


def describe_target(connection: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
    """One target campaign, as the field app shows it."""
    row = connection.execute(
        "SELECT * FROM locate_targets WHERE campaign_id = ?", (campaign_id,)
    ).fetchone()
    if row is None:
        raise LocateError(f"no Locate target owns campaign {campaign_id!r}")
    target = dict(row)
    emitter = connection.execute(
        "SELECT * FROM emitter_targets WHERE emitter_key = ?", (target["emitter_key"],)
    ).fetchone()
    if emitter is None:
        # Removed with `geo emitter remove` since. The campaign and its runs
        # stay; selecting the frequency again declares it afresh.
        raise LocateError(
            f"the emitter {target['emitter_key']!r} behind campaign {campaign_id!r} "
            "was removed; choose the frequency again to declare it"
        )
    runs = campaign_run_count(connection, campaign_id)
    solution = connection.execute(
        """
        SELECT status, area_km2_90, solved_at FROM emitter_solutions
        WHERE emitter_id = ? AND campaign_id = ?
        ORDER BY emitter_solution_id DESC LIMIT 1
        """,
        (emitter["emitter_id"], campaign_id),
    ).fetchone()
    return {
        "emitter_key": target["emitter_key"],
        "frequency_hz": float(target["frequency_hz"]),
        "label": emitter["label"],
        "emission": emitter["emission"],
        "tolerance_hz": float(emitter["tolerance_hz"]),
        "has_known_position": emitter["truth_latitude"] is not None,
        "campaign_id": campaign_id,
        "generation": int(target["generation"]),
        "tuning": {
            "center_frequency_hz": float(target["center_frequency_hz"]),
            "sample_rate_hz": float(target["sample_rate_hz"]),
            "source": target["tuning_source"],
        },
        "channel_bandwidth_hz": float(target["channel_bandwidth_hz"]),
        "band_profile_path": target["band_profile_path"],
        "gain": {
            "if_gain_reduction_db": float(target["if_gain_reduction_db"]),
            "lna_state": int(target["lna_state"]),
            "source": target["gain_source"],
            "calibrated_at_utc": target["calibrated_at_utc"],
            "calibration_path": target["calibration_path"],
        },
        "run_count": runs,
        # Calibration is offered only where it could still change the gain.
        "needs_calibration": target["calibrated_at_utc"] is None and runs == 0,
        "latest_solution": dict(solution) if solution is not None else None,
        "created_at_utc": target["created_at_utc"],
        "selected_at_utc": target["selected_at_utc"],
    }


def list_targets(connection: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    """The latest campaign of each target, most recently selected first."""
    rows = connection.execute(
        """
        SELECT l.campaign_id FROM locate_targets l
        WHERE l.generation = (
            SELECT MAX(generation) FROM locate_targets m WHERE m.emitter_key = l.emitter_key
        )
        AND EXISTS (SELECT 1 FROM emitter_targets e WHERE e.emitter_key = l.emitter_key)
        ORDER BY l.selected_at_utc DESC, l.campaign_id
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [describe_target(connection, str(row[0])) for row in rows]


__all__ = [
    "CAMPAIGN_PREFIX",
    "CHANNEL_BANDWIDTHS_HZ",
    "DEFAULT_CHANNEL_BANDWIDTH_HZ",
    "GAIN_CALIBRATED",
    "GAIN_IN_FORCE",
    "LOCATE_CENTRE_OFFSET_HZ",
    "LOCATE_SAMPLE_RATE_HZ",
    "LOCATE_WINDOW_HALF_WIDTH_HZ",
    "TUNING_AUTO",
    "TUNING_OPERATOR",
    "LocateError",
    "auto_tuning",
    "band_profile_mapping",
    "campaign_id_for",
    "campaign_run_count",
    "check_bandwidth",
    "check_tuning",
    "connect_locate_database",
    "default_tolerance_hz",
    "describe_target",
    "list_targets",
    "prepare_target",
    "profile_name",
    "record_target_gain",
    "target_key",
    "write_band_profile",
]
