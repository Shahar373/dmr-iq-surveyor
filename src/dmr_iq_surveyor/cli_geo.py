"""`dmr-surveyor geo` -- Phase 7 site geolocation commands.

Mounted additively on the existing Typer app; no existing command's name,
arguments, defaults or output paths change.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from dmr_iq_surveyor.geo.measurements import MeasurementSettings
from dmr_iq_surveyor.geo.model import SolveSettings
from dmr_iq_surveyor.geo.pipeline import (
    build_map_geojson,
    import_reference_sites,
    materialise_measurements,
    site_overview,
    solve_all_sites,
)
from dmr_iq_surveyor.geo.store import connect_geo_database, solution_history
from dmr_iq_surveyor.reference.p25_sites import ReferenceError
from dmr_iq_surveyor.survey.pipeline import DEFAULT_DATABASE_PATH

geo_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "P25 site geolocation from multiple passive survey sessions. "
        "Produces credible regions, never confirmed transmitter coordinates."
    ),
)
console = Console()

DatabaseOption = Annotated[
    Path | None, typer.Option("--database", help="Persistent inventory SQLite path")
]


def _database(value: Path | None) -> Path:
    return value if value is not None else DEFAULT_DATABASE_PATH


@geo_app.command("import-sites")
def geo_import_sites(
    csv_path: Annotated[Path, typer.Argument(help="P25 site snapshot CSV to import")],
    database: DatabaseOption = None,
    snapshot_id: Annotated[
        str | None,
        typer.Option("--snapshot-id", help="Stable id for this snapshot; re-importing replaces it"),
    ] = None,
    notes: Annotated[str, typer.Option(help="Free-text provenance note")] = "",
) -> None:
    """Import an external P25 site list into the reference registry.

    Reference data never influences discovery -- it is matched against
    observations that were already measured and stored.
    """
    try:
        summary = import_reference_sites(
            csv_path, database_path=_database(database), snapshot_id=snapshot_id, notes=notes
        )
    except (FileNotFoundError, OSError, ReferenceError, sqlite3.Error) as exc:
        console.print(f"[bold red]Reference import failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title="P25 reference snapshot")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for key in (
        "snapshot_id",
        "sites_created",
        "sites_updated",
        "channels_imported",
        "sites_without_frequency",
    ):
        table.add_row(key, str(summary[key]))
    console.print(table)
    for warning in summary["warnings"]:
        console.print(f"[yellow]note:[/yellow] {warning}")


@geo_app.command("measurements")
def geo_measurements(
    database: DatabaseOption = None,
    run: Annotated[
        list[str] | None,
        typer.Option("--run", help="Limit to these survey run ids (repeatable); default is all"),
    ] = None,
    level_metric: Annotated[
        str, typer.Option("--level-metric", help="snr_db or p95_snr_db")
    ] = "snr_db",
    tolerance_hz: Annotated[
        float, typer.Option("--tolerance-hz", help="Frequency match tolerance")
    ] = 6250.0,
    passband_guard_hz: Annotated[
        float,
        typer.Option(
            "--passband-guard-hz",
            help="Margin held back from measured passband edges before trusting a non-detection",
        ),
    ] = 25_000.0,
) -> None:
    """Materialise site-level measurements from stored survey observations."""
    settings = MeasurementSettings(
        level_metric=level_metric,
        frequency_tolerance_hz=tolerance_hz,
        passband_guard_hz=passband_guard_hz,
    )
    try:
        result = materialise_measurements(
            database_path=_database(database), run_ids=run or None, settings=settings
        )
    except (ValueError, OSError, sqlite3.Error) as exc:
        console.print(f"[bold red]Measurement extraction failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    summary = result["summary"]
    table = Table(title=f"Geolocation measurements from {result['run_count']} survey run(s)")
    table.add_column("Category", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Detections", str(summary["detections"]))
    table.add_row("Non-detections (looked, heard nothing)", str(summary["non_detections"]))
    table.add_row("Not covered (outside measured passband)", str(summary["not_covered"]))
    table.add_row("Excluded, ambiguous frequency", str(summary["ambiguous"]))
    table.add_row("Excluded, run had no position", str(summary["no_position"]))
    console.print(table)


@geo_app.command("solve")
def geo_solve(
    database: DatabaseOption = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Directory for reports and GeoJSON")
    ] = Path("runs/geo"),
    site: Annotated[
        list[str] | None,
        typer.Option("--site", help="Limit to these site keys, e.g. BEE00:37D:1:30 (repeatable)"),
    ] = None,
    batch_id: Annotated[
        str | None, typer.Option("--batch-id", help="Solve batch id; re-solving replaces it")
    ] = None,
    sigma_db: Annotated[
        float, typer.Option("--sigma-db", help="Shadow-fading standard deviation")
    ] = 8.0,
    resolution_m: Annotated[
        float, typer.Option("--resolution-m", help="Target fine grid resolution")
    ] = 100.0,
    margin_m: Annotated[
        float, typer.Option("--margin-m", help="Search margin beyond the measurement extent")
    ] = 25_000.0,
    min_detections: Annotated[
        int, typer.Option("--min-detections", help="Detections required before solving at all")
    ] = 3,
) -> None:
    """Estimate a credible region for every site with usable measurements."""
    # The coarse pass must never be finer than the fine pass. Scaling it with
    # the requested resolution keeps `--resolution-m 750` working instead of
    # failing validation after the operator has already asked for a solve.
    settings = SolveSettings(
        sigma_db=sigma_db,
        resolution_m=resolution_m,
        coarse_resolution_m=max(resolution_m * 5.0, 500.0),
        margin_m=margin_m,
        min_detections=min_detections,
    )
    try:
        report = solve_all_sites(
            database_path=_database(database),
            output_root=output,
            site_keys=site or None,
            solve_batch_id=batch_id,
            settings=settings,
        )
    except (ValueError, OSError, sqlite3.Error) as exc:
        console.print(f"[bold red]Solve failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"Geolocation solve {report['solve_batch_id']}")
    table.add_column("Site", style="bold")
    table.add_column("Status")
    table.add_column("Det.", justify="right")
    table.add_column("Non-det.", justify="right")
    table.add_column("Mode")
    table.add_column("90% area", justify="right")
    for solution in report["solutions"]:
        mode = (
            f"{solution['mode_latitude']:.5f}, {solution['mode_longitude']:.5f}"
            if solution.get("mode_latitude") is not None
            else "-"
        )
        area = solution.get("area_km2_90")
        table.add_row(
            solution["site_key"],
            solution["status"],
            str(solution["detection_count"]),
            str(solution["non_detection_count"]),
            mode,
            f"{area:.2f} km2" if area else "-",
        )
    console.print(table)
    console.print(
        "[yellow]A region is a search-area reduction, not a transmitter coordinate.[/yellow]"
    )
    if "output_dir" in report:
        markdown = (
            Path(report["output_dir"]) / "reports" / f"geolocation_{report['solve_batch_id']}.md"
        )
        console.print(f"[green]Reports:[/green] {markdown}")


@geo_app.command("sites")
def geo_sites(database: DatabaseOption = None) -> None:
    """List registry sites with their evidence and latest solution status."""
    try:
        overview = site_overview(database_path=_database(database))
    except (OSError, sqlite3.Error) as exc:
        console.print(f"[bold red]Could not read the registry:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if not overview:
        console.print("No P25 sites in the registry. Run `dmr-surveyor geo import-sites` first.")
        return

    table = Table(title="P25 site registry")
    table.add_column("Site", style="bold")
    table.add_column("Snapshot status")
    table.add_column("CC (MHz)", justify="right")
    table.add_column("Det.", justify="right")
    table.add_column("Non-det.", justify="right")
    table.add_column("Excl.", justify="right")
    table.add_column("Solution")
    table.add_column("90% area", justify="right")
    for entry in overview:
        channels = ", ".join(
            f"{channel['frequency_hz'] / 1e6:.6f}"
            + ("*" if int(channel["sharing_site_count"] or 1) > 1 else "")
            for channel in entry["channels"]
        )
        area = entry.get("area_km2_90")
        table.add_row(
            entry["site_key"],
            entry["observation_status"],
            channels or "-",
            str(entry["detections"]),
            str(entry["non_detections"]),
            str(entry["excluded"]),
            entry.get("status") or "-",
            f"{area:.2f}" if area else "-",
        )
    console.print(table)
    console.print("[dim]* frequency is on record for more than one site: not attributable[/dim]")


@geo_app.command("history")
def geo_history(
    site_key: Annotated[str, typer.Argument(help="Site key, e.g. BEE00:37D:1:30")],
    database: DatabaseOption = None,
) -> None:
    """Show how one site's credible region changed as sessions accumulated."""
    connection = connect_geo_database(_database(database))
    try:
        row = connection.execute(
            "SELECT p25_site_id FROM p25_sites WHERE site_key = ?", (site_key,)
        ).fetchone()
        if row is None:
            console.print(f"[bold red]Unknown site key:[/bold red] {site_key}")
            raise typer.Exit(code=1)
        history = solution_history(connection, int(row["p25_site_id"]))
    finally:
        connection.close()

    table = Table(title=f"Solution history for {site_key}")
    table.add_column("Solved at", style="bold")
    table.add_column("Batch")
    table.add_column("Status")
    table.add_column("Det.", justify="right")
    table.add_column("Non-det.", justify="right")
    table.add_column("50% area", justify="right")
    table.add_column("90% area", justify="right")
    for entry in history:
        table.add_row(
            entry["solved_at"],
            entry["solve_batch_id"],
            entry["status"],
            str(entry["detection_count"]),
            str(entry["non_detection_count"]),
            f"{entry['area_km2_50']:.2f}" if entry["area_km2_50"] else "-",
            f"{entry['area_km2_90']:.2f}" if entry["area_km2_90"] else "-",
        )
    console.print(table)


@geo_app.command("export")
def geo_export(
    output: Annotated[Path, typer.Argument(help="GeoJSON file to write")],
    database: DatabaseOption = None,
) -> None:
    """Write measurements, estimates and credible regions as one GeoJSON."""
    try:
        payload = build_map_geojson(database_path=_database(database))
    except (OSError, sqlite3.Error) as exc:
        console.print(f"[bold red]Export failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    console.print(
        f"[green]Wrote[/green] {len(payload['features'])} features to {destination}"
    )


__all__ = ["geo_app"]
