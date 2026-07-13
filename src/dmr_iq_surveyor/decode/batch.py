from __future__ import annotations

import csv
import json
from dataclasses import fields
from pathlib import Path
from typing import Any

import yaml

from dmr_iq_surveyor.batch import BatchConfigError, load_batch_config
from dmr_iq_surveyor.decode.core import ExtractionSettings
from dmr_iq_surveyor.decode.dsd import (
    DecoderSettings,
    run_decoder_profiles,
)
from dmr_iq_surveyor.decode.extract import run_channel_extraction


def _dataclass_from_mapping(
    cls: type,
    payload: dict[str, Any] | None,
):
    payload = payload or {}
    allowed = {item.name for item in fields(cls)}
    unknown = set(payload) - allowed
    if unknown:
        raise BatchConfigError(
            f"Unknown {cls.__name__} settings: {sorted(unknown)}"
        )
    return cls(**payload)


def _load_phase4_config(config_path: str | Path) -> dict[str, Any]:
    config = load_batch_config(config_path)
    raw = yaml.safe_load(
        Path(config["config_path"]).read_text(encoding="utf-8")
    )
    extraction_raw = (
        raw.get("channel_extraction")
        if isinstance(raw.get("channel_extraction"), dict)
        else {}
    )
    decoder_raw = (
        raw.get("decoder")
        if isinstance(raw.get("decoder"), dict)
        else {}
    )
    phase4_raw = (
        raw.get("phase4")
        if isinstance(raw.get("phase4"), dict)
        else {}
    )
    config["extraction"] = _dataclass_from_mapping(
        ExtractionSettings,
        extraction_raw,
    )
    config["decoder"] = _dataclass_from_mapping(
        DecoderSettings,
        decoder_raw,
    )
    config["phase4"] = {
        "max_candidates": int(
            phase4_raw.get("max_candidates", 8)
        ),
        "candidate_classes": list(
            phase4_raw.get(
                "candidate_classes",
                ["dmr_like_narrowband"],
            )
        ),
        "minimum_confidence": float(
            phase4_raw.get("minimum_confidence", 0.0)
        ),
        "iq_hypotheses": list(
            phase4_raw.get("iq_hypotheses", ["IQ"])
        ),
        "run_decoder": bool(
            phase4_raw.get("run_decoder", True)
        ),
    }
    if config["phase4"]["max_candidates"] < 1:
        raise BatchConfigError(
            "phase4.max_candidates must be positive"
        )
    invalid_iq = set(
        config["phase4"]["iq_hypotheses"]
    ) - {"IQ", "QI"}
    if invalid_iq:
        raise BatchConfigError(
            f"Unsupported IQ hypotheses: {sorted(invalid_iq)}"
        )
    return config


def _candidate_frequency(
    candidate: dict[str, Any],
    iq_order: str,
) -> float:
    field = (
        "frequency_hz_assuming_iq"
        if iq_order == "IQ"
        else "frequency_hz_if_qi"
    )
    return float(candidate[field])


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_decode_batch(config_path: str | Path) -> dict[str, Any]:
    config = _load_phase4_config(config_path)
    output_root: Path = config["output_root"]
    candidates_path = output_root / "candidates" / "candidates.json"
    if not candidates_path.is_file():
        raise FileNotFoundError(candidates_path)
    candidates = json.loads(
        candidates_path.read_text(encoding="utf-8")
    )
    selected = [
        candidate
        for candidate in candidates
        if candidate["preliminary_class"]
        in config["phase4"]["candidate_classes"]
        and float(candidate["confidence"])
        >= config["phase4"]["minimum_confidence"]
    ]
    selected.sort(
        key=lambda item: float(item["confidence"]),
        reverse=True,
    )
    selected = selected[: config["phase4"]["max_candidates"]]
    recording_lookup = {
        recording.recording_id: recording
        for recording in config["recordings"]
    }
    rows: list[dict[str, Any]] = []

    for candidate in selected:
        candidate_id = str(candidate["candidate_id"])
        recording_ids = candidate.get("recording_ids") or list(
            recording_lookup
        )
        for recording_id in recording_ids:
            recording = recording_lookup.get(str(recording_id))
            if recording is None:
                rows.append(
                    {
                        "candidate_id": candidate_id,
                        "recording_id": recording_id,
                        "iq_order": "",
                        "frequency_hz": "",
                        "extraction_status": "skipped",
                        "decoder_status": "",
                        "best_inversion": "",
                        "dmr_sync_count": "",
                        "color_codes": "",
                        "talkgroup_ids": "",
                        "radio_ids": "",
                        "error": (
                            "recording id not found in config"
                        ),
                        "output_dir": "",
                    }
                )
                continue
            for iq_order in config["phase4"]["iq_hypotheses"]:
                frequency = _candidate_frequency(
                    candidate,
                    iq_order,
                )
                destination = (
                    output_root
                    / "decodes"
                    / candidate_id
                    / recording.recording_id
                    / iq_order.lower()
                )
                error = ""
                extraction_status = "ok"
                decoder_status = "not_run"
                best_inversion = ""
                sync_count: int | str = ""
                color_codes: list[int] | str = ""
                talkgroups: list[int] | str = ""
                radio_ids: list[int] | str = ""
                try:
                    extraction = run_channel_extraction(
                        recording.path,
                        destination,
                        candidate_frequency_hz=frequency,
                        settings=config["extraction"],
                        assumed_iq_order=iq_order,
                        candidate_id=candidate_id,
                        recording_id=recording.recording_id,
                    )
                    if config["phase4"]["run_decoder"]:
                        decoder = run_decoder_profiles(
                            extraction["wav_path"],
                            destination / "decoder",
                            settings=config["decoder"],
                        )
                        decoder_status = decoder["status"]
                        best_inversion = decoder[
                            "best_inversion"
                        ]
                        best = next(
                            attempt
                            for attempt in decoder["attempts"]
                            if attempt["inversion"]
                            == best_inversion
                        )
                        evidence = best["evidence"]
                        sync_count = evidence[
                            "dmr_sync_count"
                        ]
                        color_codes = evidence["color_codes"]
                        talkgroups = evidence[
                            "talkgroup_ids"
                        ]
                        radio_ids = evidence["radio_ids"]
                except Exception as exc:
                    extraction_status = "failed"
                    error = f"{type(exc).__name__}: {exc}"
                rows.append(
                    {
                        "candidate_id": candidate_id,
                        "recording_id": recording.recording_id,
                        "iq_order": iq_order,
                        "frequency_hz": frequency,
                        "extraction_status": extraction_status,
                        "decoder_status": decoder_status,
                        "best_inversion": best_inversion,
                        "dmr_sync_count": sync_count,
                        "color_codes": color_codes,
                        "talkgroup_ids": talkgroups,
                        "radio_ids": radio_ids,
                        "error": error,
                        "output_dir": str(destination),
                    }
                )

    decodes_root = output_root / "decodes"
    decodes_root.mkdir(parents=True, exist_ok=True)
    _write_csv(
        decodes_root / "decode_batch_summary.csv",
        rows,
    )
    confirmed = sum(
        row["decoder_status"] == "confirmed_dmr"
        for row in rows
    )
    unavailable = sum(
        row["decoder_status"] == "decoder_unavailable"
        for row in rows
    )
    payload = {
        "project_name": config["project_name"],
        "config_path": str(config["config_path"]),
        "output_dir": str(decodes_root),
        "selected_candidate_count": len(selected),
        "attempt_count": len(rows),
        "confirmed_dmr_attempts": confirmed,
        "decoder_unavailable_attempts": unavailable,
        "phase4": config["phase4"],
        "channel_extraction": config["extraction"].to_dict(),
        "decoder": config["decoder"].to_dict(),
        "rows": rows,
    }
    (decodes_root / "decode_batch_summary.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    table = [
        (
            "| Candidate | Recording | IQ | Frequency MHz | "
            "Extraction | Decoder | Syncs | CC |"
        ),
        "|---|---|---|---:|---|---|---:|---|",
    ]
    for row in rows:
        frequency_mhz = (
            float(row["frequency_hz"]) / 1e6
            if row["frequency_hz"] != ""
            else 0.0
        )
        syncs = (
            row["dmr_sync_count"]
            if row["dmr_sync_count"] != ""
            else "-"
        )
        color_codes = (
            row["color_codes"]
            if row["color_codes"] != ""
            else "-"
        )
        table.append(
            f"| {row['candidate_id']} | "
            f"{row['recording_id']} | "
            f"{row['iq_order'] or '-'} | "
            f"{frequency_mhz:.6f} | "
            f"{row['extraction_status']} | "
            f"{row['decoder_status'] or '-'} | "
            f"{syncs} | {color_codes} |"
        )
    report = f"""# Phase 4 narrowband extraction and decoding

- Selected candidates: **{len(selected)}**
- Attempts: **{len(rows)}**
- Attempts with explicit DMR sync: **{confirmed}**
- Decoder unavailable attempts: **{unavailable}**
- Recordings and IQ hypotheses are processed independently.

{chr(10).join(table)}
"""
    (decodes_root / "decode_batch_report.md").write_text(
        report,
        encoding="utf-8",
    )
    return payload
