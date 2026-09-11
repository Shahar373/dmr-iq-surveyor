"""The `project` sub-app: declare a project, adopt an existing database, and
say what a database currently belongs to.

Adoption is the command that matters here, and it is built so that the first
thing it does is never the thing that changes something. It inspects a file
without opening it, reports what it found and what it would write, and only a
second run with `--write` touches anything. A database is never claimed as a
side effect of being looked at.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import typer
import yaml
from rich.console import Console
from rich.table import Table

from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.project.claim import (
    Claim,
    inspect_database,
    open_read_only,
    read_claim,
    read_contents,
    write_claim,
    write_manifest_atomically,
)
from dmr_iq_surveyor.project.manifest import (
    ANALYZER_P25_SITE_GEOLOCATION,
    CAMPAIGN_STATUS_CLOSED,
    CampaignDefaults,
    CampaignManifest,
    ProjectDefaults,
    ProjectError,
    ProjectManifest,
    load_campaign_manifest,
    load_project_manifest,
    normalise_project_id,
    render_campaign_manifest,
    render_project_manifest,
    resolve_campaign,
    resolve_project,
    set_campaign_status_text,
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
        # Read-only by construction, so looking cannot claim. `read_contents`
        # is what `--adopt` itself checks before it will write anything, so
        # `show` reports the same two facts -- SQLite's own integrity check
        # and the minimum dmr-iq-surveyor schema signature -- rather than
        # only ever knowing "claimed" or "unclaimed". A foreign or corrupt
        # file used to fall through to "no claim, run adopt"; adopt would
        # then refuse it for the same reason, so that advice was never
        # actionable for exactly the databases it was shown for.
        connection = open_read_only(manifest.database)
        try:
            contents = read_contents(connection)
            claim: Claim | None = None
            claim_error: str | None = None
            if contents.intact and contents.recognised:
                try:
                    claim = read_claim(connection)
                except ProjectError as exc:
                    claim_error = str(exc)
        finally:
            connection.close()

        if not contents.intact or not contents.recognised:
            console.print(f"[bold red]Database:[/bold red] {contents.refusal}")
        elif claim_error is not None:
            console.print(f"[bold red]Database:[/bold red] {claim_error}")
        elif claim is None:
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
        if contents.counts:
            console.print(
                "Contents: "
                + ", ".join(f"{table_name} {count}" for table_name, count in contents.counts.items())
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
        # The lifecycle, not a bare "ok". A campaign that reads correctly and
        # a campaign that may still be recorded into are two different facts,
        # and the second is the one an operator is looking for here.
        listing.add_row(campaign.campaign_id, campaign.label, campaign.status)
    console.print(listing)


@project_app.command("init")
def project_init(
    project_id: Annotated[str, typer.Option("--project-id", help="Slug identifying the project")],
    label: Annotated[str, typer.Option("--label", help="Human-readable project name")],
    database: Annotated[Path, typer.Option("--database", help="The project's SQLite database")],
    manifest: Annotated[Path, typer.Option("--manifest", help="Where to write project.yaml")],
    create: Annotated[
        bool,
        typer.Option(
            "--create",
            help=(
                "Make a new, empty project database. Refuses to touch an existing one. "
                "Reports only, unless --write"
            ),
        ),
    ] = False,
    adopt: Annotated[
        bool,
        typer.Option("--adopt", help="Take on an existing database. Reports only, unless --write"),
    ] = False,
    write: Annotated[
        bool,
        typer.Option(
            "--write",
            help="Actually write. Without it both --create and --adopt only report",
        ),
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
    if manifest_path == database_path:
        # Checked immediately after resolving both, before anything else runs
        # -- rendering the manifest text, opening the database, writing a
        # claim. `--create --write` with the two pointed at the same missing
        # path would otherwise: create the SQLite file, write a claim into
        # it, then write the manifest to "the same path", which is
        # `write_manifest_atomically`'s `os.replace` silently overwriting the
        # database it had just claimed with the manifest's YAML text. The
        # command would report success; the claim it had just written would
        # already be gone.
        _fail(
            f"--manifest and --database both resolve to {manifest_path}. A "
            "project's database and its manifest must be two different "
            "files -- writing one would silently replace the other."
        )
        return
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
        _create(database_path, manifest_path, text, resolved_id, analyzer, write=write)
    else:
        _adopt(database_path, manifest_path, text, resolved_id, analyzer, write=write)


_MANIFEST_ABSENT = "absent"
_MANIFEST_IDENTICAL = "identical"
_MANIFEST_DIFFERS = "differs"
_MANIFEST_UNREADABLE = "unreadable"

_MANIFEST_STATE_LABEL = {
    _MANIFEST_ABSENT: "would be written",
    _MANIFEST_IDENTICAL: "already identical; would be left as it is",
    _MANIFEST_DIFFERS: "[red]exists and differs; this will be refused[/red]",
    _MANIFEST_UNREADABLE: "[red]exists and cannot be read; this will be refused[/red]",
}


def _manifest_state(manifest_path: Path, text: str) -> str:
    """What is at the manifest target, compared with what would be written.

    A state rather than a refusal, so the dry run can *report* a conflict
    instead of discovering it only once `--write` is passed. "Nothing was
    written" about a run that could never have written anything reads as
    "so far, so good", which is the opposite of true.
    """
    if not manifest_path.exists():
        return _MANIFEST_ABSENT
    try:
        current = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # `UnicodeDecodeError` is not an `OSError` -- it is raised by the
        # decode step inside `read_text`, after the read itself succeeded, so
        # a file that exists, is permission-readable, and simply is not valid
        # UTF-8 (binary junk, a different encoding) used to escape this
        # `except` entirely and crash `_create`/`_adopt` with a raw
        # traceback instead of the refusal the table already promises.
        return _MANIFEST_UNREADABLE
    return _MANIFEST_IDENTICAL if current == text else _MANIFEST_DIFFERS


def _existing_claim(database_path: Path) -> Claim | None:
    """The claim on an existing database, read without being able to write."""
    connection = open_read_only(database_path)
    try:
        return read_claim(connection)
    finally:
        connection.close()


def _resumable(database_path: Path, project_id: str, analyzer: str) -> bool:
    """Whether an existing database is one a previous `--create` left behind.

    Creation writes the database and its claim first and the manifest second,
    so a crash between the two leaves a claimed database and no manifest.
    Without this, re-running would hit "already exists" and the operator would
    be stuck with a state no command could finish -- so a database carrying
    *exactly* this project's claim is treated as the half-done creation it is,
    and the re-run completes it by writing the manifest.

    Any other existing database -- claimed by someone else, or claimed by
    nobody -- is still refused. Those are adoption's business, not creation's.
    """
    found = inspect_database(database_path)
    if not found.usable:
        return False
    existing = _existing_claim(database_path)
    return (
        existing is not None
        and existing.project_id == project_id
        and existing.analyzer == analyzer
    )


def _create(
    database_path: Path,
    manifest_path: Path,
    text: str,
    project_id: str,
    analyzer: str,
    *,
    write: bool,
) -> None:
    found = inspect_database(database_path)
    resuming = False
    if found.exists:
        try:
            resuming = _resumable(database_path, project_id, analyzer)
        except ProjectError as exc:
            # A `project_meta` table that exists but cannot be read is not the
            # same fact as "claimed by someone else" or "not claimed at all" --
            # it means this existing database cannot be trusted to say whether
            # it is safe to resume, so it is refused here, before anything is
            # compared or written, rather than silently treated as fine to
            # write over.
            _fail(f"{database_path} exists but its claim could not be read: {exc}")
            return
    if found.exists and not resuming:
        _fail(
            f"{database_path} already exists. --create makes a new database; "
            "use --adopt to take on one that is already there."
        )

    manifest_state = _manifest_state(manifest_path, text)

    table = Table(title=f"Creating {database_path}")
    table.add_column("what")
    table.add_column("value")
    table.add_row(
        "database",
        "would be created"
        if not resuming
        else "[yellow]already created and claimed by this project; would be left as it is[/yellow]",
    )
    table.add_row("would claim as", f"{project_id} ({analyzer})")
    table.add_row("manifest", str(manifest_path))
    table.add_row("manifest at target", _MANIFEST_STATE_LABEL[manifest_state])
    console.print(table)

    # Reported before the dry run can say "nothing was written", and refused
    # in the dry run too: a run that cannot succeed must not look like one
    # that is merely waiting for --write. Both bad states are refused here,
    # not just the one that differs -- a target this command cannot read to
    # compare is exactly as unsafe to write over as one it read and found
    # different, and the table above already says "this will be refused"
    # for both.
    if manifest_state in (_MANIFEST_DIFFERS, _MANIFEST_UNREADABLE):
        _fail(
            f"{manifest_path} already exists and "
            f"{'differs from' if manifest_state == _MANIFEST_DIFFERS else 'cannot be read to compare with'} "
            "what this would write. Move it aside, or edit it by hand; it will not be "
            "overwritten."
        )

    if not write:
        console.print(
            "\n[yellow]Nothing was written.[/yellow] Re-run with --write to create the "
            "database and write the manifest."
        )
        return

    created_here = not found.exists
    connection = connect_geo_database(database_path)
    try:
        claim = write_claim(connection, project_id=project_id, analyzer=analyzer)
    except (ProjectError, sqlite3.Error) as exc:
        connection.close()
        if created_here:
            _remove_database(database_path)
        _fail(str(exc))
        return
    finally:
        connection.close()

    if manifest_state != _MANIFEST_IDENTICAL:
        try:
            write_manifest_atomically(manifest_path, text)
        except OSError as exc:
            # Roll the creation back. This database did not exist when this
            # command started and holds nothing but the claim this command
            # just wrote, so removing it restores the filesystem exactly.
            # A database that exists, is claimed, and has no manifest is the
            # one state creation must not leave behind.
            if created_here:
                _remove_database(database_path)
                _fail(f"the manifest could not be written ({exc}); the new database was removed")
            _fail(f"the manifest could not be written ({exc})")
            return

    console.print(
        f"[green]{'Completed' if resuming else 'Created'}[/green] project {claim.project_id} "
        f"at {database_path}\nManifest: {manifest_path}"
    )


def _remove_database(database_path: Path) -> None:
    """Undo a creation this command made, siblings included."""
    database_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm", "-journal"):
        database_path.with_name(database_path.name + suffix).unlink(missing_ok=True)


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

    # A read-only connection, so everything below -- the integrity check, the
    # schema signature, the row counts, the existing claim -- is established
    # without the file being writable at all. `mode=ro` refuses every write,
    # which is what lets a foreign file be examined and then left exactly as
    # it was found.
    connection = open_read_only(database_path)
    try:
        try:
            existing = read_claim(connection)
        except ProjectError as exc:
            _fail(f"{database_path} could not be read: {exc}")
            return
        contents = read_contents(connection)
    finally:
        connection.close()
    counts = contents.counts
    manifest_state = _manifest_state(manifest_path, text)

    table = Table(title=f"Adopting {database_path}")
    table.add_column("what")
    table.add_column("value")
    table.add_row("size", f"{found.size_bytes / 1024:.0f} KiB")
    table.add_row("integrity", contents.integrity)
    table.add_row(
        "schema",
        "dmr-iq-surveyor" if contents.recognised else "[red]not recognised[/red]",
    )
    for name, count in counts.items():
        table.add_row(f"rows in {name}", str(count))
    table.add_row(
        "current claim",
        "none" if existing is None else f"{existing.project_id} ({existing.analyzer})",
    )
    table.add_row("would claim as", f"{project_id} ({analyzer})")
    table.add_row("would write manifest", str(manifest_path))
    table.add_row("manifest at target", _MANIFEST_STATE_LABEL[manifest_state])
    console.print(table)

    # Before anything is said about what would be written. A file that is not
    # this project's database is not adoptable in a dry run either, and saying
    # "nothing was written" about it would read as "so far, so good".
    if contents.refusal:
        _fail(contents.refusal)

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

    # Before "nothing was written", not after it and not only under --write.
    # The refusal is the same either way; what changes is that the dry run now
    # tells the operator now rather than on the run they expected to succeed.
    if manifest_state in (_MANIFEST_DIFFERS, _MANIFEST_UNREADABLE):
        _fail(
            f"{manifest_path} already exists and {'differs from' if manifest_state == _MANIFEST_DIFFERS else 'cannot be read to compare with'} "
            "what this would write. Move it aside, or edit it by hand; it will not be "
            "overwritten."
        )

    if not write:
        console.print(
            "\n[yellow]Nothing was written.[/yellow] Re-run with --write to claim the database "
            "and write the manifest."
        )
        return

    connection = connect_geo_database(database_path)
    try:
        claim = write_claim(connection, project_id=project_id, analyzer=analyzer)
    except (ProjectError, sqlite3.Error) as exc:
        connection.close()
        _fail(str(exc))
        return
    finally:
        connection.close()

    if manifest_state != _MANIFEST_IDENTICAL:
        write_manifest_atomically(manifest_path, text)
    console.print(
        f"[green]Adopted[/green] {database_path} as project {claim.project_id} "
        f"(claimed {claim.claimed_at})\nManifest: {manifest_path}"
    )


def _campaign_files(manifest: ProjectManifest) -> list[Path]:
    """Every campaign file declared under a project, in name order."""
    if not manifest.campaign_dir.is_dir():
        return []
    return sorted(manifest.campaign_dir.glob("*.yaml"))


def _runs_per_campaign(database: Path) -> dict[str | None, int]:
    """How many survey runs each campaign holds, read without being able to
    write.

    A database that is missing, unreadable or older than `campaign_id`
    answers "nothing known" rather than raising: listing campaigns is a
    reporting command, and it has to keep working on a laptop that has the
    manifests but not the field database.
    """
    if not database.is_file():
        return {}
    try:
        # Quoted, because the path is going into a URI. A `#` in it starts a
        # fragment: it truncated both the rest of the path AND `?mode=ro`, so
        # a command documented as writing nothing opened a different file
        # read-WRITE and created it. `%` was mishandled the same way.
        connection = sqlite3.connect(
            f"file:{quote(str(database))}?mode=ro", uri=True
        )
    except sqlite3.Error:
        return {}
    try:
        rows = connection.execute(
            "SELECT campaign_id, COUNT(*) FROM survey_runs GROUP BY campaign_id"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return {row[0]: int(row[1]) for row in rows}


def _campaign_summary(
    manifest: ProjectManifest, path: Path, runs: dict[str | None, int]
) -> dict[str, Any]:
    """One campaign as a row of facts, or as the problem that stopped it
    being read.

    A manifest that does not parse is reported in its own row and never
    raises. One unreadable file among twenty is exactly when an operator
    needs the list most, and a traceback would take the other nineteen away.
    """
    try:
        campaign = load_campaign_manifest(path, expect_project_id=manifest.project_id)
    except (ProjectError, FileNotFoundError, OSError) as exc:
        return {
            "campaign_id": path.stem,
            "manifest": str(path),
            "problem": str(exc),
        }
    defaults = campaign.defaults
    return {
        "campaign_id": campaign.campaign_id,
        "label": campaign.label,
        "status": campaign.status,
        "manifest": str(campaign.path),
        "runs": runs.get(campaign.campaign_id, 0),
        # The values that actually apply: what the round pins, or what it
        # inherits from the project when it pins nothing.
        "band": defaults.band or manifest.defaults.band,
        "site": defaults.site or manifest.defaults.site,
        "hardware": defaults.hardware or manifest.defaults.hardware,
        "capture": dict(defaults.capture),
        "problem": None,
    }


@campaign_app.command("list")
def campaign_list(
    project: ProjectOption,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Machine-readable output, for scripts and fieldctl"),
    ] = False,
    current: Annotated[
        str | None,
        typer.Option(
            "--current",
            help=(
                "Mark this campaign as the one being recorded into. Passed in by the "
                "caller, because which campaign a deployment records into is the "
                "deployment's state and is not written in any manifest"
            ),
        ),
    ] = None,
) -> None:
    """Every campaign declared under a project, with its lifecycle and size.

    Reads manifests and, if it is there, the project's database. Opens no
    SDR, writes nothing, and needs no privileges.
    """
    try:
        manifest = resolve_project(project)
    except (ProjectError, FileNotFoundError) as exc:
        _fail(str(exc))
        return

    runs = _runs_per_campaign(manifest.database)
    summaries = [
        _campaign_summary(manifest, path, runs) for path in _campaign_files(manifest)
    ]
    for summary in summaries:
        summary["current"] = current is not None and summary["campaign_id"] == current

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "project_id": manifest.project_id,
                    "manifest": str(manifest.path),
                    "database": str(manifest.database),
                    "unassigned_runs": runs.get(None, 0),
                    "campaigns": summaries,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    if not summaries:
        console.print(f"No campaigns declared under {manifest.campaign_dir}.")
        return

    table = Table(title=f"Campaigns in {manifest.project_id}")
    for column in ("campaign_id", "label", "status", "current", "runs", "band", "site", "hardware"):
        table.add_column(column)
    for summary in summaries:
        if summary["problem"]:
            table.add_row(
                summary["campaign_id"],
                f"[red]unreadable: {summary['problem']}[/red]",
                "[red]?[/red]",
                "",
                "",
                "",
                "",
                "",
            )
            continue
        table.add_row(
            summary["campaign_id"],
            summary["label"],
            summary["status"],
            "yes" if summary["current"] else "",
            str(summary["runs"]),
            summary["band"] or "-",
            summary["site"] or "-",
            summary["hardware"] or "-",
        )
    console.print(table)
    unreadable = [summary for summary in summaries if summary["problem"]]
    if unreadable:
        console.print(
            f"[yellow]{len(unreadable)} campaign manifest(s) could not be read.[/yellow] "
            "They are listed above with the reason; nothing else is affected."
        )
    if runs.get(None):
        console.print(
            f"{runs[None]} run(s) carry no campaign at all -- the rounds recorded before "
            "campaigns existed. Nothing here assigns them to one."
        )


@campaign_app.command("close")
def campaign_close(
    project: ProjectOption,
    campaign_id: Annotated[str, typer.Option("--campaign-id", help="The round to close")],
    write: Annotated[
        bool,
        typer.Option("--write", help="Actually write. Without it this only reports"),
    ] = False,
) -> None:
    """Mark a campaign closed: no new stops, everything still readable.

    This is the manifest half of closing a round. It does not know, and
    cannot know, whether a service is currently recording into this campaign
    -- that is deployment state -- so on a Pi use `fieldctl campaign close`,
    which checks that first and then calls this.

    Changes no database row and deletes nothing.
    """
    try:
        manifest = resolve_project(project)
        campaign = resolve_campaign(manifest, campaign_id)
    except (ProjectError, FileNotFoundError) as exc:
        _fail(str(exc))
        return

    table = Table(title=f"Closing {campaign.campaign_id}")
    table.add_column("what")
    table.add_column("value")
    table.add_row("manifest", str(campaign.path))
    table.add_row("status now", campaign.status)
    table.add_row("status after", CAMPAIGN_STATUS_CLOSED)
    table.add_row("database", "untouched -- no row is changed, moved or deleted")
    console.print(table)

    if campaign.is_closed:
        console.print(f"[green]Already closed[/green] {campaign.path}")
        return

    try:
        original = campaign.path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"{campaign.path} could not be read: {exc}")
        return
    updated = set_campaign_status_text(original, CAMPAIGN_STATUS_CLOSED)

    if not write:
        console.print(
            "\n[yellow]Nothing was written.[/yellow] Re-run with --write to close the "
            "campaign. It stays readable and analysable afterwards; what stops is new "
            "acquisition."
        )
        return

    _refuse_symlink(campaign.path)
    write_manifest_atomically(campaign.path, updated)
    # Read back through the real loader rather than trusting the text this
    # command produced. A file that no longer parses is one the service
    # would refuse at startup, and the time to find that out is now.
    try:
        confirmed = load_campaign_manifest(
            campaign.path, expect_project_id=manifest.project_id, expect_campaign_id=campaign.campaign_id
        )
    except (ProjectError, FileNotFoundError) as exc:
        write_manifest_atomically(campaign.path, original)
        _fail(f"the closed manifest did not validate ({exc}); the file was restored")
        return
    if not confirmed.is_closed:
        write_manifest_atomically(campaign.path, original)
        _fail("the manifest did not read back as closed; the file was restored")
        return
    console.print(f"[green]Closed[/green] {campaign.path}")


def _refuse_symlink(path: Path) -> None:
    """A manifest that is a symlink is refused rather than written through.

    `write_manifest_atomically` replaces the path it is given, which for a
    symlink means replacing the link with a regular file -- so the file the
    operator believes they are editing is left untouched and the link they
    set up is gone.

    Checked on the path as given. A path that came back from
    `load_campaign_manifest` has already been resolved and therefore names
    the target rather than the link, which is exactly what should be written
    and is why this passes for one; a path this module assembled itself, as
    `campaign new` does, has not, and is where the refusal bites.
    """
    if path.is_symlink():
        _fail(
            f"{path} is a symbolic link. Writing would replace the link with a regular "
            "file and leave its target unchanged, so this is refused. Edit the target "
            "directly, or remove the link."
        )


@campaign_app.command("new")
def campaign_new(
    project: ProjectOption,
    campaign_id: Annotated[str, typer.Option("--campaign-id", help="Slug identifying the round")],
    label: Annotated[str | None, typer.Option("--label", help="Human-readable name")] = None,
    band: Annotated[str | None, typer.Option("--band", help="Band profile this round fixes")] = None,
    site: Annotated[str | None, typer.Option("--site", help="Site profile this round fixes")] = None,
    hardware: Annotated[
        str | None,
        typer.Option("--hardware", help="Hardware profile this round is run with"),
    ] = None,
    center_frequency_hz: Annotated[
        float | None,
        typer.Option("--center-frequency-hz", help="Tuner centre frequency this round pins"),
    ] = None,
    sample_rate_hz: Annotated[
        float | None,
        typer.Option("--sample-rate-hz", help="Sample rate this round pins"),
    ] = None,
    duration_seconds: Annotated[
        float | None,
        typer.Option("--duration-seconds", help="Stop duration this round pins"),
    ] = None,
    copy_from: Annotated[
        str | None,
        typer.Option(
            "--from",
            help=(
                "Copy band, site, hardware and capture settings from this campaign in "
                "the same project. An explicit flag still wins over what is copied"
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Report what would be written and write nothing",
        ),
    ] = False,
) -> None:
    """Write a campaign manifest under the project. Opens no database.

    A new round usually continues the last one: same radio, same band, same
    place, same capture settings, a different day. `--from` copies those from
    an existing campaign so the second round of a survey is declared by
    naming what changed rather than by retyping what did not.

    Nothing is ever overwritten. A manifest that already exists and says
    something else is a refusal, not a merge.
    """
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

    source: CampaignManifest | None = None
    if copy_from is not None:
        try:
            source = resolve_campaign(manifest, copy_from)
        except (ProjectError, FileNotFoundError) as exc:
            _fail(f"--from {copy_from!r} could not be read: {exc}")
            return

    inherited = source.defaults if source is not None else CampaignDefaults()
    capture = dict(inherited.capture)
    for key, value in (
        ("center_frequency_hz", center_frequency_hz),
        ("sample_rate_hz", sample_rate_hz),
        ("duration_seconds", duration_seconds),
    ):
        if value is not None:
            capture[key] = value

    defaults = CampaignDefaults(
        band=band if band is not None else inherited.band,
        site=site if site is not None else inherited.site,
        hardware=hardware if hardware is not None else inherited.hardware,
        capture=capture,
    )

    # The filename is how a campaign is found: `resolve_campaign` asks for
    # this exact path and reads whatever is there, so the name and the id
    # inside it have to agree byte for byte. Built from the validated id
    # rather than from what was typed.
    destination = manifest.campaign_path(resolved_id)
    # The leaf is checked below; the DIRECTORY is checked here, because
    # `write_manifest_atomically` resolves the path it is given and a
    # symlinked `campaigns/` therefore sends the bytes somewhere else while
    # every message still names the path inside the project.
    if manifest.campaign_dir.is_symlink():
        _fail(
            f"{manifest.campaign_dir} is a symbolic link. A manifest written through it "
            "would land outside the project while every message here named a path inside "
            "it, so this is refused. Point the project at the real directory."
        )
        return
    text = render_campaign_manifest(
        campaign_id=resolved_id,
        project_id=manifest.project_id,
        label=label or (source.label if source is not None else resolved_id),
        defaults=defaults,
    )

    table = Table(title=f"Campaign {resolved_id}")
    table.add_column("what")
    table.add_column("value")
    table.add_row("manifest", str(destination))
    table.add_row("project", f"{manifest.project_id} ({manifest.path})")
    table.add_row("copied from", str(source.path) if source is not None else "nothing")
    for key, value in defaults.to_dict().items():
        if key == "capture":
            continue
        inherited_from_project = value is None and getattr(manifest.defaults, key, None)
        table.add_row(
            key,
            str(value)
            if value is not None
            else (
                f"{inherited_from_project} (inherited from the project)"
                if inherited_from_project
                else "not set"
            ),
        )
    table.add_row(
        "capture",
        ", ".join(f"{key}={value:g}" for key, value in sorted(capture.items()))
        if capture
        else "not pinned",
    )
    console.print(table)

    if destination.exists():
        # Read as text and compared, exactly as before: a re-run that would
        # write the same bytes is a no-op worth saying out loud, and anything
        # else is refused rather than merged.
        try:
            existing = destination.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _fail(f"{destination} already exists and could not be read to compare: {exc}")
            return
        if existing == text:
            console.print(f"[green]Unchanged[/green] {destination}")
            return
        _fail(f"{destination} already exists and differs; it will not be overwritten.")

    if dry_run:
        console.print(
            "\n[yellow]Nothing was written.[/yellow] Re-run without --dry-run to write "
            "the manifest. Writing one changes no deployment: it does not switch the "
            "campaign being recorded into and does not restart anything."
        )
        return

    _refuse_symlink(destination)
    write_manifest_atomically(destination, text)
    # Read back through the loader every other command uses, so a manifest
    # this command wrote can never be one the service would refuse at
    # startup.
    try:
        load_campaign_manifest(
            destination,
            expect_project_id=manifest.project_id,
            expect_campaign_id=resolved_id,
        )
    except (ProjectError, FileNotFoundError) as exc:
        destination.unlink(missing_ok=True)
        _fail(f"the manifest this wrote did not validate ({exc}); it was removed")
        return
    console.print(f"[green]Wrote[/green] {destination}")


__all__ = ["campaign_app", "console", "project_app"]
