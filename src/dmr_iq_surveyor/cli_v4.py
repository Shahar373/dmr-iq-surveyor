from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from dmr_iq_surveyor.cli_v3 import app, console
from dmr_iq_surveyor.decode import DecoderSettings
from dmr_iq_surveyor.inventory import import_standalone_log
from dmr_iq_surveyor.targeted import (
    load_capture_metadata,
    run_targeted_capture,
)


@app.command("targeted-decode")
def targeted_decode(
    recording: Annotated[
        Path,
        typer.Argument(help="Channel-centered SDRconnect IQ recording"),
    ],
    frequency_hz: Annotated[
        float,
        typer.Option("--frequency", help="Known RF channel center in Hz"),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Targeted run output root"),
    ] = Path("runs/targeted"),
    profile: Annotated[
        str,
        typer.Option(help="Extraction profile: auto, 10m, 500k, or 250k"),
    ] = "auto",
    recording_id: Annotated[
        str | None,
        typer.Option(help="Stable recording identifier"),
    ] = None,
    run_id: Annotated[
        str | None,
        typer.Option(help="Stable Phase 5 run identifier"),
    ] = None,
    metadata: Annotated[
        Path | None,
        typer.Option(help="Capture metadata YAML or JSON file"),
    ] = None,
    database: Annotated[
        Path | None,
        typer.Option(help="Persistent inventory SQLite path"),
    ] = None,
    binary: Annotated[
        str,
        typer.Option(help="DSD-FME binary name or path"),
    ] = "dsd-fme",
    timeout_seconds: Annotated[
        float,
        typer.Option(help="Maximum duration of each decoder attempt"),
    ] = 120.0,
    try_inverted: Annotated[
        bool,
        typer.Option("--try-inverted/--normal-only"),
    ] = True,
) -> None:
    """Extract, decode, and inventory one known DMR frequency."""
    inversions = ["normal", "inverted"] if try_inverted else ["normal"]
    try:
        result = run_targeted_capture(
            recording,
            output,
            frequency_hz=frequency_hz,
            profile_name=profile,
            recording_id=recording_id,
            run_id=run_id,
            metadata=load_capture_metadata(metadata),
            database_path=database,
            decoder_settings=DecoderSettings(
                binary=binary,
                timeout_seconds=timeout_seconds,
                inversions=inversions,
            ),
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        console.print(f"[bold red]Targeted decode failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    table = Table(title="Targeted DMR run")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", str(result["run_id"]))
    table.add_row("Frequency", f"{result['frequency_hz'] / 1e6:.6f} MHz")
    table.add_row("Profile", str(result["profile_name"]))
    table.add_row("Decoder", str(result["decoder"]["status"]))
    table.add_row(
        "Talkgroups",
        str(result["inventory"]["talkgroup_ids"]),
    )
    table.add_row(
        "Radio IDs",
        str(result["inventory"]["radio_ids"]),
    )
    console.print(table)
    console.print(f"Open: {Path(output).resolve() / 'targeted_run.md'}")


@app.command("inventory-import-log")
def inventory_import_log(
    log_path: Annotated[
        Path,
        typer.Argument(help="Standalone DSD-FME stdout/stderr log"),
    ],
    frequency_hz: Annotated[
        float,
        typer.Option("--frequency", help="RF frequency in Hz"),
    ],
    run_id: Annotated[
        str,
        typer.Option(help="Stable Phase 5 run identifier"),
    ],
    recording_id: Annotated[
        str,
        typer.Option(help="Stable recording identifier"),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Import output root"),
    ] = Path("runs/standalone"),
    database: Annotated[
        Path | None,
        typer.Option(help="Persistent inventory SQLite path"),
    ] = None,
    metadata: Annotated[
        Path | None,
        typer.Option(help="Capture metadata YAML or JSON file"),
    ] = None,
) -> None:
    """Import an existing DSD-FME log directly into Phase 5."""
    try:
        result = import_standalone_log(
            log_path,
            output,
            frequency_hz=frequency_hz,
            run_id=run_id,
            recording_id=recording_id,
            database_path=database,
            capture_metadata=load_capture_metadata(metadata),
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        console.print(f"[bold red]Log import failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"Imported {result['frequency_hz'] / 1e6:.6f} MHz as "
        f"{result['status']}"
    )
    console.print(f"Open: {Path(output).resolve() / 'inventory' / 'phase5_report.md'}")


__all__ = ["app"]
