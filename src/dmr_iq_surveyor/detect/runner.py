from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dmr_iq_surveyor.detect.core import DetectionSettings
from dmr_iq_surveyor.detect.features import detect_spectrum
from dmr_iq_surveyor.detect.merge import merge_recordings
from dmr_iq_surveyor.detect.output import write_detection_outputs


def _settings_from_config(
    raw: dict[str, Any] | None,
) -> DetectionSettings:
    raw = raw or {}
    allowed = set(DetectionSettings.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(
            f"Unknown detection settings: {sorted(unknown)}"
        )
    settings = DetectionSettings(**raw)
    settings.validate()
    return settings


def run_detect(
    spectrum_dir: str | Path,
    output_dir: str | Path,
    settings: DetectionSettings | None = None,
    recording_id: str = "recording",
) -> dict[str, Any]:
    resolved = settings or DetectionSettings()
    result = detect_spectrum(spectrum_dir, resolved)
    candidates = merge_recordings(
        [(recording_id, result)],
        resolved,
    )
    summary = write_detection_outputs(
        output_dir,
        candidates,
        [(recording_id, result)],
        resolved,
    )
    return {"summary": summary, "candidates": candidates}


def run_detect_batch(config_path: str | Path) -> dict[str, Any]:
    config_file = Path(config_path).expanduser().resolve()
    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Batch configuration must be a mapping")
    project = (
        raw.get("project")
        if isinstance(raw.get("project"), dict)
        else {}
    )
    recordings = raw.get("recordings")
    if not isinstance(recordings, list) or not recordings:
        raise ValueError("Batch configuration requires recordings")
    output_root = Path(
        project.get("output_root", "runs/batch")
    ).expanduser().resolve()
    detection_raw = (
        raw.get("detection")
        if isinstance(raw.get("detection"), dict)
        else {}
    )
    settings = _settings_from_config(detection_raw)
    results: list[tuple[str, dict[str, Any]]] = []
    failures: list[dict[str, str]] = []
    for item in recordings:
        recording_id = str(
            item.get("id") or Path(str(item["path"])).stem
        )
        spectrum_dir = (
            output_root
            / "recordings"
            / recording_id
            / "spectrum"
        )
        try:
            result = detect_spectrum(spectrum_dir, settings)
        except Exception as exc:
            failures.append(
                {
                    "recording_id": recording_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        results.append((recording_id, result))

    candidates = merge_recordings(results, settings)
    summary = write_detection_outputs(
        output_root / "candidates",
        candidates,
        results,
        settings,
    )
    summary.update(
        {
            "successful_recordings": len(results),
            "failed_recordings": len(failures),
            "failures": failures,
        }
    )
    return summary
