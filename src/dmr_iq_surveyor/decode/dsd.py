from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


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


def parse_dsd_fme_log(stdout: str, stderr: str) -> dict[str, Any]:
    text = "\n".join(part for part in (stdout, stderr) if part)
    lines = text.splitlines()
    sync_lines = [
        line
        for line in lines
        if re.search(
            r"\bSync:\s*[+-]?DMR\b",
            line,
            flags=re.IGNORECASE,
        )
    ]
    color_codes = _unique_ints(
        r"Color\s+Code\s*[=:]\s*(\d+)",
        text,
    )
    talkgroup_ids = _unique_ints(
        r"(?:Talkgroup|\bTG|\bTGT|Target)\s*[=:]\s*(\d+)",
        text,
    )
    radio_ids = _unique_ints(
        r"(?:Radio\s+ID|Source|\bSRC)\s*[=:]\s*(\d+)",
        text,
    )
    slot1_count = sum(
        bool(
            re.search(
                r"\bslot\s*1\b|\bslot1\b",
                line,
                re.IGNORECASE,
            )
        )
        for line in sync_lines
    )
    slot2_count = sum(
        bool(
            re.search(
                r"\bslot\s*2\b|\bslot2\b",
                line,
                re.IGNORECASE,
            )
        )
        for line in sync_lines
    )
    event_counts = {
        "voice": len(
            re.findall(r"\bvoice\b", text, flags=re.IGNORECASE)
        ),
        "data": len(
            re.findall(r"\bdata\b", text, flags=re.IGNORECASE)
        ),
        "idle": len(
            re.findall(r"\bidle\b", text, flags=re.IGNORECASE)
        ),
        "csbk": len(
            re.findall(r"\bCSBK\b", text, flags=re.IGNORECASE)
        ),
    }
    error_lines = [
        line
        for line in lines
        if re.search(
            r"\b(error|failed|fatal)\b",
            line,
            flags=re.IGNORECASE,
        )
    ][:100]
    return {
        "dmr_sync_count": len(sync_lines),
        "explicit_dmr_sync": bool(sync_lines),
        "sync_lines": sync_lines[:100],
        "color_codes": color_codes,
        "slot1_sync_count": slot1_count,
        "slot2_sync_count": slot2_count,
        "talkgroup_ids": talkgroup_ids,
        "radio_ids": radio_ids,
        "event_counts": event_counts,
        "error_lines": error_lines,
    }


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
        if evidence["explicit_dmr_sync"]:
            status = "confirmed_dmr"
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
            int(item["evidence"]["dmr_sync_count"]),
            len(item["evidence"]["color_codes"]),
            item["status"] == "confirmed_dmr",
        ),
    )
    payload = {
        "status": best["status"],
        "best_inversion": best["inversion"],
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
        f"- Decoder available: **{probe.available}**",
        "",
        (
            "| Inversion | Status | DMR syncs | Color Codes | "
            "TG IDs | Radio IDs |"
        ),
        "|---|---|---:|---|---|---|",
    ]
    for attempt in attempts:
        evidence = attempt["evidence"]
        lines.append(
            "| {inv} | {status} | {syncs} | {cc} | "
            "{tg} | {rid} |".format(
                inv=attempt["inversion"],
                status=attempt["status"],
                syncs=evidence["dmr_sync_count"],
                cc=evidence["color_codes"],
                tg=evidence["talkgroup_ids"],
                rid=evidence["radio_ids"],
            )
        )
    (destination / "decoder_report.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return payload
