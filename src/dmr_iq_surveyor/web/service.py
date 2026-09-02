"""Field service layer: the operations the web app exposes.

Every operation here is a thin composition of code that already exists and
is tested elsewhere -- `capture.run_capture`, `survey.run_survey`,
`geo.pipeline` -- so the web app cannot drift into a second, differently
behaved implementation of the pipeline.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.capture.core import CaptureSettings, run_capture
from dmr_iq_surveyor.capture.device import probe_soapysdr
from dmr_iq_surveyor.geo.measurements import MeasurementSettings
from dmr_iq_surveyor.geo.model import SolveSettings
from dmr_iq_surveyor.geo.pipeline import (
    build_map_geojson,
    materialise_measurements,
    site_overview,
    solve_all_sites,
)
from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.survey.pipeline import DEFAULT_DATABASE_PATH, run_survey
from dmr_iq_surveyor.web.jobs import Job, JobRegistry

_SLUG = re.compile(r"[^a-z0-9]+")

POSITION_SOURCE_BROWSER = "browser_gps"
POSITION_SOURCE_MANUAL = "user"


def slugify(value: str, fallback: str) -> str:
    slug = _SLUG.sub("_", value.strip().lower()).strip("_")
    return slug or fallback


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
    duration_seconds: float = 120.0
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
        payload["tool_version"] = __version__
        return payload


class FieldService:
    """Stateful glue between HTTP requests and the analysis pipeline."""

    def __init__(self, settings: FieldSettings) -> None:
        self.settings = settings
        self.jobs = JobRegistry()
        self._position_path = Path(settings.output_root).expanduser().resolve() / "position.json"

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

    def state(self) -> dict[str, Any]:
        return {
            "settings": self.settings.to_public_dict(),
            "position": self.get_position(),
            "device": self.device_probe(),
            "sites": site_overview(database_path=self.settings.database_path),
            "runs": self.survey_runs(),
            "jobs": self.jobs.list(),
        }

    def geojson(self) -> dict[str, Any]:
        return build_map_geojson(database_path=self.settings.database_path)

    # -- jobs --------------------------------------------------------------

    def start_capture(self, payload: dict[str, Any]) -> Job:
        if not self.settings.allow_capture:
            raise RuntimeError("captures are disabled on this server (--no-capture)")
        position = self.get_position()
        if position.get("latitude") is None:
            raise ValueError(
                "mark your position on the map before recording; a recording with no "
                "position cannot contribute to geolocation"
            )
        # Probed before accepting the job so a missing or busy SDR is an
        # immediate, actionable answer rather than a job that starts, looks
        # like it is running, and then fails.
        probe = probe_soapysdr(self.settings.driver)
        if not probe.available:
            raise RuntimeError(probe.probe_error or "no SDR device available")

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
        band = str(payload.get("band") or self.settings.band)
        solve = bool(payload.get("solve", self.settings.solve_after_capture))

        def work(job: Job) -> dict[str, Any]:
            return self._capture_and_analyse(
                job,
                capture=capture,
                band=band,
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

        def on_progress(frames: int, target: int, elapsed: float) -> None:
            job.check_cancelled()
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
        job.emit(
            "survey",
            f"{survey['observation_count']} observation(s), coverage {survey['coverage_status']}",
            progress=0.82,
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


__all__ = ["FieldService", "FieldSettings", "slugify"]
