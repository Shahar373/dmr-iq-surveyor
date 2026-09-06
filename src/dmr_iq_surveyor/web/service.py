"""Field service layer: the operations the web app exposes.

Every operation here is a thin composition of code that already exists and
is tested elsewhere -- `capture.run_capture`, `survey.run_survey`,
`geo.pipeline` -- so the web app cannot drift into a second, differently
behaved implementation of the pipeline.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.capture.core import CaptureSettings, run_capture
from dmr_iq_surveyor.capture.device import probe_soapysdr
from dmr_iq_surveyor.geo.export import to_gpx, to_kml
from dmr_iq_surveyor.geo.measurements import MeasurementSettings
from dmr_iq_surveyor.geo.model import SolveSettings
from dmr_iq_surveyor.geo.pipeline import (
    VALIDATION_NOTE,
    build_map_geojson,
    materialise_measurements,
    site_overview,
    solve_all_sites,
)
from dmr_iq_surveyor.geo.solver import GEOLOCATION_MATURITY
from dmr_iq_surveyor.geo.store import (
    EXCLUSION_SCOPE_NON_DETECTIONS,
    clear_run_exclusion,
    connect_geo_database,
    exclude_run,
    latest_plan,
    run_exclusion,
    solution_history,
)
from dmr_iq_surveyor.live.session import LiveSession, LiveSettings, Position
from dmr_iq_surveyor.reference.store import list_sites
from dmr_iq_surveyor.survey.pipeline import DEFAULT_DATABASE_PATH, DriveViewSettings, run_survey
from dmr_iq_surveyor.survey.profiles import (
    ProfileError,
    SiteProfile,
    resolve_band_profile,
    resolve_site_profile,
)
from dmr_iq_surveyor.survey.store import delete_survey_run
from dmr_iq_surveyor.web.jobs import Job, JobRegistry
from dmr_iq_surveyor.web.recordings import disk_status, enforce_retention, purge_recordings

_SLUG = re.compile(r"[^a-z0-9]+")

# How much of a drive the status endpoint carries back. Both are for drawing,
# not for the record -- the record is in the database -- so they are bounded
# and the oldest end is dropped. A phone polling this every second should not
# be handed a growing payload for an hour.
_LIVE_TRAIL_LIMIT = 1_200
_LIVE_BIN_LIMIT = 400


class PositionStale(RuntimeError):
    """The marked position is old enough that it must be confirmed."""

POSITION_SOURCE_BROWSER = "browser_gps"
POSITION_SOURCE_MANUAL = "user"


def slugify(value: str, fallback: str) -> str:
    slug = _SLUG.sub("_", value.strip().lower()).strip("_")
    return slug or fallback


# Minimum wall-clock spacing between forwarded capture-progress callbacks.
# Fast enough to look live to a human, far below the per-chunk rate a
# capture actually delivers at.
_CAPTURE_PROGRESS_MIN_INTERVAL_SECONDS = 0.5


def throttle_capture_progress(
    callback: Callable[[int, int, float], None],
    *,
    min_interval_seconds: float = _CAPTURE_PROGRESS_MIN_INTERVAL_SECONDS,
) -> Callable[[int, int, float], None]:
    """Wrap a capture `on_progress` callback so it forwards at most a few
    times a second, always including the final (`frames >= target`) call.

    `run_capture()` calls its `on_progress` on every device read -- tens of
    times a second at a field sample rate -- from INSIDE the same loop that
    reads from the SDR. `Job.emit()` (the callback this wraps in practice)
    takes a lock, formats a timestamp and appends to a list; cheap once, but
    SoapySDR's own ring buffer is only tens of milliseconds deep (see
    `capture/device.py`), so that overhead, paid on every single chunk for
    the whole capture, is enough to starve the read loop into a real buffer
    overflow. Reproduced live in the field: a 90 s capture at 5 MS/s through
    this app overflowed continuously and had to be aborted, while the
    identical rate and gain through the plain CLI -- whose progress callback
    only updates a Rich progress bar, no lock or list involved -- completed
    two clean 15 s runs with zero overflows.
    """
    last_emitted = -min_interval_seconds

    def wrapped(frames: int, target: int, elapsed: float) -> None:
        nonlocal last_emitted
        if frames < target and elapsed - last_emitted < min_interval_seconds:
            return
        last_emitted = elapsed
        callback(frames, target, elapsed)

    return wrapped


@dataclass(slots=True)
class FieldSettings:
    database_path: Path = field(default_factory=lambda: DEFAULT_DATABASE_PATH)
    recordings_dir: Path = field(default_factory=lambda: Path("runs/field/recordings"))
    output_root: Path = field(default_factory=lambda: Path("runs/field"))
    profile_base_dir: Path = field(default_factory=lambda: Path("."))
    band: str = "central_800"
    site_profile: str = "home"
    center_frequency_hz: float = 867_406_250.0
    sample_rate_hz: float = 5_000_000.0
    # 90 s at 5 MS/s is 1.68 GiB. With one recording kept that peaks at
    # 3.35 GiB, which fits the ~10 GiB a Pi in the field actually has, and
    # still yields 18 analysis segments -- enough for persistence to tell a
    # continuous control channel from a passing burst.
    duration_seconds: float = 90.0
    if_gain_reduction_db: float = 40.0
    lna_state: int = 2
    driver: str = "sdrplay"
    allow_capture: bool = True
    # Solving every site after each stop is what makes the map update in
    # the field, but a full-resolution solve over a few dozen sites takes
    # minutes on a Pi -- too long to sit through at every stop. The field
    # solve therefore runs at a coarser grid by default; `geo solve` at
    # full resolution is the end-of-day step.
    solve_after_capture: bool = True
    solve_resolution_m: float = 250.0
    solve_max_cells: int = 40_000
    # A 5 MS/s, 120 s stop writes ~2.24 GiB, so a campaign cannot keep every
    # recording on the storage a Pi has in the field. The survey has already
    # extracted everything geolocation needs into SQLite, and `live stop`
    # takes a stationary measurement without writing IQ at all -- but nothing
    # in this codebase yet captures a short IQ snippet around an interesting
    # event, so keeping zero recordings would leave nothing to replay a field
    # anomaly against. Transitional policy (stabilize/p25-geolocation-v0.10,
    # see docs/known-issues-v0.10.md): keep the most recent one. Revisit once
    # event-triggered IQ snippets exist. A failed stop still keeps its IQ
    # regardless of this setting, and every deletion is written to a ledger.
    keep_recordings: int = 1
    # Every recorded stop is also read back the way a drive bin hears it
    # (24 spread periodograms per one-second window, at the live FFT size)
    # and stored as a second, solve-excluded run beside the stop. Measured
    # reason: the detector's p95 is a percentile across frames, so a channel
    # near the gate is decided differently by 24 frames and by ~150. Keeping
    # both views is what lets the digest say how often that happens on air.
    drive_view_for_stops: bool = True
    # A capture that delivered materially less than it asked for must not
    # contribute: undetected weak signals would become non-detections, which
    # is evidence, not absence.
    min_capture_completeness: float = 0.8
    # Fraction of the recorded wall-clock span the samples actually account
    # for. Below this the gaps are wide enough that "heard nothing" might
    # mean "was not listening", and the stop's non-detections are set aside.
    #
    # 0.95 is set against what these measurements actually are. A registry
    # channel here is a P25 CONTROL channel, which transmits continuously --
    # so a site audible from a stop is audible across the whole recording,
    # not in one window that a gap might straddle. Losing a few percent of
    # the span costs a little averaging, and leaves a non-detection saying
    # exactly what it said before: nothing was there.
    #
    # A threshold appropriate to intermittent traffic would have to be far
    # tighter, since a gap really could swallow the only transmission. That
    # is a different measurement than the one this system makes.
    #
    # On what an overflow actually costs, measured rather than assumed: a
    # 90 s capture reporting 23 overflows spanned 137 s of wall clock, and a
    # 60 s capture reporting 27 spanned 153 s. That is seconds per event,
    # not the milliseconds the FIFO is deep -- because the depth is the
    # buffer, while the loss is however long the stall lasted, and the
    # driver goes on overflowing throughout it. So 0.95 admits a couple of
    # brief stalls in a 90 s capture, not the dozens a naive
    # milliseconds-per-event reading would suggest.
    #
    # Those two recordings covered under half the span they occupied. Their
    # non-detections were rightly refused, and the fix for that was to stop
    # the stalls (see throttle_capture_progress), never to widen this.
    #
    # What it is deliberately NOT is a count of overflows: a stop recorded
    # 8 km east of every other one -- the geometry the campaign most needed
    # -- was discarded over a single overflow, because one event and
    # twenty-seven events were treated identically.
    min_time_coverage: float = 0.95
    # Beyond this, the marked position is presumed stale and the operator has
    # to confirm it before a stop is recorded against it.
    position_stale_after_seconds: float = 1_200.0
    # -- live (moving) survey ---------------------------------------------
    # A drive never writes IQ. Each spatial bin becomes an ordinary survey
    # run of about 8 KiB, measured, so a whole campaign of driving costs
    # single-digit megabytes against the gigabytes a handful of stationary
    # stops cost. That is the reason the mode exists on a Pi at all.
    live_window_seconds: float = 1.0
    live_bin_size_m: float = 150.0
    live_min_windows_per_bin: int = 3
    live_max_windows_per_bin: int = 10
    live_max_position_age_seconds: float = 5.0
    # Analysis size per window: 305 Hz at 5 MS/s, forty bins across a 12.5 kHz
    # channel. Measured against 65536 on a synthesised carrier at 35, 20 and
    # 10 dB, the reported SNR agrees within 0.2 dB -- so a drive's bins and a
    # stationary stop's offline analysis stay on one scale -- while costing a
    # quarter of the memory and a third of the detection time, both of which
    # bind on a Pi.
    live_fft_size: int = 16_384
    live_frames_per_window: int = 24
    # Re-solve automatically after this many new bins. Zero -- the default --
    # means never: the operator asks for a solve when they want one. Solving
    # is seconds to minutes of CPU across a campaign's sites, and although it
    # runs on its own thread while the stream keeps going, it still competes
    # for the Pi's cores and every core it takes is overflows on the stream.
    # Bins are being written the whole time either way; what a solve buys is
    # only seeing them, and that is a decision worth leaving to the person in
    # the car. Set it above zero to have it happen on its own.
    live_solve_every_bins: int = 0
    # The measurement span itself is fixed at 150 m either way (see
    # live/bins.py: MIN_ADAPTIVE_BIN_M == MAX_ADAPTIVE_BIN_M == 150.0,
    # measured against correlated shadow fading -- a shorter span was tried
    # and rejected). What "adaptive" controls is the *pitch*: the grid
    # underneath becomes a fixed 50 m ledger of measured road, and the next
    # measurement only starts a full 150 m span from where the last one
    # began, which is what stops spans from ever overlapping at any speed.
    live_adaptive_bins: bool = True
    # Backstop only, not a plan: a drive normally ends when the operator
    # stops it. Without a cap a forgotten session holds the SDR and burns
    # the battery until the Pi is found.
    live_max_seconds: float = 7_200.0
    # Origin of the bin grid, a campaign constant. Falls back to the map
    # centre, which the operator already configured for this campaign, so
    # two drives on different days land on the SAME grid and the second
    # pass down a street replaces its measurement instead of adding a
    # near-identical one beside it.
    live_anchor: tuple[float, float] | None = None
    tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    tile_attribution: str = "(c) OpenStreetMap contributors"
    map_center: tuple[float, float] = (32.0853, 34.7818)
    map_zoom: int = 11
    token: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("token", None)
        for key in ("database_path", "recordings_dir", "output_root", "profile_base_dir"):
            payload[key] = str(payload[key])
        payload["map_center"] = list(self.map_center)
        payload["live_anchor"] = list(self.live_anchor) if self.live_anchor else None
        payload["tool_version"] = __version__
        return payload


class FieldService:
    """Stateful glue between HTTP requests and the analysis pipeline."""

    def __init__(self, settings: FieldSettings) -> None:
        self.settings = settings
        self.jobs = JobRegistry()
        self._position_path = Path(settings.output_root).expanduser().resolve() / "position.json"
        # Live-drive state. Held in memory only: a fix is worth nothing a
        # minute later, and the measurements it produced are already in the
        # database by then.
        self._live_lock = threading.Lock()
        self._live_fix: Position | None = None
        self._live_fix_wall: str | None = None
        self._live_fix_count = 0
        self._live_trail: list[dict[str, Any]] = []
        self._live_bins: list[dict[str, Any]] = []
        self._live_pending_runs: list[str] = []
        self._live_stats: dict[str, Any] = {}
        self._live_job_id: str | None = None
        self._live_solving = False
        self._live_last_solve: dict[str, Any] | None = None
        # A pending "measure here" request, handed to the live session the
        # next time it starts a window, and the running session itself so the
        # status poll can read the hold's live state off it.
        self._live_hold_request: float | None = None
        self._live_session: LiveSession | None = None
        # Registry control channels, built once per drive, so a near-threshold
        # channel in a bin can be named as a site without a database hit on
        # every two-second poll from the phone.
        self._live_cc_index: list[tuple[float, str]] = []
        self._live_cc_tolerance_hz: float = 6_250.0

    # -- operator position -------------------------------------------------

    def get_position(self) -> dict[str, Any]:
        """The operator's current position, persisted across restarts.

        A field session survives the Pi rebooting or the browser being
        closed; losing the marked position at that point would silently
        attach the next recording to nowhere.
        """
        if self._position_path.is_file():
            try:
                return json.loads(self._position_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {"latitude": None, "longitude": None, "source": "unavailable"}
        return {"latitude": None, "longitude": None, "source": "not_set"}

    def set_position(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            latitude = float(payload["latitude"])
            longitude = float(payload["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("latitude and longitude are required numbers") from exc
        if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
            raise ValueError("latitude or longitude is out of range")
        accuracy = payload.get("accuracy_m")
        record = {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_m": float(accuracy) if accuracy is not None else None,
            "altitude_m": (
                float(payload["altitude_m"]) if payload.get("altitude_m") is not None else None
            ),
            "label": str(payload.get("label") or "").strip(),
            "source": (
                POSITION_SOURCE_BROWSER
                if payload.get("source") == "device"
                else POSITION_SOURCE_MANUAL
            ),
            "set_at": datetime.now(UTC).isoformat(),
        }
        self._position_path.parent.mkdir(parents=True, exist_ok=True)
        self._position_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return record

    # -- read models -------------------------------------------------------

    def device_probe(self) -> dict[str, Any]:
        return probe_soapysdr(self.settings.driver).to_dict()

    def survey_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        connection = connect_geo_database(Path(self.settings.database_path))
        try:
            rows = connection.execute(
                """
                SELECT survey_run_id, site_id, capture_start_utc, coverage_status,
                       gps_latitude, gps_longitude, gps_source, analyzed_seconds,
                       (SELECT COUNT(*) FROM rf_observations o
                        WHERE o.survey_run_id = r.survey_run_id) AS observation_count
                FROM survey_runs r
                ORDER BY COALESCE(capture_start_utc, imported_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def disk(self) -> dict[str, Any]:
        return disk_status(
            self.settings.recordings_dir,
            sample_rate_hz=self.settings.sample_rate_hz,
            duration_seconds=self.settings.duration_seconds,
            keep_recordings=self.settings.keep_recordings,
        ).to_dict()

    def purge(self) -> dict[str, Any]:
        return purge_recordings(self.settings.recordings_dir)

    def position_age_seconds(self) -> float | None:
        position = self.get_position()
        set_at = position.get("set_at")
        if not set_at:
            return None
        try:
            marked = datetime.fromisoformat(set_at)
        except ValueError:
            return None
        return max(0.0, (datetime.now(UTC) - marked).total_seconds())

    def state(self) -> dict[str, Any]:
        return {
            "settings": self.settings.to_public_dict(),
            "position": self.get_position(),
            "position_age_seconds": self.position_age_seconds(),
            "device": self.device_probe(),
            "disk": self.disk(),
            "sites": site_overview(database_path=self.settings.database_path),
            "runs": self.survey_runs(),
            "stops": self.stops(),
            "plan": self.plan(),
            "jobs": self.jobs.list(),
            "live": self.live_status(),
            "geolocation": {
                "maturity": GEOLOCATION_MATURITY,
                "validation_note": VALIDATION_NOTE,
            },
        }

    def geojson(self) -> dict[str, Any]:
        collection = build_map_geojson(database_path=self.settings.database_path)
        plan = self.plan()
        collection["features"].extend(plan.get("geojson", {}).get("features", []))
        return collection

    def plan(self) -> dict[str, Any]:
        """The latest next-stop plan, or an explicit note that there is none."""
        connection = connect_geo_database(Path(self.settings.database_path))
        try:
            stored = latest_plan(connection)
        finally:
            connection.close()
        if stored is None:
            return {
                "status": "none",
                "reason": "no solve has run yet",
                "plan": {},
                "geojson": {"type": "FeatureCollection", "features": []},
            }
        return {
            "status": stored["status"],
            "reason": stored["reason"],
            "solve_batch_id": stored["solve_batch_id"],
            "plan": json.loads(stored["plan_json"] or "{}"),
            "geojson": json.loads(stored["geojson"] or "{}"),
        }

    def site_history(self, site_key: str) -> dict[str, Any]:
        """How one site's region changed as sessions accumulated."""
        connection = connect_geo_database(Path(self.settings.database_path))
        try:
            row = connection.execute(
                "SELECT p25_site_id FROM p25_sites WHERE site_key = ?", (site_key,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown site key: {site_key}")
            history = solution_history(connection, int(row["p25_site_id"]))
        finally:
            connection.close()
        return {"site_key": site_key, "history": history}

    def stops(self) -> list[dict[str, Any]]:
        """Every stop, with whether it is contributing and why not."""
        connection = connect_geo_database(Path(self.settings.database_path))
        try:
            rows = connection.execute(
                """
                SELECT r.survey_run_id, r.site_id, r.capture_start_utc, r.coverage_status,
                       r.gps_latitude, r.gps_longitude, r.analyzed_seconds, s.gain,
                       (SELECT COUNT(*) FROM rf_observations o
                        WHERE o.survey_run_id = r.survey_run_id) AS observation_count,
                       (SELECT COUNT(*) FROM geo_measurements m
                        WHERE m.survey_run_id = r.survey_run_id AND m.usability = 'usable'
                          AND m.detected = 1) AS detections,
                       (SELECT COUNT(*) FROM geo_measurements m
                        WHERE m.survey_run_id = r.survey_run_id AND m.usability = 'usable'
                          AND m.detected = 0) AS non_detections,
                       (SELECT reason FROM geo_run_exclusions e
                        WHERE e.survey_run_id = r.survey_run_id) AS exclusion_reason
                FROM survey_runs r LEFT JOIN sites s ON s.site_id = r.site_id
                ORDER BY COALESCE(r.capture_start_utc, r.imported_at) DESC
                """
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def set_stop_excluded(
        self, run_id: str, *, excluded: bool, reason: str = ""
    ) -> dict[str, Any]:
        """Take a stop out of the geolocation, or put it back.

        Excluding keeps the recording's observations and says why they do not
        count; it is the reversible option, and the right one when a stop is
        merely suspect.
        """
        connection = connect_geo_database(Path(self.settings.database_path))
        try:
            if connection.execute(
                "SELECT COUNT(*) AS n FROM survey_runs WHERE survey_run_id = ?", (run_id,)
            ).fetchone()["n"] == 0:
                raise ValueError(f"unknown stop: {run_id}")
            if excluded:
                exclude_run(
                    connection, run_id, reason or "excluded by the operator in the field"
                )
            else:
                clear_run_exclusion(connection, run_id)
            current = run_exclusion(connection, run_id)
        finally:
            connection.close()
        materialise_measurements(database_path=self.settings.database_path, run_ids=[run_id])
        return {"survey_run_id": run_id, "excluded": current is not None, "reason": current or ""}

    def delete_stop(self, run_id: str) -> dict[str, Any]:
        """Remove a stop entirely, including its observations."""
        connection = connect_geo_database(Path(self.settings.database_path))
        try:
            result = delete_survey_run(connection, run_id)
        finally:
            connection.close()
        if not result["existed"]:
            raise ValueError(f"unknown stop: {run_id}")
        return result

    def export(self, export_format: str) -> tuple[str, str, str]:
        """(body, content type, filename) for a downloadable export."""
        chosen = (export_format or "geojson").lower()
        collection = self.geojson()
        if chosen == "kml":
            return to_kml(collection), "application/vnd.google-earth.kml+xml", "p25_survey.kml"
        if chosen == "gpx":
            plan = self.plan().get("plan", {})
            visited = [
                {
                    "survey_run_id": stop["survey_run_id"],
                    "latitude": stop["gps_latitude"],
                    "longitude": stop["gps_longitude"],
                }
                for stop in self.stops()
            ]
            return to_gpx(plan, visited=visited), "application/gpx+xml", "p25_survey.gpx"
        if chosen in ("geojson", "json"):
            return json.dumps(collection, indent=2), "application/geo+json", "p25_survey.geojson"
        raise ValueError(f"unsupported export format: {export_format}")

    # -- jobs --------------------------------------------------------------

    def start_capture(self, payload: dict[str, Any]) -> Job:
        if not self.settings.allow_capture:
            raise RuntimeError("captures are disabled on this server (--no-capture)")

        # Checked FIRST, ahead of the SDR probe below. A second request while a
        # capture is running would otherwise reach that probe, which enumerates
        # a device the running capture already holds and reports it as missing
        # -- so a duplicate tap on the Record button was answered with an SDR
        # fault, sending the operator to diagnose hardware that was working.
        # JobRegistry.submit() keeps its own check: that one is atomic and is
        # what actually makes two concurrent captures impossible. This one only
        # makes the answer honest and cheap.
        running = self.jobs.active_job()
        if running is not None:
            raise RuntimeError(
                f"a {running.kind} job is already running "
                f"({running.stage or 'starting'}: {running.message or 'no detail yet'}); "
                "wait for it to finish or cancel it first"
            )

        position = self.get_position()
        if position.get("latitude") is None:
            raise ValueError(
                "mark your position on the map before recording; a recording with no "
                "position cannot contribute to geolocation"
            )
        # A stop recorded against the previous stop's coordinates is the one
        # mistake that silently corrupts a whole campaign: everything looks
        # fine and the posterior is simply wrong. So a position older than the
        # staleness window has to be confirmed before it is used again.
        age = self.position_age_seconds()
        if (
            age is not None
            and age > self.settings.position_stale_after_seconds
            and not payload.get("confirm_position")
        ):
            raise PositionStale(
                f"the marked position is {age / 60:.0f} minutes old "
                f"({position.get('label') or 'unnamed'}); confirm it is still where you are"
            )


        moment = datetime.now(UTC)
        label = str(payload.get("label") or position.get("label") or "").strip()
        stop_id = slugify(label, f"stop_{moment.strftime('%Y%m%d_%H%M%S')}")
        run_id = f"{moment.strftime('%Y%m%d_%H%M%S')}_{stop_id}"
        capture = CaptureSettings(
            center_frequency_hz=float(
                payload.get("center_frequency_hz", self.settings.center_frequency_hz)
            ),
            sample_rate_hz=float(payload.get("sample_rate_hz", self.settings.sample_rate_hz)),
            duration_seconds=float(
                payload.get("duration_seconds", self.settings.duration_seconds)
            ),
            if_gain_reduction_db=float(
                payload.get("if_gain_reduction_db", self.settings.if_gain_reduction_db)
            ),
            lna_state=int(payload.get("lna_state", self.settings.lna_state)),
            agc=False,
            driver=self.settings.driver,
        )
        capture.validate()

        # Storage is checked BEFORE the SDR is opened. Filling the medium
        # mid-capture costs the stop and can leave the recording unreadable.
        space = disk_status(
            self.settings.recordings_dir,
            sample_rate_hz=capture.sample_rate_hz,
            duration_seconds=capture.duration_seconds,
            keep_recordings=self.settings.keep_recordings,
        )
        if not space.ready:
            raise RuntimeError(space.reason)

        # Probed after the cheap preconditions, so a full card is reported as
        # a full card rather than being masked by an SDR message, and before
        # the job is accepted, so a missing or busy device is an immediate
        # answer rather than a job that starts and then fails.
        probe = probe_soapysdr(self.settings.driver)
        if not probe.available:
            raise RuntimeError(probe.probe_error or "no SDR device available")

        # Profiles are resolved NOW, not inside the job: a typo or a wrong
        # working directory would otherwise surface only after the full
        # capture had already been paid for.
        band = str(payload.get("band") or self.settings.band)
        try:
            resolve_band_profile(band, base_dir=self.settings.profile_base_dir)
            site_profile = resolve_site_profile(
                self.settings.site_profile, base_dir=self.settings.profile_base_dir
            )
        except (ProfileError, FileNotFoundError, OSError) as exc:
            raise ValueError(f"profile could not be resolved: {exc}") from exc

        # Record the gain actually applied at this stop, not the profile's
        # placeholder. Cross-stop comparability is the method's foundation, so
        # the number it depends on has to be stored per stop to be checkable.
        site_profile = replace(
            site_profile,
            gain=capture.if_gain_reduction_db,
            gain_mode="agc" if capture.agc else "manual",
            lna_state=capture.lna_state,
        )
        solve = bool(payload.get("solve", self.settings.solve_after_capture))

        def work(job: Job) -> dict[str, Any]:
            return self._capture_and_analyse(
                job,
                capture=capture,
                band=band,
                site_profile=site_profile,
                run_id=run_id,
                stop_id=stop_id,
                label=label,
                position=position,
                solve=solve,
            )

        return self.jobs.submit(
            kind="capture",
            label=f"{capture.duration_seconds:.0f}s at {capture.center_frequency_hz / 1e6:.4f} MHz",
            work=work,
        )

    def _capture_and_analyse(
        self,
        job: Job,
        *,
        capture: CaptureSettings,
        band: str,
        site_profile: SiteProfile,
        run_id: str,
        stop_id: str,
        label: str,
        position: dict[str, Any],
        solve: bool,
    ) -> dict[str, Any]:
        job.emit("device", "opening the SDR", progress=0.01)
        probe = probe_soapysdr(capture.driver)
        if not probe.available:
            raise RuntimeError(probe.probe_error or "no SDR device available")

        recordings = Path(self.settings.recordings_dir).expanduser().resolve()
        started = time.time()

        def emit_capture_progress(frames: int, target: int, elapsed: float) -> None:
            fraction = frames / target if target else 0.0
            # Capture is the long pole, so it owns most of the bar; the
            # analysis stages that follow are quick by comparison.
            job.emit(
                "capture",
                f"recording {fraction * 100:.0f}% ({elapsed:.0f}s of "
                f"{capture.duration_seconds:.0f}s)",
                progress=0.02 + 0.58 * fraction,
                extra={"frames": frames, "target_frames": target},
            )

        throttled_emit = throttle_capture_progress(emit_capture_progress)

        def on_progress(frames: int, target: int, elapsed: float) -> None:
            # Cancellation must stay responsive on every chunk; only the
            # expensive part (job.emit -- see throttle_capture_progress) is
            # throttled.
            job.check_cancelled()
            throttled_emit(frames, target, elapsed)

        job.emit("capture", "recording", progress=0.02)
        manifest = run_capture(
            recordings, settings=capture, on_progress=on_progress, filename=f"{run_id}.wav"
        )
        job.check_cancelled()
        if manifest["timed_out"]:
            job.emit(
                "capture",
                "the device delivered samples slower than real time; the recording is short",
            )
        if manifest["overflow_count"]:
            job.emit(
                "capture",
                f"{manifest['overflow_count']} driver overflow(s): the recording has gaps",
            )

        job.emit("survey", "discovering RF observations", progress=0.62)
        survey = run_survey(
            manifest["wav_path"],
            Path(self.settings.output_root).expanduser().resolve() / run_id,
            band=band,
            site=site_profile,
            run_id=run_id,
            database_path=self.settings.database_path,
            profile_base_dir=self.settings.profile_base_dir,
            gps_latitude=position["latitude"],
            gps_longitude=position["longitude"],
            gps_accuracy_m=position.get("accuracy_m"),
            gps_source=position.get("source", "user"),
            gps_fetched_at_utc=position.get("set_at"),
            site_id_override=stop_id,
            site_label_override=label or stop_id,
            drive_view=(
                DriveViewSettings(
                    fft_size=self.settings.live_fft_size,
                    frames_per_window=self.settings.live_frames_per_window,
                    window_seconds=self.settings.live_window_seconds,
                )
                if self.settings.drive_view_for_stops
                else None
            ),
        )
        job.check_cancelled()
        view = survey.get("drive_view")
        job.emit(
            "survey",
            f"{survey['observation_count']} observation(s), coverage {survey['coverage_status']}"
            + (
                f"; drive view heard {view['observation_count']} over {view['windows']} window(s)"
                if view
                else ""
            ),
            progress=0.82,
        )

        # A materially short capture cannot be allowed to contribute. Weak
        # signals it never had time to detect would arrive as non-detections,
        # and a non-detection actively pushes a site away from this stop.
        completeness = (
            manifest["actual_duration_seconds"] / capture.duration_seconds
            if capture.duration_seconds
            else 0.0
        )
        # Gaps are judged by how much time they cost, not by how many times
        # the driver dropped its FIFO. Those are very different numbers: one
        # overflow in a 30 s capture discards a buffer measured in
        # milliseconds. Counting events instead of duration threw away a
        # whole field stop -- with the new position the campaign most needed
        # -- over a single overflow, treating it exactly like a recording
        # with twenty-seven.
        coverage = float(manifest.get("time_coverage", 1.0))
        gap_seconds = float(manifest.get("gap_seconds", 0.0))
        integrity_reason = ""
        if completeness < self.settings.min_capture_completeness:
            integrity_reason = (
                f"capture delivered {completeness * 100:.0f}% of the requested "
                f"{capture.duration_seconds:.0f} s; too short to trust a non-detection"
            )
        elif coverage < self.settings.min_time_coverage:
            integrity_reason = (
                f"{gap_seconds:.1f} s of the recorded span is missing "
                f"({coverage * 100:.1f}% covered, {manifest['overflow_count']} driver "
                "overflow(s)); a signal absent from this stop may simply have been in a gap"
            )
        if integrity_reason:
            connection = connect_geo_database(Path(self.settings.database_path))
            try:
                # Scoped to non-detections. Both reasons above are about gaps
                # in the recording, and a gap can hide a signal but cannot
                # invent one -- so what was heard here is still evidence.
                # Barring the whole run instead cost two real field stops:
                # 90 s and 60 s over the full band, 25 and 19 observations,
                # discarded entirely for 23 and 27 overflows.
                exclude_run(
                    connection,
                    run_id,
                    integrity_reason,
                    scope=EXCLUSION_SCOPE_NON_DETECTIONS,
                )
            finally:
                connection.close()
            job.emit(
                "measurements",
                "non-detections from this stop excluded from geolocation: " + integrity_reason,
            )

        job.emit("measurements", "matching observations against the site registry", progress=0.85)
        measurements = materialise_measurements(
            database_path=self.settings.database_path,
            run_ids=[run_id],
            settings=MeasurementSettings(),
        )
        summary = measurements["summary"]
        job.emit(
            "measurements",
            f"{summary['detections']} detection(s), {summary['non_detections']} non-detection(s), "
            f"{summary['not_covered']} outside the measured passband",
            progress=0.88,
        )

        # The recording has done its job: the observations are in SQLite. Only
        # now, and only after a SUCCESSFUL survey, is it eligible for deletion.
        retention = enforce_retention(
            self.settings.recordings_dir,
            keep=self.settings.keep_recordings,
            reason=f"survey {run_id} completed",
        )
        if retention["deleted_count"]:
            job.emit(
                "measurements",
                f"freed {retention['freed_gib']:.2f} GiB by discarding "
                f"{retention['deleted_count']} analysed recording(s)",
            )

        solve_report: dict[str, Any] | None = None
        if solve:
            job.check_cancelled()
            job.emit("solve", "re-solving every site with the new evidence", progress=0.90)
            solve_report = self._solve(
                job, progress_from=0.90, progress_to=0.99, settings=self.field_solve_settings()
            )

        return {
            "run_id": run_id,
            "recording": manifest["wav_path"],
            "capture": {
                "complete": manifest["complete"],
                "timed_out": manifest["timed_out"],
                "overflow_count": manifest["overflow_count"],
                "actual_duration_seconds": manifest["actual_duration_seconds"],
            },
            "survey": survey,
            "measurements": summary,
            "excluded_from_geolocation": integrity_reason or None,
            "retention": retention,
            "disk": self.disk(),
            "solve": (
                {
                    "solve_batch_id": solve_report["solve_batch_id"],
                    "solutions": solve_report["solutions"],
                }
                if solve_report
                else None
            ),
            "elapsed_seconds": time.time() - started,
        }

    def start_analysis(self, payload: dict[str, Any]) -> Job:
        """Run the survey and geolocation chain on a recording already on disk.

        Also the way the whole chain can be exercised without an SDR
        attached, which matters because the field app must be verifiable
        before anyone drives anywhere with it.
        """
        recording = Path(str(payload.get("recording", ""))).expanduser()
        if not recording.is_file():
            raise ValueError(f"no such recording: {recording}")
        position = self.get_position()
        if position.get("latitude") is None:
            raise ValueError("mark your position on the map before analysing a recording")
        moment = datetime.now(UTC)
        label = str(payload.get("label") or position.get("label") or "").strip()
        stop_id = slugify(label, f"stop_{moment.strftime('%Y%m%d_%H%M%S')}")
        run_id = str(payload.get("run_id") or f"{moment.strftime('%Y%m%d_%H%M%S')}_{stop_id}")
        band = str(payload.get("band") or self.settings.band)
        solve = bool(payload.get("solve", self.settings.solve_after_capture))

        def work(job: Job) -> dict[str, Any]:
            job.emit("survey", "discovering RF observations", progress=0.1)
            survey = run_survey(
                recording,
                Path(self.settings.output_root).expanduser().resolve() / run_id,
                band=band,
                site=self.settings.site_profile,
                run_id=run_id,
                database_path=self.settings.database_path,
                profile_base_dir=self.settings.profile_base_dir,
                gps_latitude=position["latitude"],
                gps_longitude=position["longitude"],
                gps_accuracy_m=position.get("accuracy_m"),
                gps_source=position.get("source", "user"),
                gps_fetched_at_utc=position.get("set_at"),
                site_id_override=stop_id,
                site_label_override=label or stop_id,
            )
            job.check_cancelled()
            job.emit("measurements", "matching against the site registry", progress=0.7)
            measurements = materialise_measurements(
                database_path=self.settings.database_path, run_ids=[run_id]
            )
            solve_report = None
            if solve:
                job.emit("solve", "re-solving every site", progress=0.8)
                solve_report = self._solve(
                    job, progress_from=0.8, progress_to=0.99, settings=self.field_solve_settings()
                )
            return {
                "run_id": run_id,
                "survey": survey,
                "measurements": measurements["summary"],
                "solve": (
                    {
                        "solve_batch_id": solve_report["solve_batch_id"],
                        "solutions": solve_report["solutions"],
                    }
                    if solve_report
                    else None
                ),
            }

        return self.jobs.submit(kind="analyse", label=f"analyse {recording.name}", work=work)

    # -- live (moving) survey ---------------------------------------------

    def push_live_position(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Accept one fix from the phone's GPS.

        Held in memory rather than written to `position.json`: fixes arrive
        about once a second, and the marked position is a deliberate act the
        operator performs for a stationary stop. Conflating the two would
        overwrite that mark hundreds of times a drive.

        The timestamp is taken HERE, on the receiving side, from the same
        monotonic clock the live session reads. A phone's own clock can be
        wrong by hours, and staleness -- the one check that stops a
        measurement being placed where the receiver used to be -- has to be
        measured against something that cannot jump.
        """
        try:
            latitude = float(payload["latitude"])
            longitude = float(payload["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("latitude and longitude are required numbers") from exc
        if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
            raise ValueError("latitude or longitude is out of range")
        accuracy = payload.get("accuracy_m")
        fix = Position(
            latitude=latitude,
            longitude=longitude,
            accuracy_m=float(accuracy) if accuracy is not None else None,
            at=time.monotonic(),
        )
        point = {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_m": fix.accuracy_m,
            "speed_mps": (
                float(payload["speed_mps"]) if payload.get("speed_mps") is not None else None
            ),
            "at": datetime.now(UTC).isoformat(),
        }
        with self._live_lock:
            self._live_fix = fix
            self._live_fix_wall = point["at"]
            self._live_fix_count += 1
            self._live_trail.append(point)
            if len(self._live_trail) > _LIVE_TRAIL_LIMIT:
                del self._live_trail[: len(self._live_trail) - _LIVE_TRAIL_LIMIT]
            running = self._live_job_id
            bins = len(self._live_bins)
        return {"accepted": True, "fix_count": self._live_fix_count,
                "drive_running": running is not None, "bins": bins}

    def _live_position(self) -> Position | None:
        with self._live_lock:
            return self._live_fix

    def live_position_age_seconds(self) -> float | None:
        with self._live_lock:
            fix = self._live_fix
        return None if fix is None else max(0.0, time.monotonic() - fix.at)

    def live_settings(self, payload: dict[str, Any] | None = None) -> LiveSettings:
        """The live configuration actually in force, overrides applied."""
        given = payload or {}
        anchor = self.settings.live_anchor or self.settings.map_center
        settings = LiveSettings(
            band=str(given.get("band") or self.settings.band),
            site_id=self.settings.site_profile,
            center_frequency_hz=float(
                given.get("center_frequency_hz", self.settings.center_frequency_hz)
            ),
            sample_rate_hz=float(given.get("sample_rate_hz", self.settings.sample_rate_hz)),
            if_gain_reduction_db=float(
                given.get("if_gain_reduction_db", self.settings.if_gain_reduction_db)
            ),
            lna_state=int(given.get("lna_state", self.settings.lna_state)),
            driver=self.settings.driver,
            window_seconds=float(given.get("window_seconds", self.settings.live_window_seconds)),
            bin_size_m=float(given.get("bin_size_m", self.settings.live_bin_size_m)),
            grid_anchor_latitude=float(given.get("anchor_latitude", anchor[0])),
            grid_anchor_longitude=float(given.get("anchor_longitude", anchor[1])),
            min_windows_per_bin=int(
                given.get("min_windows_per_bin", self.settings.live_min_windows_per_bin)
            ),
            max_windows_per_bin=int(
                given.get("max_windows_per_bin", self.settings.live_max_windows_per_bin)
            ),
            max_position_age_seconds=float(
                given.get("max_position_age_seconds", self.settings.live_max_position_age_seconds)
            ),
            adaptive_bin_size=bool(
                given.get("adaptive_bin_size", self.settings.live_adaptive_bins)
            ),
            fft_size=int(given.get("fft_size", self.settings.live_fft_size)),
            frames_per_window=int(
                given.get("frames_per_window", self.settings.live_frames_per_window)
            ),
        )
        settings.validate()
        return settings

    def live_status(self) -> dict[str, Any]:
        job = self.jobs.get(self._live_job_id) if self._live_job_id else None
        if job is None or job.is_terminal():
            # The registry, not the remembered id, is the authority on what is
            # running. The id is recorded after `submit` has already started
            # the thread, so the first bin of a drive can land before it is
            # set -- and the phone would be told, for that moment, that the
            # drive it just started is not running.
            active = self.jobs.active_job()
            if active is not None and active.kind == "live":
                job = active
                self._live_job_id = active.job_id
        running = job is not None and not job.is_terminal()
        with self._live_lock:
            payload = {
                "running": running,
                "job_id": self._live_job_id,
                "stats": dict(self._live_stats),
                "bins": list(self._live_bins[-_LIVE_BIN_LIMIT:]),
                "bin_count": len(self._live_bins),
                "trail": list(self._live_trail[-_LIVE_TRAIL_LIMIT:]),
                "fix_count": self._live_fix_count,
                "last_fix_utc": self._live_fix_wall,
                "solving": self._live_solving,
                "last_solve": self._live_last_solve,
            }
            session = self._live_session if running else None
            payload["hold"] = {
                "active": bool(session is not None and session.stats.hold_active),
                "seconds_left": round(session.stats.hold_seconds_left, 1) if session else 0.0,
                "requested": self._live_hold_request is not None,
                "holds_written": session.stats.holds_written if session else 0,
            }
            payload["pull_over"] = self._pull_over_hint(running)
        payload["position_age_seconds"] = self.live_position_age_seconds()
        payload["job"] = job.snapshot() if job is not None else None
        return payload

    def _pull_over_hint(self, running: bool) -> dict[str, Any]:
        """Whether the last bin suggests a longer, stationary measurement here.

        A drive bin integrates for ten seconds; a stop can integrate for
        sixty. A channel that missed the detection gate by a little in most
        of a bin's windows is one that a stop would probably turn into a
        detection -- and if that channel is a registry site's control channel,
        the stop is worth making. Said as a hint, never acted on: the operator
        decides whether this is a place a car can stop.
        """
        empty = {"suggest": False, "sites": [], "channels": 0, "bin": None, "reason": ""}
        if not running or not self._live_bins:
            return empty
        latest = self._live_bins[-1]
        if latest.get("kind") == "hold" or (self._live_session and self._live_session.stats.hold_active):
            return {**empty, "reason": "a stationary measurement was just taken here"}
        near = latest.get("near_threshold") or []
        if not near:
            return empty
        sites: dict[str, float] = {}
        for entry in near:
            frequency = float(entry["frequency_hz"])
            for cc_hz, site_key in self._live_cc_index:
                if abs(cc_hz - frequency) <= self._live_cc_tolerance_hz:
                    sites[site_key] = max(sites.get(site_key, -99.0), float(entry["p95_snr_db"]))
        if not sites:
            return {**empty, "channels": len(near),
                    "reason": f"{len(near)} channel(s) near threshold, none a registry control channel"}
        return {
            "suggest": True,
            "sites": [{"site_key": key, "p95_snr_db": db} for key, db in sorted(sites.items())],
            "channels": len(near),
            "bin": latest.get("survey_run_id"),
            "reason": (
                f"{len(sites)} registry site(s) sat just under the detection gate in the last bin; "
                "a 60 s stationary measurement here would probably hear them"
            ),
        }

    def request_live_hold(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask the running drive to stop binning and integrate here.

        Bounded to 10-300 s: shorter than ten is no better than the drive bin
        it replaces, longer than five minutes is a stationary stop that should
        be recorded as one. Takes effect at the next window boundary, so it is
        never more than a second late.
        """
        job = self.jobs.active_job()
        if job is None or job.kind != "live":
            raise ValueError("no drive is running; a hold only makes sense mid-drive")
        seconds = float(payload.get("seconds", 60.0) or 60.0)
        seconds = max(10.0, min(300.0, seconds))
        with self._live_lock:
            session = self._live_session
            if session is not None and session.stats.hold_active:
                return {"accepted": False, "seconds": seconds, "reason": "a hold is already running"}
            self._live_hold_request = seconds
        return {"accepted": True, "seconds": seconds, "reason": ""}

    def _take_hold_request(self) -> float | None:
        with self._live_lock:
            wanted, self._live_hold_request = self._live_hold_request, None
        return wanted

    def start_live(self, payload: dict[str, Any]) -> Job:
        """Begin a moving survey. Nothing is recorded; bins are written as
        they complete, so an interrupted drive has already contributed
        everything it measured."""
        if not self.settings.allow_capture:
            raise RuntimeError("captures are disabled on this server (--no-capture)")
        running = self.jobs.active_job()
        if running is not None:
            raise RuntimeError(
                f"a {running.kind} job is already running "
                f"({running.stage or 'starting'}: {running.message or 'no detail yet'}); "
                "wait for it to finish or cancel it first"
            )

        # A drive with no GPS is not a degraded drive, it is no drive at all:
        # every window would be dropped for want of a position. Refused here,
        # before the SDR is opened, rather than discovered as a session that
        # ran for ten minutes and wrote nothing.
        age = self.live_position_age_seconds()
        limit = self.settings.live_max_position_age_seconds
        if age is None:
            raise ValueError(
                "no GPS fix has arrived yet; start location sharing in the browser first "
                "(it needs HTTPS -- run the app with --tls)"
            )
        if age > limit * 2:
            raise ValueError(
                f"the last GPS fix is {age:.0f} s old, older than the {limit:.0f} s a window "
                "may be tagged with; check location sharing is still on"
            )

        live = self.live_settings(payload)
        probe = probe_soapysdr(self.settings.driver)
        if not probe.available:
            raise RuntimeError(probe.probe_error or "no SDR device available")
        try:
            band = resolve_band_profile(live.band, base_dir=self.settings.profile_base_dir)
            site_profile = resolve_site_profile(
                self.settings.site_profile, base_dir=self.settings.profile_base_dir
            )
        except (ProfileError, FileNotFoundError, OSError) as exc:
            raise ValueError(f"profile could not be resolved: {exc}") from exc

        max_seconds = float(payload.get("max_seconds") or self.settings.live_max_seconds)
        # Zero means "only when asked". Kept as zero rather than clamped to
        # one, or the default would silently become "solve after every bin".
        solve_every = max(0, int(
            payload.get("solve_every_bins", self.settings.live_solve_every_bins) or 0
        ))
        session_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

        # Cleared BEFORE the job is submitted, because submitting starts the
        # thread: clearing afterwards could wipe the first bins of the drive
        # that is already running.
        # Registry control channels for naming near-threshold hints. Read once
        # here: the registry does not change mid-drive and the phone polls
        # status every couple of seconds.
        registry = connect_geo_database(Path(self.settings.database_path))
        try:
            cc_index = [
                (float(channel["frequency_hz"]), str(site_row["site_key"]))
                for site_row in list_sites(registry)
                for channel in site_row.get("channels", [])
            ]
        finally:
            registry.close()

        with self._live_lock:
            self._live_bins = []
            self._live_pending_runs = []
            self._live_stats = {}
            self._live_last_solve = None
            self._live_hold_request = None
            self._live_cc_index = cc_index
            self._live_cc_tolerance_hz = float(band.comparison.frequency_tolerance_hz)

        def work(job: Job) -> dict[str, Any]:
            return self._run_live(
                job,
                live=live,
                band=band,
                site_profile=site_profile,
                session_id=session_id,
                max_seconds=max_seconds,
                solve_every=solve_every,
            )

        job = self.jobs.submit(
            kind="live",
            label=(
                f"live drive at {live.center_frequency_hz / 1e6:.4f} MHz, "
                f"{live.bin_size_m:.0f} m bins"
            ),
            work=work,
        )
        self._live_job_id = job.job_id
        return job

    def _run_live(
        self,
        job: Job,
        *,
        live: LiveSettings,
        band: Any,
        site_profile: SiteProfile,
        session_id: str,
        max_seconds: float,
        solve_every: int,
    ) -> dict[str, Any]:
        started = time.monotonic()
        job.emit(
            "live",
            f"streaming at {live.sample_rate_hz / 1e6:.2f} MS/s; drive on, "
            f"{live.bin_size_m:.0f} m bins",
            progress=0.02,
        )

        def on_bin(info: dict[str, Any]) -> None:
            with self._live_lock:
                self._live_bins.append(info)
                self._live_pending_runs.append(info["survey_run_id"])
                written = len(self._live_bins)
            job.emit(
                "bin",
                f"bin {written} written at {info['latitude']:.5f}, {info['longitude']:.5f} "
                f"({info['observations']} observation(s))",
                progress=min(0.95, 0.02 + written / 200.0),
                extra={"bin": info},
            )
            if solve_every and written % solve_every == 0:
                self._start_live_solve(job)

        def stop() -> bool:
            if job.cancelled:
                return True
            return time.monotonic() - started >= max_seconds

        connection = connect_geo_database(Path(self.settings.database_path))
        # A background solve holds the write lock for short bursts while the
        # drive keeps writing bins. Five seconds -- the sqlite3 default --
        # is enough for that in principle and not enough to be sure; losing
        # the whole drive to one collision is not a trade worth taking.
        connection.execute("PRAGMA busy_timeout = 30000")
        session = LiveSession(
            session_id=session_id,
            settings=live,
            band=band,
            site=site_profile,
            database_path=self.settings.database_path,
            position_provider=self._live_position,
            on_bin=on_bin,
            hold_provider=self._take_hold_request,
        )
        with self._live_lock:
            self._live_session = session
        try:
            stats = session.run(stop=stop, connection=connection)
        finally:
            connection.close()
            with self._live_lock:
                self._live_session = None

        with self._live_lock:
            self._live_stats = stats.to_dict()
        job.emit(
            "live",
            f"drive finished: {stats.bins_written} bin(s) from "
            f"{stats.windows_recorded} window(s)",
            progress=0.96,
        )
        # The final solve runs in the job, not on a side thread: the stream
        # has stopped, so there is nothing left to starve, and the operator
        # is waiting to see the regions the drive just bought.
        pending = self._take_pending_runs()
        if pending:
            materialise_measurements(
                database_path=self.settings.database_path, run_ids=pending
            )
        report = self._solve(
            job, progress_from=0.96, progress_to=0.99,
            settings=self.field_solve_settings(),
        )
        return {
            "session_id": session_id,
            "live": stats.to_dict(),
            "settings": live.to_dict(),
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "solutions": len(report["solutions"]),
        }

    def _take_pending_runs(self) -> list[str]:
        with self._live_lock:
            pending, self._live_pending_runs = self._live_pending_runs, []
        return pending

    def request_live_solve(self) -> dict[str, Any]:
        """Solve now, on the operator's word, while the drive keeps running.

        The counterpart to `live_solve_every_bins = 0`: bins are written
        continuously whatever happens here, and this only decides when the
        map catches up with them.
        """
        job = self.jobs.active_job()
        if job is None or job.kind != "live":
            raise ValueError(
                "no drive is running; use the ordinary re-solve, which reads the same "
                "measurements"
            )
        with self._live_lock:
            already = self._live_solving
        if already:
            return {"started": False, "reason": "a solve is already running"}
        self._start_live_solve(job)
        return {"started": True, "reason": ""}

    def _start_live_solve(self, job: Job) -> None:
        """Re-solve on a side thread while the drive keeps streaming.

        Not inline: a field solve across a campaign's sites is seconds to
        minutes, and the SDR is not being read for any of it. A drive that
        paused to solve would lose that stretch of road outright. Overflows
        during the solve cost windows instead, which the binning absorbs --
        and a window stretched past its travel limit is dropped rather than
        smeared, so the failure mode is fewer bins, never misplaced ones.
        """
        with self._live_lock:
            if self._live_solving:
                # A solve slower than the interval must not stack; skipping
                # is right because the next one will include these bins too.
                return
            self._live_solving = True
        pending = self._take_pending_runs()

        def run() -> None:
            try:
                if pending:
                    materialise_measurements(
                        database_path=self.settings.database_path, run_ids=pending
                    )
                report = solve_all_sites(
                    database_path=self.settings.database_path,
                    output_root=self.settings.output_root,
                    settings=self.field_solve_settings(),
                )
                solved = sum(1 for row in report["solutions"] if row["status"] == "ok")
                with self._live_lock:
                    self._live_last_solve = {
                        "at": datetime.now(UTC).isoformat(),
                        "solved": solved,
                        "sites": len(report["solutions"]),
                    }
                job.emit(
                    "solve",
                    f"{solved} of {len(report['solutions'])} site(s) bounded",
                    extra={"live_solve": True},
                )
            except Exception as exc:  # noqa: BLE001 - a failed solve must not end the drive
                # The drive is the irreplaceable part: it is happening on a
                # road the operator is on now. A solve can be repeated at any
                # time from the same database, so its failure is reported and
                # the streaming continues.
                job.emit(
                    "solve",
                    f"background solve failed ({type(exc).__name__}: {exc}); "
                    "the drive continues and the bins are kept",
                    extra={"live_solve": True, "failed": True},
                )
            finally:
                with self._live_lock:
                    self._live_solving = False

        threading.Thread(target=run, name="live-solve", daemon=True).start()

    def field_solve_settings(self) -> SolveSettings:
        """Solve settings for the in-field pass: same model, coarser grid."""
        return SolveSettings(
            resolution_m=self.settings.solve_resolution_m,
            coarse_resolution_m=max(self.settings.solve_resolution_m * 3.0, 750.0),
            max_coarse_cells=self.settings.solve_max_cells,
            max_fine_cells=self.settings.solve_max_cells,
            target_fine_cells=max(self.settings.solve_max_cells // 3, 2_000),
        )

    def start_solve(self, payload: dict[str, Any]) -> Job:
        rebuild = bool(payload.get("rebuild_measurements", False))
        # Read defaults off an instance, not the class: this dataclass uses
        # slots, so the class attributes are slot descriptors rather than
        # the default values.
        base = self.field_solve_settings() if payload.get("field", True) else SolveSettings()
        settings = SolveSettings(
            sigma_db=float(payload.get("sigma_db") or base.sigma_db),
            min_detections=int(payload.get("min_detections") or base.min_detections),
            resolution_m=base.resolution_m,
            coarse_resolution_m=base.coarse_resolution_m,
            max_coarse_cells=base.max_coarse_cells,
            max_fine_cells=base.max_fine_cells,
            target_fine_cells=base.target_fine_cells,
        )

        def work(job: Job) -> dict[str, Any]:
            if rebuild:
                job.emit("measurements", "rebuilding every run's measurements", progress=0.05)
                materialise_measurements(database_path=self.settings.database_path)
            return self._solve(job, progress_from=0.1, progress_to=0.99, settings=settings)

        return self.jobs.submit(kind="solve", label="re-solve all sites", work=work)

    def _solve(
        self,
        job: Job,
        *,
        progress_from: float,
        progress_to: float,
        settings: SolveSettings | None = None,
    ) -> dict[str, Any]:
        span = progress_to - progress_from

        def on_progress(site_key: str, index: int, total: int) -> None:
            job.check_cancelled()
            job.emit(
                "solve",
                f"solving {site_key} ({index + 1} of {total})",
                progress=progress_from + span * (index / max(total, 1)),
            )

        report = solve_all_sites(
            database_path=self.settings.database_path,
            output_root=self.settings.output_root,
            settings=settings,
            on_progress=on_progress,
        )
        solved = sum(1 for row in report["solutions"] if row["status"] == "ok")
        job.emit(
            "solve",
            f"{solved} of {len(report['solutions'])} site(s) produced a bounded region",
            progress=progress_to,
        )
        return report


__all__ = [
    "FieldService",
    "FieldSettings",
    "PositionStale",
    "slugify",
    "throttle_capture_progress",
]
