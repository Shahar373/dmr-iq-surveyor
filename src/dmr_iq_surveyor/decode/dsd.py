from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ERROR_TOKEN = re.compile(r"\b(?:ERR(?:OR)?|FAILED|FATAL)\b", re.IGNORECASE)
_SIGNED_DMR_SYNC = re.compile(r"\bSync:\s*[+-]DMR\b", re.IGNORECASE)


@dataclass(slots=True)
class DecoderSettings:
    binary: str = "dsd-fme"
    timeout_seconds: float = 120.0
    inversions: list[str] = field(
        default_factory=lambda: ["normal", "inverted"]
    )

    def validate(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        invalid = set(self.inversions) - {"normal", "inverted"}
        if invalid:
            raise ValueError(
                f"Unsupported decoder inversions: {sorted(invalid)}"
            )
        if not self.inversions:
            raise ValueError("At least one decoder inversion is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DecoderProbe:
    available: bool
    requested_binary: str
    resolved_binary: str | None
    help_text: str
    help_sha256: str | None
    probe_error: str | None
    supports_null_output: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_decoder(binary: str) -> DecoderProbe:
    resolved = shutil.which(binary)
    if resolved is None:
        return DecoderProbe(
            available=False,
            requested_binary=binary,
            resolved_binary=None,
            help_text="",
            help_sha256=None,
            probe_error=f"Decoder binary not found: {binary}",
            supports_null_output=False,
        )
    try:
        process = subprocess.run(
            [resolved, "-h"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        help_text = (process.stdout or "") + (process.stderr or "")
        lower = help_text.lower()
        return DecoderProbe(
            available=True,
            requested_binary=binary,
            resolved_binary=resolved,
            help_text=help_text,
            help_sha256=hashlib.sha256(
                help_text.encode("utf-8", errors="replace")
            ).hexdigest(),
            probe_error=(
                None
                if help_text
                else f"dsd-fme -h returned {process.returncode} with no text"
            ),
            supports_null_output=("null" in lower and "-o" in help_text),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DecoderProbe(
            available=False,
            requested_binary=binary,
            resolved_binary=resolved,
            help_text="",
            help_sha256=None,
            probe_error=f"{type(exc).__name__}: {exc}",
            supports_null_output=False,
        )


def build_decoder_command(
    probe: DecoderProbe,
    wav_path: str | Path,
    inversion: str,
) -> list[str]:
    if not probe.available or probe.resolved_binary is None:
        raise FileNotFoundError(probe.requested_binary)
    if inversion not in {"normal", "inverted"}:
        raise ValueError("inversion must be normal or inverted")
    command = [probe.resolved_binary, "-fs"]
    if inversion == "inverted":
        command.append("-xr")
    command.extend(["-i", str(Path(wav_path).resolve())])
    if probe.supports_null_output:
        command.extend(["-o", "null"])
    return command


def _unique_ints(pattern: str, text: str) -> list[int]:
    return sorted(
        {
            int(value)
            for value in re.findall(
                pattern,
                text,
                flags=re.IGNORECASE,
            )
        }
    )


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def _active_slot(line: str) -> int | None:
    if re.search(r"\[\s*slot\s*1\s*\]", line, re.IGNORECASE):
        return 1
    if re.search(r"\[\s*slot\s*2\s*\]", line, re.IGNORECASE):
        return 2
    return None


def _error_counts(lines: list[str]) -> tuple[dict[str, int], list[str]]:
    error_lines = [line for line in lines if _ERROR_TOKEN.search(line)]
    counts = {
        "total_error_lines": len(error_lines),
        "crc_error_lines": sum(
            bool(re.search(r"CRC\s+ERR", line, re.IGNORECASE))
            for line in error_lines
        ),
        "fec_error_lines": sum(
            bool(re.search(r"FEC\s+ERR", line, re.IGNORECASE))
            for line in error_lines
        ),
        "cach_error_lines": sum(
            bool(re.search(r"CACH.*ERR", line, re.IGNORECASE))
            for line in error_lines
        ),
        "frame_sync_error_lines": sum(
            bool(re.search(r"Frame\s+Sync\s+Err", line, re.IGNORECASE))
            for line in error_lines
        ),
        "voice_error_lines": sum(
            bool(re.search(r"VOICE.*ERR", line, re.IGNORECASE))
            for line in error_lines
        ),
    }
    return counts, error_lines[:100]


def _quality_score(
    *,
    sync_count: int,
    valid_cc_ratio: float,
    dominant_cc_consistency: float,
    clean_sync_ratio: float,
    error_ratio: float,
    control_event_ratio: float,
    voice_stage_diversity: float,
    repetitive_single_stage_voice: bool,
) -> tuple[float, dict[str, float]]:
    sync_support = 0.0
    if sync_count:
        sync_support = 0.25 * min(
            1.0,
            math.log1p(sync_count) / math.log(101.0),
        )
    components = {
        "valid_color_code": 4.0 * valid_cc_ratio,
        "dominant_color_code_consistency": 3.0 * dominant_cc_consistency,
        "clean_sync_ratio": 1.5 * clean_sync_ratio,
        "error_quality": 1.5 * (1.0 - error_ratio),
        "coherent_events": 1.5
        * max(control_event_ratio, voice_stage_diversity),
        "sync_support": sync_support,
        "repetitive_single_stage_voice_penalty": (
            -2.5 if repetitive_single_stage_voice else 0.0
        ),
    }
    return float(sum(components.values())), components


def _evidence_tier(evidence: dict[str, Any]) -> str:
    if evidence["dmr_sync_count"] == 0:
        return "no_dmr_sync"
    if (
        evidence["numeric_color_code_count"] >= 5
        and evidence["valid_color_code_ratio"] >= 0.8
        and evidence["dominant_color_code_consistency"] >= 0.8
        and evidence["error_ratio"] <= 0.15
        and evidence["clean_sync_count"] >= 5
        and not evidence["repetitive_single_stage_voice"]
    ):
        return "dmr_confirmed_clean"
    if (
        evidence["numeric_color_code_count"] >= 1
        and evidence["dominant_color_code_consistency"] >= 0.5
        and evidence["clean_sync_count"] >= 1
        and not evidence["repetitive_single_stage_voice"]
    ):
        return "dmr_confirmed_degraded"
    return "dmr_sync_only"


def parse_dsd_fme_log(stdout: str, stderr: str) -> dict[str, Any]:
    raw_text = "\n".join(part for part in (stdout, stderr) if part)
    text = _strip_ansi(raw_text)
    lines = text.splitlines()
    sync_lines = [line for line in lines if _SIGNED_DMR_SYNC.search(line)]

    numeric_color_codes: list[int] = []
    unknown_color_code_count = 0
    slot1_count = 0
    slot2_count = 0
    clean_sync_count = 0
    control_event_count = 0
    voice_stage_counts = {f"vc{stage}": 0 for stage in range(1, 7)}

    for line in sync_lines:
        color_match = re.search(
            r"Color\s+Code\s*[=:]\s*(\d+|XX)",
            line,
            flags=re.IGNORECASE,
        )
        if color_match:
            value = color_match.group(1)
            if value.upper() == "XX":
                unknown_color_code_count += 1
            else:
                numeric_color_codes.append(int(value))

        active_slot = _active_slot(line)
        if active_slot == 1:
            slot1_count += 1
        elif active_slot == 2:
            slot2_count += 1

        line_has_error = bool(_ERROR_TOKEN.search(line))
        if not line_has_error:
            clean_sync_count += 1
        if not line_has_error and re.search(
            r"\b(?:IDLE|CSBK|DATA)\b",
            line,
            flags=re.IGNORECASE,
        ):
            control_event_count += 1
        if not line_has_error:
            for stage in range(1, 7):
                if re.search(rf"\bVC{stage}\b", line, re.IGNORECASE):
                    voice_stage_counts[f"vc{stage}"] += 1

    color_counter = Counter(numeric_color_codes)
    if color_counter:
        dominant_color_code, dominant_color_code_count = (
            color_counter.most_common(1)[0]
        )
    else:
        dominant_color_code = None
        dominant_color_code_count = 0

    numeric_count = len(numeric_color_codes)
    color_total = numeric_count + unknown_color_code_count
    valid_color_code_ratio = (
        numeric_count / color_total if color_total else 0.0
    )
    dominant_consistency = (
        dominant_color_code_count / numeric_count if numeric_count else 0.0
    )

    error_counts, error_lines = _error_counts(lines)
    sync_count = len(sync_lines)
    error_ratio = min(
        1.0,
        error_counts["total_error_lines"] / max(sync_count, 1),
    )
    clean_sync_ratio = clean_sync_count / max(sync_count, 1)
    control_event_ratio = control_event_count / max(sync_count, 1)
    observed_voice_stages = sum(
        value > 0 for value in voice_stage_counts.values()
    )
    voice_stage_diversity = observed_voice_stages / 6.0
    total_voice_stage_count = sum(voice_stage_counts.values())
    repetitive_single_stage_voice = (
        total_voice_stage_count >= 5
        and observed_voice_stages <= 1
        and control_event_count == 0
    )

    quality_score, score_components = _quality_score(
        sync_count=sync_count,
        valid_cc_ratio=valid_color_code_ratio,
        dominant_cc_consistency=dominant_consistency,
        clean_sync_ratio=clean_sync_ratio,
        error_ratio=error_ratio,
        control_event_ratio=control_event_ratio,
        voice_stage_diversity=voice_stage_diversity,
        repetitive_single_stage_voice=repetitive_single_stage_voice,
    )

    event_counts = {
        "voice": len(
            re.findall(r"\bvoice\b", text, flags=re.IGNORECASE)
        ),
        "data": len(re.findall(r"\bdata\b", text, flags=re.IGNORECASE)),
        "idle": len(re.findall(r"\bidle\b", text, flags=re.IGNORECASE)),
        "csbk": len(re.findall(r"\bCSBK\b", text, flags=re.IGNORECASE)),
    }

    result: dict[str, Any] = {
        "dmr_sync_count": sync_count,
        "explicit_dmr_sync": bool(sync_lines),
        "sync_lines": sync_lines[:100],
        "color_codes": sorted(color_counter),
        "color_code_counts": {
            str(key): value for key, value in sorted(color_counter.items())
        },
        "numeric_color_code_count": numeric_count,
        "unknown_color_code_count": unknown_color_code_count,
        "valid_color_code_ratio": valid_color_code_ratio,
        "dominant_color_code": dominant_color_code,
        "dominant_color_code_count": dominant_color_code_count,
        "dominant_color_code_consistency": dominant_consistency,
        "slot1_sync_count": slot1_count,
        "slot2_sync_count": slot2_count,
        "clean_sync_count": clean_sync_count,
        "clean_sync_ratio": clean_sync_ratio,
        "talkgroup_ids": _unique_ints(
            r"(?:Talkgroup|\bTG|\bTGT|Target)\s*[=:]\s*(\d+)",
            text,
        ),
        "radio_ids": _unique_ints(
            r"(?:Radio\s+ID|Source|\bSRC)\s*[=:]\s*(\d+)",
            text,
        ),
        "event_counts": event_counts,
        "control_event_count": control_event_count,
        "control_event_ratio": control_event_ratio,
        "voice_stage_counts": voice_stage_counts,
        "voice_stage_diversity": voice_stage_diversity,
        "repetitive_single_stage_voice": repetitive_single_stage_voice,
        "error_counts": error_counts,
        "error_ratio": error_ratio,
        "error_lines": error_lines,
        "quality_score": quality_score,
        "quality_score_components": score_components,
    }
    result["evidence_tier"] = _evidence_tier(result)
    return result


def run_decoder_attempt(
    wav_path: str | Path,
    output_dir: str | Path,
    *,
    settings: DecoderSettings,
    inversion: str,
    probe: DecoderProbe | None = None,
) -> dict[str, Any]:
    settings.validate()
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    resolved_probe = probe or probe_decoder(settings.binary)
    stdout_path = destination / f"dsd_fme_{inversion}_stdout.log"
    stderr_path = destination / f"dsd_fme_{inversion}_stderr.log"

    if not resolved_probe.available:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(
            resolved_probe.probe_error or "decoder unavailable",
            encoding="utf-8",
        )
        result = {
            "status": "decoder_unavailable",
            "inversion": inversion,
            "command": [],
            "return_code": None,
            "timed_out": False,
            "probe": resolved_probe.to_dict(),
            "evidence": parse_dsd_fme_log("", ""),
        }
    else:
        command = build_decoder_command(
            resolved_probe,
            wav_path,
            inversion,
        )
        timed_out = False
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=settings.timeout_seconds,
                check=False,
                cwd=destination,
            )
            stdout = process.stdout or ""
            stderr = process.stderr or ""
            return_code: int | None = process.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = (
                exc.stdout.decode(errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode(errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
            return_code = None
        except OSError as exc:
            stdout = ""
            stderr = f"{type(exc).__name__}: {exc}"
            return_code = None
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        evidence = parse_dsd_fme_log(stdout, stderr)
        tier = evidence["evidence_tier"]
        if tier != "no_dmr_sync":
            status = tier
        elif timed_out:
            status = "decoder_timeout"
        elif return_code not in {0, None}:
            status = "decoder_error"
        else:
            status = "no_dmr_sync"
        result = {
            "status": status,
            "inversion": inversion,
            "command": command,
            "return_code": return_code,
            "timed_out": timed_out,
            "probe": resolved_probe.to_dict(),
            "evidence": evidence,
        }

    report_path = destination / f"decoder_report_{inversion}.json"
    report_path.write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )
    return result


def run_decoder_profiles(
    wav_path: str | Path,
    output_dir: str | Path,
    *,
    settings: DecoderSettings | None = None,
) -> dict[str, Any]:
    resolved = settings or DecoderSettings()
    resolved.validate()
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    probe = probe_decoder(resolved.binary)
    attempts = [
        run_decoder_attempt(
            wav_path,
            destination,
            settings=resolved,
            inversion=inversion,
            probe=probe,
        )
        for inversion in resolved.inversions
    ]
    best = max(
        attempts,
        key=lambda item: (
            float(item["evidence"]["quality_score"]),
            item["status"] == "dmr_confirmed_clean",
            item["status"] == "dmr_confirmed_degraded",
            item["inversion"] == "normal",
        ),
    )
    payload = {
        "status": best["status"],
        "best_inversion": best["inversion"],
        "best_quality_score": best["evidence"]["quality_score"],
        "probe": probe.to_dict(),
        "attempts": attempts,
    }
    (destination / "decoder_report.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# DSD-FME decoder report",
        "",
        f"- Status: **{payload['status']}**",
        f"- Best inversion: **{payload['best_inversion']}**",
        f"- Best quality score: **{payload['best_quality_score']:.3f}**",
        f"- Decoder available: **{probe.available}**",
        "",
        (
            "| Inversion | Status | Score | Syncs | Dominant CC | "
            "Valid CC | Errors | Slot 1 | Slot 2 | TG IDs | Radio IDs |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for attempt in attempts:
        evidence = attempt["evidence"]
        dominant_cc = evidence["dominant_color_code"]
        lines.append(
            "| {inv} | {status} | {score:.3f} | {syncs} | {cc} | "
            "{valid:.1%} | {errors:.1%} | {slot1} | {slot2} | "
            "{tg} | {rid} |".format(
                inv=attempt["inversion"],
                status=attempt["status"],
                score=evidence["quality_score"],
                syncs=evidence["dmr_sync_count"],
                cc=dominant_cc if dominant_cc is not None else "-",
                valid=evidence["valid_color_code_ratio"],
                errors=evidence["error_ratio"],
                slot1=evidence["slot1_sync_count"],
                slot2=evidence["slot2_sync_count"],
                tg=evidence["talkgroup_ids"],
                rid=evidence["radio_ids"],
            )
        )
    (destination / "decoder_report.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return payload
