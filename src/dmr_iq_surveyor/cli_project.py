"""The `project` sub-app: declare a project, adopt an existing database, and
say what a database currently belongs to.

Adoption is the command that matters here, and it is built so that the first
thing it does is never the thing that changes something. It inspects a file
without opening it, reports what it found and what it would write, and only a
second run with `--write` touches anything. A database is never claimed as a
side effect of being looked at.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.table import Table

from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.project.claim import (
    inspect_database,
    open_read_only,
    read_claim,
    summarise_contents,
    write_claim,
    write_manifest_atomically,
)
from dmr_iq_surveyor.project.manifest import (
    ANALYZER_P25_SITE_GEOLOCATION,
    CampaignDefaults,
    ProjectDefaults,
    ProjectError,
    load_campaign_manifest,
    load_project_manifest,
    normalise_project_id,
    render_campaign_manifest,
    render_project_manifest,
    resolve_project,
    validate_project_text,
)

project_app = typer.Typer(
    help="Projects and campaigns: manifests, adoption, and what a database belongs to."
)
campaign_app = typer.Typer(help="Campaign manifests inside one project.")
project_app.add_typer(campaign_app, name="campaign")
console = Console()

ProjectOption = Annotated[
    str,
    typer.Option(
        "--project",
        help="Project manifest path, project directory, or a name under projects/<name>/",
    ),
]


def _fail(message: str) -> None:
    console.print(f"[bold red]{message}[/bold red]")
    raise typer.Exit(code=1)


@project_app.command("validate")
def project_validate(
    manifest: Annotated[Path, typer.Argument(help="A project.yaml or a campaign manifest")],
) -> None:
    """Parse and fully check one manifest. Opens no database."""
    try:
        raw = yaml.safe_load(Path(manifest).expanduser().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        _fail(f"{manifest} could not be read: {exc}")
        return
    try:
        # A project declares an analyzer; a campaign names the project it is in.
        if isinstance(raw, dict) and "analyzer" in raw:
            project = load_project_manifest(manifest)
            console.print(
                f"[green]Project manifest is valid[/green] {project.project_id} "
                f"({project.analyzer}) -> {project.database}"
            )
        else:
            campaign = load_campaign_manifest(manifest)
            console.print(
                f"[green]Campaign manifest is valid[/green] {campaign.campaign_id} "
                f"in project {campaign.project_id}"
            )
    except (ProjectError, FileNotFoundError) as exc:
        _fail(str(exc))


@project_app.command("show")
def project_show(project: ProjectOption) -> None:
    """The manifest, the database it names, and whether that database agrees."""
    try:
        manifest = resolve_project(project)
    except (ProjectError, FileNotFoundError) as exc:
        _fail(str(exc))
        return

    table = Table(title=f"Project {manifest.project_id}")
    table.add_column("field")
    table.add_column("value")
    table.add_row("manifest", str(manifest.path))
    table.add_row("label", manifest.label)
    table.add_row("analyzer", manifest.analyzer)
    table.add_row("database", str(manifest.database))
    for key, value in manifest.defaults.to_dict().items():
        table.add_row(f"default {key}", "not set" if value is None else str(value))
    console.print(table)

    found = inspect_database(manifest.database)
    if not found.usable:
        console.print(f"[yellow]Database:[/yellow] {found.reason}")
    else:
        # Read-only by construction, so looking cannot claim.
        connection = open_read_only(manifest.database)
        try:
            claim = read_claim(connection)
            counts = summarise_contents(connection)
        finally:
            connection.close()
        if claim is None:
            console.print(
                "[yellow]Database carries no claim.[/yellow] Run "
                "`dmr-surveyor project init --adopt` to take it on."
            )
        elif claim.project_id == manifest.project_id and claim.analyzer == manifest.analyzer:
            console.print(
                f"[green]Database is claimed by this project[/green] since {claim.claimed_at} "
                f"(by version {claim.claimed_by_version})"
            )
        else:
            console.print(
                f"[bold red]Database is claimed by project {claim.project_id!r} "
                f"(analyzer {claim.analyzer!r})[/bold red], not by this manifest."
            )
        if counts:
            console.print(
                "Contents: "
                + ", ".join(f"{table_name} {count}" for table_name, count in counts.items())
            )

    campaigns = sorted(manifest.campaign_dir.glob("*.yaml")) if manifest.campaign_dir.is_dir() else []
    if not campaigns:
        console.print("No campaigns declared yet.")
        return
    listing = Table(title="Campaigns")
    listing.add_column("campaign_id")
    listing.add_column("label")
    listing.add_column("state")
    for path in campaigns:
        try:
            campaign = load_campaign_manifest(path, expect_project_id=manifest.project_id)
        except (ProjectError, FileNotFoundError) as exc:
            listing.add_row(path.stem, "-", f"[red]{exc}[/red]")
            continue
        listing.add_row(campaign.campaign_id, campaign.label, "ok")
    console.print(listing)


@project_app.command("init")
def project_init(
    project_id: Annotated[str, typer.Option("--project-id", help="Slug identifying the project")],
    label: Annotated[str, typer.Option("--label", help="Human-readable project name")],
    database: Annotated[Path, typer.Option("--database", help="The project's SQLite database")],
    manifest: Annotated[Path, typer.Option("--manifest", help="Where to write project.yaml")],
    create: Annotated[
        bool,
        typer.Option("--create", help="Make a new, empty project database. Refuses to touch an existing one"),
    ] = False,
    adopt: Annotated[
        bool,
        typer.Option("--adopt", help="Take on an existing database. Reports only, unless --write"),
    ] = False,
    write: Annotated[
        bool,
        typer.Option("--write", help="With --adopt, actually claim the database and write the manifest"),
    ] = False,
    analyzer: Annotated[str, typer.Option("--analyzer", help="Analyzer that reads this project")] = (
        ANALYZER_P25_SITE_GEOLOCATION
    ),
    band: Annotated[str | None, typer.Option("--band", help="Default band profile for this project")] = None,
    site: Annotated[str | None, typer.Option("--site", help="Default site profile for this project")] = None,
    output: Annotated[Path | None, typer.Option("--output", help="Default output root")] = None,
) -> None:
    """Create a new project database, or adopt an existing one.

    The two are separate verbs on purpose: creation must never adopt something
    that is already there, and adoption must never create.
    """
    if create == adopt:
        _fail("choose exactly one of --create or --adopt")
    try:
        resolved_id = normalise_project_id(project_id)
    except ProjectError as exc:
        _fail(str(exc))
        return
    if resolved_id is None:
        _fail("--project-id must not be empty")
        return

    manifest_path = Path(manifest).expanduser().resolve()
    database_path = Path(database).expanduser().resolve()
    defaults = ProjectDefaults(
        band=band, site=site, output=None if output is None else str(Path(output).expanduser())
    )

    # Rendered and checked while it is still a string. An invalid manifest must
    # never be the reason a database was touched.
    text = render_project_manifest(
        project_id=resolved_id,
        label=label,
        analyzer=analyzer,
        database=database_path,
        defaults=defaults,
    )
    try:
        validate_project_text(text, manifest_path)
    except ProjectError as exc:
        _fail(f"the manifest this would write is not valid: {exc}")

    if create:
        _create(database_path, manifest_path, text, resolved_id, analyzer)
    else:
        _adopt(database_path, manifest_path, text, resolved_id, analyzer, write=write)


def _refuse_existing_manifest(manifest_path: Path, text: str) -> bool:
    """True when the manifest is already exactly what would be written."""
    if not manifest_path.exists():
        return False
    current = manifest_path.read_text(encoding="utf-8")
    if current == text:
        return True
    _fail(
        f"{manifest_path} already exists and differs from what this would write. "
        "Move it aside, or edit it by hand; it will not be overwritten."
    )
    return False


def _create(
    database_path: Path, manifest_path: Path, text: str, project_id: str, analyzer: str
) -> None:
    found = inspect_database(database_path)
    if found.exists:
        _fail(
            f"{database_path} already exists. --create makes a new database; "
            "use --adopt to take on one that is already there."
        )
    identical = _refuse_existing_manifest(manifest_path, text)

    connection = connect_geo_database(database_path)
    try:
        claim = write_claim(connection, project_id=project_id, analyzer=analyzer)
    except ProjectError as exc:
        connection.close()
        _fail(str(exc))
        return
    finally:
        connection.close()
    if not identical:
        write_manifest_atomically(manifest_path, text)
    console.print(
        f"[green]Created[/green] project {claim.project_id} at {database_path}\n"
        f"Manifest: {manifest_path}"
    )


def _adopt(
    database_path: Path,
    manifest_path: Path,
    text: str,
    project_id: str,
    analyzer: str,
    *,
    write: bool,
) -> None:
    found = inspect_database(database_path)
    if not found.usable:
        _fail(found.reason)
        return

    connection = open_read_only(database_path)
    try:
        existing = read_claim(connection)
        counts = summarise_contents(connection)
    finally:
        connection.close()

    table = Table(title=f"Adopting {database_path}")
    table.add_column("what")
    table.add_column("value")
    table.add_row("size", f"{found.size_bytes / 1024:.0f} KiB")
    for name, count in counts.items():
        table.add_row(f"rows in {name}", str(count))
    table.add_row(
        "current claim",
        "none" if existing is None else f"{existing.project_id} ({existing.analyzer})",
    )
    table.add_row("would claim as", f"{project_id} ({analyzer})")
    table.add_row("would write manifest", str(manifest_path))
    console.print(table)
    console.print(
        "[bold]Adoption assigns this whole database to the project[/bold], every table and every "
        "historical run. It assigns no run to a campaign: `campaign_id` stays NULL until a run is "
        "recorded under one."
    )

    if existing is not None and (
        existing.project_id != project_id or existing.analyzer != analyzer
    ):
        _fail(
            f"already claimed by project {existing.project_id!r} (analyzer {existing.analyzer!r}); "
            "it cannot be re-claimed"
        )

    if not write:
        console.print(
            "\n[yellow]Nothing was written.[/yellow] Re-run with --write to claim the database "
            "and write the manifest."
        )
        return

    # The manifest is checked before the claim, so a refused manifest cannot
    # leave a claimed database behind.
    identical = _refuse_existing_manifest(manifest_path, text)

    connection = connect_geo_database(database_path)
    try:
        claim = write_claim(connection, project_id=project_id, analyzer=analyzer)
    except (ProjectError, sqlite3.Error) as exc:
        connection.close()
        _fail(str(exc))
        return
    finally:
        connection.close()

    if not identical:
        write_manifest_atomically(manifest_path, text)
    console.print(
        f"[green]Adopted[/green] {database_path} as project {claim.project_id} "
        f"(claimed {claim.claimed_at})\nManifest: {manifest_path}"
    )


@campaign_app.command("new")
def campaign_new(
    project: ProjectOption,
    campaign_id: Annotated[str, typer.Option("--campaign-id", help="Slug identifying the round")],
    label: Annotated[str | None, typer.Option("--label", help="Human-readable name")] = None,
    band: Annotated[str | None, typer.Option("--band", help="Band profile this round fixes")] = None,
    site: Annotated[str | None, typer.Option("--site", help="Site profile this round fixes")] = None,
) -> None:
    """Write a campaign manifest under the project. Opens no database."""
    from dmr_iq_surveyor.survey.provenance import ProvenanceError, normalise_campaign_id

    try:
        manifest = resolve_project(project)
        resolved_id = normalise_campaign_id(campaign_id)
    except (ProjectError, ProvenanceError, FileNotFoundError) as exc:
        _fail(str(exc))
        return
    if resolved_id is None:
        _fail("--campaign-id must not be empty")
        return

    destination = manifest.campaign_path(resolved_id)
    text = render_campaign_manifest(
        campaign_id=resolved_id,
        project_id=manifest.project_id,
        label=label or resolved_id,
        defaults=CampaignDefaults(band=band, site=site),
    )
    if destination.exists():
        if destination.read_text(encoding="utf-8") == text:
            console.print(f"[green]Unchanged[/green] {destination}")
            return
        _fail(f"{destination} already exists and differs; it will not be overwritten.")

    write_manifest_atomically(destination, text)
    console.print(f"[green]Wrote[/green] {destination}")


__all__ = ["campaign_app", "console", "project_app"]
