from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from dmr_iq_surveyor.cli_v2 import app, console
from dmr_iq_surveyor.inventory import (
    build_inventory,
    build_inventory_from_config,
)


@app.command("inventory-build")
def inventory_build(
    decodes_dir: Annotated[
        Path,
        typer.Argument(help="Phase 4/4.1 decodes directory"),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Directory for Phase 5 inventory artifacts",
        ),
    ] = Path("runs/inventory"),
    database: Annotated[
        Path | None,
        typer.Option(help="Persistent SQLite database path"),
    ] = None,
    run_id: Annotated[
        str | None,
        typer.Option(help="Stable id for this imported decoder run"),
    ] = None,
    max_gap_lines: Annotated[
        int,
        typer.Option(
            help="Maximum log-line gap inside one correlated session"
        ),
    ] = 12,
) -> None:
    """Build or update a persistent DMR event and channel inventory."""
    try:
        result = build_inventory(
            decodes_dir,
            output,
            database_path=database,
            run_id=run_id,
            max_gap_lines=max_gap_lines,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        console.print(
            f"[bold red]Phase 5 inventory failed:[/bold red] {exc}"
        )
        raise typer.Exit(code=1) from exc
    _print_inventory_result(result)


@app.command("inventory-batch")
def inventory_batch(
    config: Annotated[
        Path,
        typer.Argument(help="YAML configuration for a Phase 4 run"),
    ],
) -> None:
    """Build inventory using project.output_root and phase5 settings."""
    try:
        result = build_inventory_from_config(config)
    except (FileNotFoundError, OSError, ValueError) as exc:
        console.print(
            f"[bold red]Phase 5 inventory failed:[/bold red] {exc}"
        )
        raise typer.Exit(code=1) from exc
    _print_inventory_result(result)


def _print_inventory_result(result: dict[str, object]) -> None:
    table = Table(title="Phase 5 DMR inventory")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", str(result["run_id"]))
    table.add_row("Imported attempts", str(result["attempts_imported"]))
    table.add_row("Database attempts", str(result["database_attempts"]))
    table.add_row("Channels", str(result["database_channels"]))
    table.add_row("Events", str(result["events"]))
    table.add_row("Sessions, total", str(result["sessions"]))
    table.add_row(
        "Meaningful sessions",
        str(result["meaningful_sessions"]),
    )
    table.add_row(
        "Error-only sessions",
        str(result["error_only_sessions"]),
    )
    table.add_row("Talkgroup IDs", str(result["talkgroup_ids"]))
    table.add_row("Radio IDs", str(result["radio_ids"]))
    console.print(table)
    output_dir = Path(str(result["output_dir"]))
    console.print(
        f"[green]Phase 5 artifacts written to:[/green] {output_dir}"
    )
    console.print(f"Open: {output_dir / 'phase5_report.md'}")


__all__ = ["app"]
