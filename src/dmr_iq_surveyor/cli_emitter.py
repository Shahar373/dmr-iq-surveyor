"""`dmr-surveyor geo emitter` and `geo calibrate` -- Phase 8 commands.

Mounted additively under the existing `geo` sub-app; no existing command's
name, arguments, defaults or output paths change.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from dmr_iq_surveyor.cli_geo import CampaignOption, DatabaseOption, _database, _scope
from dmr_iq_surveyor.geo.calibration import (
    LAYOUTS,
    CalibrationScenario,
    render_calibration_markdown,
    run_calibration,
)
from dmr_iq_surveyor.geo.emitters import (
    DEFAULT_TOLERANCE_HZ,
    EMISSIONS,
    EMITTER_SHADOWING_CORRELATION_M,
    EmitterError,
    connect_emitter_database,
    list_emitters,
    materialise_emitter_measurements,
    remove_emitter,
    solve_emitters,
    upsert_emitter,
)
from dmr_iq_surveyor.geo.measurements import MeasurementSettings
from dmr_iq_surveyor.geo.model import SolveSettings
from dmr_iq_surveyor.inspection import write_json

emitter_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Fixed-frequency emitter geolocation: declare a frequency, measure it at several "
        "places, get a credible region. No protocol decoding is involved."
    ),
)
console = Console()


@emitter_app.command("add")
def emitter_add(
    emitter_key: Annotated[str, typer.Argument(help="Stable name for this emitter")],
    frequency_mhz: Annotated[float, typer.Option("--frequency-mhz", help="Carrier frequency")],
    database: DatabaseOption = None,
    label: Annotated[str, typer.Option(help="Free-text label")] = "",
    tolerance_hz: Annotated[
        float, typer.Option("--tolerance-hz", help="How far a measured centre may sit")
    ] = DEFAULT_TOLERANCE_HZ,
    emission: Annotated[
        str,
        typer.Option(
            help=(
                "continuous: a non-detection is evidence. intermittent: a non-detection is "
                "recorded but not used, since the emitter may simply have been off"
            )
        ),
    ] = "continuous",
    truth_lat: Annotated[
        float | None,
        typer.Option("--truth-lat", help="Known latitude, for validation only; never solved on"),
    ] = None,
    truth_lon: Annotated[
        float | None, typer.Option("--truth-lon", help="Known longitude, for validation only")
    ] = None,
    truth_source: Annotated[
        str, typer.Option("--truth-source", help="Where the known position comes from")
    ] = "",
    notes: Annotated[str, typer.Option(help="Free-text note")] = "",
) -> None:
    """Declare (or re-declare) a fixed-frequency emitter."""
    connection = connect_emitter_database(_database(database))
    try:
        emitter = upsert_emitter(
            connection,
            emitter_key=emitter_key,
            frequency_hz=frequency_mhz * 1e6,
            label=label,
            tolerance_hz=tolerance_hz,
            emission=emission,
            truth_latitude=truth_lat,
            truth_longitude=truth_lon,
            truth_source=truth_source,
            notes=notes,
        )
    except EmitterError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1) from exc
    finally:
        connection.close()
    console.print(
        f"Emitter [bold]{emitter['emitter_key']}[/bold] at "
        f"{emitter['frequency_hz'] / 1e6:.6f} MHz ({emitter['emission']})"
        + (" with a known position for validation" if emitter["truth_latitude"] is not None else "")
    )


@emitter_app.command("list")
def emitter_list(database: DatabaseOption = None) -> None:
    """List declared emitters."""
    connection = connect_emitter_database(_database(database))
    try:
        emitters = list_emitters(connection)
    finally:
        connection.close()
    table = Table(title="Declared emitters")
    for column in ("key", "MHz", "tolerance Hz", "emission", "known position", "label"):
        table.add_column(column)
    for emitter in emitters:
        known = (
            f"{emitter['truth_latitude']:.5f}, {emitter['truth_longitude']:.5f} "
            f"({emitter['truth_source']})"
            if emitter["truth_latitude"] is not None
            else "-"
        )
        table.add_row(
            emitter["emitter_key"],
            f"{emitter['frequency_hz'] / 1e6:.6f}",
            f"{emitter['tolerance_hz']:g}",
            emitter["emission"],
            known,
            emitter["label"],
        )
    console.print(table)


@emitter_app.command("remove")
def emitter_remove(
    emitter_key: Annotated[str, typer.Argument(help="Emitter to remove")],
    database: DatabaseOption = None,
) -> None:
    """Remove an emitter and every measurement and solution derived for it."""
    connection = connect_emitter_database(_database(database))
    try:
        remove_emitter(connection, emitter_key)
    except EmitterError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1) from exc
    finally:
        connection.close()
    console.print(f"Removed emitter {emitter_key}")


@emitter_app.command("measurements")
def emitter_measurements(
    database: DatabaseOption = None,
    campaign: CampaignOption = None,
    run_id: Annotated[
        list[str] | None, typer.Option("--run-id", help="Rebuild only these runs")
    ] = None,
    level_metric: Annotated[
        str, typer.Option("--level-metric", help="snr_db or p95_snr_db")
    ] = "snr_db",
) -> None:
    """Rebuild emitter measurements from stored survey observations."""
    try:
        report = materialise_emitter_measurements(
            database_path=_database(database),
            run_ids=run_id,
            settings=MeasurementSettings(level_metric=level_metric),
            scope=_scope(campaign),
        )
    except (ValueError, sqlite3.Error) as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1) from exc
    console.print_json(json.dumps({"run_count": report["run_count"], **report["summary"]}))


@emitter_app.command("solve")
def emitter_solve(
    database: DatabaseOption = None,
    campaign: CampaignOption = None,
    emitter: Annotated[
        list[str] | None, typer.Option("--emitter", help="Solve only these emitters")
    ] = None,
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Write JSON and GeoJSON reports here")
    ] = None,
    solve_batch_id: Annotated[str | None, typer.Option("--solve-batch-id")] = None,
    level_metric: Annotated[
        str, typer.Option("--level-metric", help="snr_db or p95_snr_db")
    ] = "snr_db",
    shadowing_correlation_m: Annotated[
        float,
        typer.Option(
            "--shadowing-correlation-m",
            help=(
                "Distance over which shadow fading is assumed shared between stops; stops "
                "closer than this count as less than one independent measurement each. "
                "0 treats every stop as independent (Phase 7 behaviour), which is "
                "overconfident on dense drive data"
            ),
        ),
    ] = EMITTER_SHADOWING_CORRELATION_M,
) -> None:
    """Solve every declared emitter. Produces credible regions, never coordinates."""
    try:
        report = solve_emitters(
            database_path=_database(database),
            output_root=output_root,
            emitter_keys=emitter,
            solve_batch_id=solve_batch_id,
            measurement_settings=MeasurementSettings(level_metric=level_metric),
            scope=_scope(campaign),
            settings=SolveSettings(shadowing_correlation_m=shadowing_correlation_m),
        )
    except (ValueError, sqlite3.Error) as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"Emitter solutions ({report['solve_batch_id']})")
    for column in ("emitter", "status", "det/non", "90% area km2", "vs known position"):
        table.add_column(column)
    for row in report["solutions"]:
        truth = row.get("truth") or {}
        if truth.get("mode_error_m") is not None:
            versus = (
                f"mode {truth['mode_error_m']:,.0f} m away; "
                f"inside 90%: {'yes' if truth['inside'].get('0.9') else 'NO'}"
            )
        else:
            versus = "-"
        area = row.get("area_km2_90")
        table.add_row(
            row["emitter_key"],
            row["status"],
            f"{row['detection_count']}/{row['non_detection_count']}",
            "-" if area is None else f"{area:.2f}",
            versus,
        )
    console.print(table)
    console.print(
        "[dim]A credible region is a search area, not a transmitter coordinate; "
        "one transmitter per frequency is assumed.[/dim]"
    )
    if "output_dir" in report:
        console.print(f"Reports written under {report['output_dir']}/reports")


def calibrate_command(
    trials: Annotated[int, typer.Option(help="Independent simulated campaigns")] = 40,
    stops: Annotated[int, typer.Option(help="Stops per campaign")] = 8,
    layout: Annotated[str, typer.Option(help=f"One of {', '.join(LAYOUTS)}")] = "ring",
    radius_m: Annotated[float, typer.Option("--radius-m")] = 4000.0,
    transmitter_spread_m: Annotated[float, typer.Option("--transmitter-spread-m")] = 1500.0,
    reference_level_db: Annotated[
        float, typer.Option("--reference-level-db", help="True level at 1 km")
    ] = 30.0,
    exponent: Annotated[float, typer.Option(help="True path-loss exponent")] = 3.5,
    sigma_db: Annotated[float, typer.Option("--sigma-db", help="True shadow fading")] = 6.0,
    correlation_m: Annotated[
        float,
        typer.Option(
            "--correlation-m",
            help="Shadowing decorrelation distance; 0 = independent per stop",
        ),
    ] = 0.0,
    censor_level_db: Annotated[float, typer.Option("--censor-level-db")] = 6.0,
    drive_spacing_m: Annotated[float, typer.Option("--drive-spacing-m")] = 150.0,
    model_correlation_m: Annotated[
        float,
        typer.Option(
            "--model-correlation-m",
            help=(
                "Shadowing correlation the SOLVER assumes (what `geo emitter solve "
                "--shadowing-correlation-m` sets); --correlation-m is the simulated truth"
            ),
        ),
    ] = EMITTER_SHADOWING_CORRELATION_M,
    seed: Annotated[int, typer.Option()] = 0,
    output: Annotated[
        Path | None, typer.Option("--output", help="Write the full JSON report here")
    ] = None,
) -> None:
    """Measure whether credible regions are honest, by simulation.

    Draws campaigns from a declared scenario, solves each with the production
    solver, and reports how often the 50%/90% regions contain the true
    transmitter, plus mode error and region size.
    """
    scenario = CalibrationScenario(
        stops=stops,
        layout=layout,
        radius_m=radius_m,
        transmitter_spread_m=transmitter_spread_m,
        reference_level_db=reference_level_db,
        true_path_loss_exponent=exponent,
        true_sigma_db=sigma_db,
        correlation_m=correlation_m,
        censor_level_db=censor_level_db,
        drive_spacing_m=drive_spacing_m,
        trials=trials,
        seed=seed,
    )
    try:
        report = run_calibration(
            scenario, SolveSettings(shadowing_correlation_m=model_correlation_m)
        )
    except ValueError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1) from exc
    console.print(render_calibration_markdown(report), markup=False)
    if output is not None:
        write_json(output, report)
        console.print(f"Full report written to {output}")


__all__ = ["EMISSIONS", "calibrate_command", "emitter_app"]
