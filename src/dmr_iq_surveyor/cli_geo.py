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

from dmr_iq_surveyor.geo.commonmode import CommonModeSettings
from dmr_iq_surveyor.geo.export import to_gpx, to_kml
from dmr_iq_surveyor.geo.measurements import MeasurementSettings
from dmr_iq_surveyor.geo.model import SolveSettings
from dmr_iq_surveyor.geo.pipeline import (
    VALIDATION_NOTE,
    build_map_geojson,
    import_reference_sites,
    materialise_measurements,
    site_overview,
    solve_all_sites,
)
from dmr_iq_surveyor.geo.solver import GEOLOCATION_MATURITY
from dmr_iq_surveyor.geo.store import (
    connect_geo_database,
    latest_plan,
    solution_history,
)
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
        float | None,
        typer.Option(
            "--sigma-db",
            help=(
                "Fix shadow fading at one value instead of marginalising over the default "
                "range. Narrows the region, and states a confidence the data may not support"
            ),
        ),
    ] = None,
    resolution_m: Annotated[
        float, typer.Option("--resolution-m", help="Target fine grid resolution")
    ] = 100.0,
    margin_m: Annotated[
        float, typer.Option("--margin-m", help="Search margin beyond the measurement extent")
    ] = 25_000.0,
    min_detections: Annotated[
        int | None,
        typer.Option(
            "--min-detections",
            help="Detections required before solving at all (default 2)",
        ),
    ] = None,
    common_mode: Annotated[
        bool,
        typer.Option(
            "--common-mode/--no-common-mode",
            help=(
                "Detect and correct a per-stop level offset shared by every site heard there "
                "(a moved antenna, a raised noise floor). It is always reported; this controls "
                "whether it is applied"
            ),
        ),
    ] = True,
) -> None:
    """Estimate a credible region for every site with usable measurements."""
    # The coarse pass must never be finer than the fine pass. Scaling it with
    # the requested resolution keeps `--resolution-m 750` working instead of
    # failing validation after the operator has already asked for a solve.
    #
    # These options default to None rather than to a repeated copy of the
    # estimator's own defaults. Restating them here meant that changing a
    # default in SolveSettings silently did nothing for anyone using the CLI:
    # `--min-detections` alone still carried the value it had before the gate
    # was rewritten, so `geo solve` kept refusing sites the field app solved.
    overrides: dict[str, object] = {}
    if min_detections is not None:
        overrides["min_detections"] = min_detections
    if sigma_db is not None:
        # Asking for one sigma means one sigma. Setting only the scalar would
        # leave the solver marginalising over its default range and quietly
        # ignore the flag, which is worse than not offering it.
        overrides["sigma_db"] = sigma_db
        overrides["sigma_db_values"] = (sigma_db,)
    settings = SolveSettings(
        resolution_m=resolution_m,
        coarse_resolution_m=max(resolution_m * 5.0, 500.0),
        margin_m=margin_m,
        **overrides,  # type: ignore[arg-type]
    )
    try:
        report = solve_all_sites(
            database_path=_database(database),
            output_root=output,
            site_keys=site or None,
            solve_batch_id=batch_id,
            settings=settings,
            common_mode=CommonModeSettings(enabled=common_mode),
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
    console.print(
        f"[yellow]Geolocation maturity: {report['geolocation_maturity']}.[/yellow] "
        f"{report['validation_note']}"
    )

    offsets = report["common_mode"]
    notable = [
        offset for offset in offsets["offsets"].values() if offset["status"] != "within_noise"
    ]
    if notable:
        table = Table(title="Per-stop common-mode level offsets")
        table.add_column("Stop", style="bold")
        table.add_column("Status")
        table.add_column("Offset", justify="right")
        table.add_column("Sites", justify="right")
        table.add_column("Applied")
        for offset in sorted(notable, key=lambda item: item["survey_run_id"]):
            table.add_row(
                offset["survey_run_id"],
                offset["status"],
                f"{offset['offset_db']:+.1f} dB",
                str(offset["site_count"]),
                "yes" if offset["applied"] else "no",
            )
        console.print(table)

    plan = report["plan"]
    if plan.get("top_stops"):
        table = Table(title="Where to go next")
        table.add_column("#", justify="right")
        table.add_column("Latitude")
        table.add_column("Longitude")
        table.add_column("Value", justify="right")
        table.add_column("Helps most")
        for rank, stop in enumerate(plan["top_stops"], start=1):
            table.add_row(
                str(rank),
                f"{stop['latitude']:.5f}",
                f"{stop['longitude']:.5f}",
                f"{stop['value']:.2f}",
                ", ".join(item["site_key"] for item in stop["helps_most"]),
            )
        console.print(table)
    elif plan.get("reason"):
        console.print(f"[dim]No next-stop suggestion: {plan['reason']}[/dim]")
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
    console.print(f"[yellow]Geolocation maturity: {GEOLOCATION_MATURITY}.[/yellow] {VALIDATION_NOTE}")


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


@geo_app.command("plan")
def geo_plan(database: DatabaseOption = None) -> None:
    """Show where the next stop would teach the most."""
    connection = connect_geo_database(_database(database))
    try:
        stored = latest_plan(connection)
    finally:
        connection.close()
    if stored is None:
        console.print("No plan yet. Run `dmr-surveyor geo solve` first.")
        return
    plan = json.loads(stored["plan_json"] or "{}")
    if not plan.get("top_stops"):
        console.print(f"[yellow]{plan.get('reason') or stored['status']}[/yellow]")
        return

    table = Table(title=f"Where to go next (from solve {stored['solve_batch_id']})")
    table.add_column("#", justify="right")
    table.add_column("Latitude")
    table.add_column("Longitude")
    table.add_column("Value", justify="right")
    table.add_column("Helps most")
    for rank, stop in enumerate(plan["top_stops"], start=1):
        table.add_row(
            str(rank),
            f"{stop['latitude']:.5f}",
            f"{stop['longitude']:.5f}",
            f"{stop['value']:.2f}",
            ", ".join(item["site_key"] for item in stop["helps_most"]),
        )
    console.print(table)
    console.print(
        "[dim]Value is how unpredictable a measurement there would be: a place where a site is "
        "certainly heard, or certainly not, teaches nothing about where it is.[/dim]"
    )


@geo_app.command("export")
def geo_export(
    output: Annotated[
        Path,
        typer.Argument(
            help="File to write. The format follows the extension unless --format is given"
        ),
    ],
    database: DatabaseOption = None,
    export_format: Annotated[
        str | None,
        typer.Option(
            "--format",
            help="geojson, kml (regions over imagery in Google Earth), or gpx (stops for a navigator)",
        ),
    ] = None,
) -> None:
    """Write measurements, estimates, regions and the next-stop plan."""
    destination = Path(output).expanduser().resolve()
    chosen = (export_format or destination.suffix.lstrip(".") or "geojson").lower()
    if chosen not in ("geojson", "json", "kml", "gpx"):
        console.print(f"[bold red]Unsupported format:[/bold red] {chosen}")
        raise typer.Exit(code=1)

    try:
        connection = connect_geo_database(_database(database))
        try:
            stored = latest_plan(connection)
            visited = [
                dict(row)
                for row in connection.execute(
                    "SELECT DISTINCT survey_run_id, latitude, longitude FROM geo_measurements "
                    "WHERE latitude IS NOT NULL"
                )
            ]
        finally:
            connection.close()
        plan = json.loads((stored or {}).get("plan_json") or "{}")
        collection = build_map_geojson(database_path=_database(database))
        if stored is not None:
            plan_features = json.loads(stored["geojson"] or "{}").get("features", [])
            collection["features"].extend(plan_features)
    except (OSError, sqlite3.Error) as exc:
        console.print(f"[bold red]Export failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    if chosen == "kml":
        destination.write_text(to_kml(collection), encoding="utf-8")
        console.print(f"[green]Wrote KML to[/green] {destination}")
    elif chosen == "gpx":
        if not plan.get("top_stops"):
            console.print(
                "[yellow]No suggested stops to export.[/yellow] Run `geo solve` first."
            )
        destination.write_text(to_gpx(plan, visited=visited), encoding="utf-8")
        console.print(f"[green]Wrote GPX to[/green] {destination}")
    else:
        destination.write_text(json.dumps(collection, indent=2), encoding="utf-8")
        console.print(
            f"[green]Wrote[/green] {len(collection['features'])} features to {destination}"
        )


__all__ = ["geo_app"]
