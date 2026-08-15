from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from dmr_iq_surveyor.survey.pipeline import DEFAULT_DATABASE_PATH, run_comparison, run_survey
from dmr_iq_surveyor.survey.profiles import ProfileError, resolve_band_profile
from dmr_iq_surveyor.survey.store import (
    connect_survey_database,
    get_run,
    get_run_observations,
    list_runs,
)

survey_app = typer.Typer(
    no_args_is_help=True,
    help="Protocol-agnostic RF survey: discovery, persistent inventory, and run comparison.",
)
console = Console()


@survey_app.command("run")
def survey_run(
    recording: Annotated[
        Path,
        typer.Argument(help="Wideband SDRconnect IQ recording covering the band profile"),
    ],
    band: Annotated[
        str,
        typer.Option(help="Band profile name (config/bands/<name>.yaml) or a path to one"),
    ],
    site: Annotated[
        str,
        typer.Option(help="Site profile name (config/sites/<name>.yaml) or a path to one"),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Survey run output directory"),
    ] = Path("runs/survey"),
    run_id: Annotated[
        str | None,
        typer.Option(help="Stable survey run identifier; defaults to a timestamp + site id"),
    ] = None,
    database: Annotated[
        Path | None,
        typer.Option(help="Persistent inventory SQLite path"),
    ] = None,
    iq_order: Annotated[
        str,
        typer.Option(help="Assumed channel order: IQ or QI"),
    ] = "IQ",
    hash_source: Annotated[
        bool,
        typer.Option(
            "--hash-source/--no-hash-source",
            help="Compute a SHA-256 of the source recording (slow on large files)",
        ),
    ] = False,
    fft_size: Annotated[
        int,
        typer.Option(help="FFT size for segmented spectrum analysis"),
    ] = 65_536,
    overlap_ratio: Annotated[
        float,
        typer.Option(help="FFT overlap ratio for segmented spectrum analysis"),
    ] = 0.5,
) -> None:
    """Discover RF observations in a wideband recording and store them."""
    try:
        result = run_survey(
            recording,
            output,
            band=band,
            site=site,
            run_id=run_id,
            database_path=database,
            assumed_iq_order=iq_order,
            compute_source_hash=hash_source,
            spectrum_fft_size=fft_size,
            spectrum_overlap_ratio=overlap_ratio,
        )
    except (FileNotFoundError, OSError, ValueError, ProfileError) as exc:
        console.print(f"[bold red]Survey run failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title="RF survey run")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", str(result["run_id"]))
    table.add_row("Observations", str(result["observation_count"]))
    passband = result["usable_passband"]
    table.add_row(
        "Usable passband",
        f"{passband['usable_low_hz'] / 1e6:.6f}-{passband['usable_high_hz'] / 1e6:.6f} MHz "
        f"({result['coverage_status']})",
    )
    table.add_row("Elapsed", f"{result['elapsed_seconds']:.3f} s")
    console.print(table)
    console.print(f"[green]Artifacts written to:[/green] {result['output_dir']}")
    console.print(f"Open: {Path(result['output_dir']) / 'reports' / 'report.md'}")


@survey_app.command("list")
def survey_list(
    site: Annotated[
        str | None,
        typer.Option(help="Filter to one site id"),
    ] = None,
    database: Annotated[
        Path | None,
        typer.Option(help="Persistent inventory SQLite path"),
    ] = None,
) -> None:
    """List survey runs stored in the persistent inventory database."""
    database_path = database or DEFAULT_DATABASE_PATH
    if not Path(database_path).expanduser().resolve().is_file():
        console.print(f"[yellow]No survey database found at {database_path}[/yellow]")
        raise typer.Exit(code=0)
    connection = connect_survey_database(database_path)
    try:
        runs = list_runs(connection, site_id=site)
    finally:
        connection.close()

    table = Table(title="Survey runs")
    table.add_column("Run ID")
    table.add_column("Site")
    table.add_column("Band")
    table.add_column("Capture time")
    table.add_column("Coverage")
    table.add_column("Observations", justify="right")
    for row in runs:
        table.add_row(
            str(row["survey_run_id"]),
            str(row["site_id"]),
            str(row["band_profile"]),
            str(row["capture_start_utc"] or f"unknown ({row['capture_time_source']})"),
            str(row["coverage_status"]),
            str(row["observation_count"]),
        )
    console.print(table)


@survey_app.command("show")
def survey_show(
    run_id: Annotated[str, typer.Argument(help="Survey run identifier")],
    database: Annotated[
        Path | None,
        typer.Option(help="Persistent inventory SQLite path"),
    ] = None,
) -> None:
    """Show one survey run and its RF observations."""
    database_path = database or DEFAULT_DATABASE_PATH
    connection = connect_survey_database(database_path)
    try:
        run = get_run(connection, run_id)
        if run is None:
            console.print(f"[bold red]Unknown survey run:[/bold red] {run_id}")
            raise typer.Exit(code=1)
        observations = get_run_observations(connection, run_id)
    finally:
        connection.close()

    table = Table(title=f"Survey run {run_id}")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Site", str(run["site_id"]))
    table.add_row("Band profile", str(run["band_profile"]))
    table.add_row("Source", str(run["source_path"]))
    table.add_row(
        "Capture time",
        f"{run['capture_start_utc'] or 'unknown'} (source: {run['capture_time_source']})",
    )
    table.add_row(
        "Usable passband",
        f"{(run['usable_low_hz'] or 0) / 1e6:.6f}-{(run['usable_high_hz'] or 0) / 1e6:.6f} MHz "
        f"({run['coverage_status']})",
    )
    table.add_row("Observations", str(len(observations)))
    console.print(table)

    obs_table = Table(title="RF observations")
    obs_table.add_column("Frequency MHz", justify="right")
    obs_table.add_column("SNR dB", justify="right")
    obs_table.add_column("P95 SNR dB", justify="right")
    obs_table.add_column("Occupancy %", justify="right")
    obs_table.add_column("Persistence", justify="right")
    obs_table.add_column("Spectral class")
    for observation in observations:
        obs_table.add_row(
            f"{observation['measured_center_hz'] / 1e6:.6f}",
            f"{observation['snr_db']:.1f}",
            f"{observation['p95_snr_db']:.1f}",
            f"{observation['occupancy_pct']:.1f}",
            f"{observation['persistence']:.2f}",
            str(observation["spectral_class"]),
        )
    console.print(obs_table)


@survey_app.command("compare")
def survey_compare(
    baseline_run_id: Annotated[str, typer.Argument(help="Baseline (earlier) run id")],
    target_run_id: Annotated[str, typer.Argument(help="Target (later) run id")],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Directory for comparison reports"),
    ] = Path("runs/survey/compare"),
    database: Annotated[
        Path | None,
        typer.Option(help="Persistent inventory SQLite path"),
    ] = None,
    band: Annotated[
        str | None,
        typer.Option(help="Band profile supplying comparison tolerances; defaults built in otherwise"),
    ] = None,
) -> None:
    """Compare two survey runs. Works with no protocol decoder installed."""
    try:
        band_profile = resolve_band_profile(band) if band else None
        report = run_comparison(
            output,
            baseline_run_id=baseline_run_id,
            target_run_id=target_run_id,
            database_path=database,
            tolerances_from=band_profile,
        )
    except (FileNotFoundError, OSError, ValueError, ProfileError) as exc:
        console.print(f"[bold red]Survey comparison failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"Comparison {baseline_run_id} -> {target_run_id}")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for status, count in sorted(report["status_counts"].items()):
        table.add_row(status, str(count))
    console.print(table)
    console.print(f"[green]Reports written to:[/green] {Path(output).resolve() / 'reports'}")


__all__ = ["survey_app"]
