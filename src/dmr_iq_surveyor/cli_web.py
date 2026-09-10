"""`dmr-surveyor web` -- serve the field control app from the Raspberry Pi.

Mounted additively; no existing command changes.
"""

from __future__ import annotations

import re
import secrets
import socket
import sqlite3
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from dmr_iq_surveyor.inventory.store import connect_database
from dmr_iq_surveyor.project.binding import project_binding
from dmr_iq_surveyor.project.manifest import (
    CampaignManifest,
    ProjectError,
    ProjectManifest,
    resolve_campaign,
    resolve_project,
    resolve_setting,
)
from dmr_iq_surveyor.survey.pipeline import DEFAULT_DATABASE_PATH
from dmr_iq_surveyor.survey.profiles import (
    ProfileError,
    SiteProfile,
    resolve_band_profile,
    resolve_site_profile,
)
from dmr_iq_surveyor.survey.provenance import ProvenanceError, normalise_campaign_id
from dmr_iq_surveyor.web.recordings import GIB, disk_status
from dmr_iq_surveyor.web.server import serve_forever
from dmr_iq_surveyor.web.service import FieldSettings
from dmr_iq_surveyor.web.tls import (
    Certificate,
    TlsUnavailable,
    ensure_self_signed,
    load_certificate,
)

web_app = typer.Typer(
    no_args_is_help=True,
    help="Field web app: mark a position, record, watch progress, view geolocation results.",
)
console = Console()


def _local_addresses(port: int, scheme: str = "http") -> list[str]:
    """Best-effort list of URLs the phone can use.

    Uses a UDP socket's chosen source address rather than resolving the
    hostname: on a Pi tethered to a phone hotspot the hostname usually
    resolves to 127.0.1.1, which is exactly the address that will not work
    from the phone.
    """
    addresses: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 1))  # TEST-NET-1: routed nowhere, never sends a packet
        addresses.append(f"{scheme}://{probe.getsockname()[0]}:{port}")
    except OSError:
        pass
    finally:
        probe.close()
    addresses.append(f"{scheme}://localhost:{port}")
    return addresses


def _resolve_capture_gain(
    if_gain_reduction: float | None,
    lna_state: int | None,
    site_profile: SiteProfile,
) -> tuple[float, int, list[str]]:
    """Resolve the field app's default capture gain, and say where it came from.

    Precedence: an explicit CLI flag always wins. Otherwise the resolved
    site profile -- the one file this project's whole field guide tells an
    operator to fill in -- seeds it silently, since that is a deliberate
    choice already recorded. Only when NEITHER says anything does this fall
    back to a hardcoded default, and that fallback is always reported: an
    unconfirmed default is exactly the risk the project's gain-discipline
    checks (`gain_differs_from_campaign`) exist to catch after the fact, so
    it is better caught here, before a single stop is recorded.
    """
    notices: list[str] = []

    resolved_gain = if_gain_reduction
    if resolved_gain is None:
        if site_profile.gain is not None:
            resolved_gain = site_profile.gain
        else:
            resolved_gain = 40.0
            notices.append(
                f"no gain recorded in the {site_profile.site_id!r} site profile; "
                f"defaulting IF gain reduction to {resolved_gain:g} dB -- confirm this in the "
                "app before recording"
            )

    resolved_lna = lna_state
    if resolved_lna is None:
        if site_profile.lna_state is not None:
            resolved_lna = site_profile.lna_state
        else:
            resolved_lna = 2
            notices.append(
                f"no LNA state recorded in the {site_profile.site_id!r} site profile; "
                f"defaulting to LNA state {resolved_lna} -- confirm this in the app before "
                "recording"
            )

    return resolved_gain, resolved_lna, notices


# The token travels in a URL (`?token=...`), because the page has to load
# before `app.js` can read the token out of its own address and start sending
# it as a header -- see web/server.py's `_serve_static`. So the value has to
# survive a query string intact: anything outside the URL-unreserved set is
# rejected up front rather than producing an app that silently cannot
# authenticate. Non-ASCII in particular is the case web/server.py documents as
# having dropped the connection with no response at all.
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~-]+$")


class TokenError(ValueError):
    """The shared token could not be resolved from what was given."""


@dataclass(frozen=True, slots=True)
class ResolvedToken:
    """A resolved token, and where it came from.

    The source is not bookkeeping: it decides whether the value may be
    printed. A token the operator typed, or asked this process to generate,
    is already on their screen and printing it back is how they get the URL.
    A token read from a file exists precisely so that it never reaches a log
    -- under systemd this banner goes straight to the journal.
    """

    value: str | None
    source: str  # "none" | "flag" | "generated" | "file"


def _read_token_file(path: Path) -> str:
    """Read a token from `path`, refusing anything that cannot safely be one.

    Every failure here is a refusal rather than a fallback, and the empty
    case is why. `web/server.py`'s `_authorised` treats a falsy token as
    "this server has no token", which authorises *every* request. A token
    file that is empty -- truncated by a failed write, or created by a
    `touch` that was never followed up -- would therefore turn an app
    reachable over Tailscale into an open one, silently. Refusing to start
    is the safe direction.
    """
    resolved = Path(path).expanduser()
    try:
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise TokenError(f"token file {str(resolved)!r} could not be read: {exc}") from exc
    if stat.S_ISDIR(mode):
        raise TokenError(f"token file {str(resolved)!r} is a directory")
    if mode & 0o077:
        raise TokenError(
            f"token file {str(resolved)!r} is readable by more than its owner "
            f"(mode {stat.S_IMODE(mode):04o}). Run: chmod 600 {resolved}"
        )
    try:
        raw = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TokenError(f"token file {str(resolved)!r} could not be read: {exc}") from exc
    # Stripped because the ordinary way to write one of these is `echo`, and a
    # trailing newline is not part of the secret.
    value = raw.strip()
    if not value:
        raise TokenError(
            f"token file {str(resolved)!r} is empty. An empty token would disable "
            "authentication for every request, so it is refused rather than served."
        )
    if not _TOKEN_PATTERN.match(value):
        raise TokenError(
            f"token file {str(resolved)!r} contains characters that cannot survive a "
            "URL query string; use only A-Z a-z 0-9 and . _ ~ -"
        )
    return value


def resolve_token(token: str | None, token_file: Path | None) -> ResolvedToken:
    """Resolve the shared token from `--token` or `--token-file`.

    `--token`'s three existing behaviours are unchanged: absent means no
    authentication, `auto` mints a fresh one, anything else is used verbatim.
    """
    if token is not None and token_file is not None:
        raise TokenError(
            "--token and --token-file cannot be given together; pass one or the other"
        )
    if token_file is not None:
        return ResolvedToken(_read_token_file(token_file), "file")
    if token == "auto":
        return ResolvedToken(secrets.token_urlsafe(12), "generated")
    if token is None:
        return ResolvedToken(None, "none")
    return ResolvedToken(token, "flag")


def token_query_suffix(resolved: ResolvedToken) -> str:
    """The `?token=...` a printed URL may carry, or "" when it may not.

    A file-sourced token is withheld deliberately. It is the one source whose
    whole purpose is to keep the secret out of this process's output, and the
    startup banner is captured by journald when the app runs as a service.
    """
    # `not resolved.value`, not `is None`: `--token ""` resolves to an empty
    # string, which `_authorised` treats as "no token configured". It printed
    # a bare URL before this helper existed and must keep doing so.
    if not resolved.value or resolved.source == "file":
        return ""
    return f"?token={resolved.value}"


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """Everything a resolved `--project` decides, before anything is opened."""

    manifest: ProjectManifest
    campaign: CampaignManifest | None
    band: str
    site: str
    output: Path
    database: Path
    capture: dict[str, float]
    notices: list[str]


def _flag_was_typed(ctx: typer.Context | None, name: str) -> bool:
    """Whether the operator actually passed this flag, or it is the default.

    `--band` has a non-None default, so its value alone cannot say whether it
    was chosen or merely inherited -- and the difference decides whether a
    manifest is overridden or a conflict is refused. Click records the source
    of every parameter, which is the only honest answer.
    """
    if ctx is None:
        return False
    # Click answers `None` for a name it does not know, which would quietly
    # read as "not typed" and turn a misspelled parameter name here into a
    # manifest that silently wins over an explicit flag. The names are
    # Python identifiers, not the `--dashed` spellings.
    known = {parameter.name for parameter in getattr(ctx.command, "params", ())}
    if known and name not in known:
        raise KeyError(f"{name!r} is not a parameter of this command; known: {sorted(known)}")
    source = ctx.get_parameter_source(name)
    return getattr(source, "name", "") == "COMMANDLINE"


def _same_band(flag: Any, manifest: Any) -> bool:
    """Whether two spellings name the same band profile.

    A band may be given as a name or as a path to the YAML that name resolves
    to, so comparing the strings would refuse `--band central_800_narrow`
    against a manifest holding `config/bands/central_800_narrow.yaml` -- a
    conflict that does not exist. Both sides are resolved and compared by
    content, which also makes a copied profile agree with its original while
    two genuinely different profiles still disagree.

    A side that will not resolve is not equivalent to anything. It fails a
    moment later in profile resolution with a better message than this one
    could give, and the conservative answer here is the safe one: a conflict
    is refused rather than silently resolved in the manifest's favour.
    """
    if str(flag) == str(manifest):
        return True
    try:
        return (
            resolve_band_profile(str(flag)).to_dict()
            == resolve_band_profile(str(manifest)).to_dict()
        )
    except (ProfileError, FileNotFoundError, OSError):
        return False


def _resolve_project_context(
    ctx: typer.Context | None,
    *,
    project: str,
    campaign: str | None,
    band: str,
    site: str,
    output: Path,
    database: Path | None,
) -> ProjectContext:
    """Resolve the project, its campaign and the settings they decide.

    Reads files only. No database is opened here and none is bound, so a
    manifest that turns out to be wrong costs nothing.
    """
    manifest = resolve_project(project)
    resolved_campaign = resolve_campaign(manifest, campaign) if campaign else None

    campaign_defaults = resolved_campaign.defaults if resolved_campaign else None
    band_choice = resolve_setting(
        "band",
        flag=band,
        flag_explicit=_flag_was_typed(ctx, "band"),
        campaign=campaign_defaults.band if campaign_defaults else None,
        project=manifest.defaults.band,
        equivalent=_same_band,
    )
    site_choice = resolve_setting(
        "site",
        flag=site,
        flag_explicit=_flag_was_typed(ctx, "site"),
        campaign=campaign_defaults.site if campaign_defaults else None,
        project=manifest.defaults.site,
    )
    output_choice = resolve_setting(
        "output",
        flag=output,
        flag_explicit=_flag_was_typed(ctx, "output"),
        project=Path(manifest.defaults.output) if manifest.defaults.output else None,
    )

    # An explicit `--database` may point somewhere else -- at a second
    # database of the same project, say -- but it is not exempt from the
    # guard: the binding below is made against whatever wins here, and
    # opening it then requires it to exist and to carry this project's
    # claim.
    if _flag_was_typed(ctx, "database") and database is not None:
        database_choice = database
    else:
        database_choice = manifest.database

    notices = [
        f"Project {manifest.project_id!r} ({manifest.label}) from {manifest.path}",
        f"Analyzer {manifest.analyzer}, database {database_choice}",
    ]
    if resolved_campaign is not None:
        notices.append(
            f"Campaign {resolved_campaign.campaign_id!r} ({resolved_campaign.label}) "
            f"from {resolved_campaign.path}"
        )
    else:
        notices.append(
            "No campaign given: project defaults apply and stops recorded here stay unassigned"
        )
    for key, choice in (("band", band_choice), ("site", site_choice), ("output", output_choice)):
        if choice.from_manifest:
            notices.append(f"{key} {choice.value!s} from the {choice.origin} manifest")

    return ProjectContext(
        manifest=manifest,
        campaign=resolved_campaign,
        band=str(band_choice.value),
        site=str(site_choice.value),
        output=Path(output_choice.value),
        database=Path(database_choice).expanduser().resolve(),
        capture=dict(campaign_defaults.capture) if campaign_defaults else {},
        notices=notices,
    )


@web_app.command("serve")
def web_serve(
    ctx: typer.Context,
    host: Annotated[
        str,
        typer.Option(
            help=(
                "Interface to bind. Defaults to loopback; use 0.0.0.0 to reach it from a "
                "phone on the same network"
            )
        ),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="TCP port")] = 8765,
    database: Annotated[
        Path | None, typer.Option("--database", help="Persistent inventory SQLite path")
    ] = None,
    band: Annotated[
        str,
        typer.Option(
            help=(
                "Band profile name or path. The default matches what one 5 MS/s capture at "
                "867.40625 MHz can actually cover; central_800 asks for 866-870 MHz and would "
                "report every run as partial coverage"
            )
        ),
    ] = "central_800_narrow",
    site: Annotated[
        str, typer.Option(help="Site profile name or path, providing the fixed equipment context")
    ] = "home",
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            help=(
                "Project manifest path, or a name looked up as "
                "projects/<name>/project.yaml. Serves that project: its database must "
                "already exist and already carry its claim, and this process will open "
                "no other. Left unset nothing binds and the app behaves exactly as it "
                "always has"
            ),
        ),
    ] = None,
    campaign: Annotated[
        str | None,
        typer.Option(
            "--campaign",
            help=(
                "Collection round this run belongs to, e.g. 2026-09_day1. Lower case, "
                "digits, '.', '_' and '-'. Left unset the run is unassigned, which is "
                "what every run recorded before campaigns existed is. With --project it "
                "also selects campaigns/<id>.yaml, which must already exist"
            ),
        ),
    ] = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Root for survey outputs and reports")
    ] = Path("runs/field"),
    recordings: Annotated[
        Path | None, typer.Option(help="Where captures are written; defaults to <output>/recordings")
    ] = None,
    center_frequency: Annotated[
        float, typer.Option("--center-frequency", help="Default tuner centre frequency in Hz")
    ] = 867_406_250.0,
    sample_rate: Annotated[
        float, typer.Option("--sample-rate", help="Default sample rate in samples/s")
    ] = 5_000_000.0,
    duration: Annotated[
        float,
        typer.Option(
            help=(
                "Default capture duration in seconds. 90 s at 5 MS/s is 1.68 GiB per stop"
            )
        ),
    ] = 90.0,
    if_gain_reduction: Annotated[
        float | None,
        typer.Option(
            "--if-gain-reduction",
            help=(
                "Default SDRplay IF gain reduction in dB; keep it identical at every stop. "
                "Falls back to the resolved site profile's `gain`, then to 40.0 if neither "
                "is set -- an explicit flag here always wins over the site profile"
            ),
        ),
    ] = None,
    lna_state: Annotated[
        int | None,
        typer.Option(
            "--lna-state",
            help=(
                "Default SDRplay LNA state. Falls back to the site profile's `lna_state`, "
                "then to 2 if neither is set"
            ),
        ),
    ] = None,
    driver: Annotated[str, typer.Option(help="SoapySDR driver name")] = "sdrplay",
    map_latitude: Annotated[
        float, typer.Option("--map-latitude", help="Initial map centre latitude")
    ] = 32.0853,
    map_longitude: Annotated[
        float, typer.Option("--map-longitude", help="Initial map centre longitude")
    ] = 34.7818,
    map_zoom: Annotated[int, typer.Option("--map-zoom", help="Initial map zoom")] = 11,
    tile_url: Annotated[
        str, typer.Option("--tile-url", help="Map tile template; point it at a local cache offline")
    ] = "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    tile_attribution: Annotated[
        str, typer.Option("--tile-attribution", help="Attribution shown on the map")
    ] = "(c) OpenStreetMap contributors",
    token: Annotated[
        str | None,
        typer.Option(
            help="Shared token required by the API; use --token auto to generate one"
        ),
    ] = None,
    token_file: Annotated[
        Path | None,
        typer.Option(
            "--token-file",
            help=(
                "Read the shared token from this file instead of --token, so it never "
                "appears in this process's command line or in the service journal. The "
                "file must be readable by its owner only (chmod 600) and must not be "
                "empty"
            ),
        ),
    ] = None,
    capture_enabled: Annotated[
        bool,
        typer.Option(
            "--capture/--no-capture",
            help="Allow the app to start real SDR captures",
        ),
    ] = True,
    solve_after_capture: Annotated[
        bool,
        typer.Option(
            "--solve-after-capture/--no-solve-after-capture",
            help=(
                "Re-solve every site after each capture so the map updates in the field. "
                "Turn it off for a long campaign and run `geo solve` at the end of the day"
            ),
        ),
    ] = True,
    keep_recordings: Annotated[
        int,
        typer.Option(
            "--keep-recordings",
            min=0,
            help=(
                "How many captured recordings to keep on disk. Each 5 MS/s 120 s stop is about "
                "2.24 GiB, so a campaign cannot keep them all; the survey has already extracted "
                "what geolocation needs. Default 1 (re-analysable), a transitional policy until "
                "event-triggered IQ snippets exist; 0 keeps none"
            ),
        ),
    ] = 1,
    solve_resolution: Annotated[
        float,
        typer.Option(
            "--solve-resolution-m",
            help=(
                "Grid resolution for the in-field solve. Coarser than `geo solve`'s default "
                "because a full-resolution pass over a few dozen sites takes minutes on a Pi"
            ),
        ),
    ] = 250.0,
    tls: Annotated[
        bool,
        typer.Option(
            "--tls/--no-tls",
            help=(
                "Serve over HTTPS with a self-signed certificate. Required for the phone's "
                "GPS: browsers refuse geolocation outside a secure context, so a live drive "
                "is impossible over plain HTTP"
            ),
        ),
    ] = False,
    tls_cert: Annotated[
        Path | None,
        typer.Option("--tls-cert", help="Use this certificate instead of a self-signed one"),
    ] = None,
    tls_key: Annotated[
        Path | None, typer.Option("--tls-key", help="Private key for --tls-cert")
    ] = None,
    tls_dir: Annotated[
        Path | None,
        typer.Option(
            "--tls-dir",
            help="Where the self-signed pair is kept; defaults to <output>/tls",
        ),
    ] = None,
    tls_host: Annotated[
        list[str] | None,
        typer.Option(
            "--tls-host",
            help=(
                "Extra name or IP the certificate must cover. Loopback and this machine's "
                "own addresses are always included; add one if you reach the Pi by another "
                "name"
            ),
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", help="Log every HTTP request")] = False,
) -> None:
    """Serve the field app.

    The API can start a real SDR capture, so the server binds to loopback
    unless `--host` says otherwise. On an open network, pass `--token auto`
    and use the printed URL.
    """
    # Everything below runs inside this scope so that the project binding,
    # once made, is dropped on every way out: a profile that will not
    # resolve, a token file with the wrong mode, TLS that cannot be set up,
    # an exception, or the server returning normally. A binding is
    # process-wide, and one left behind would silently judge whatever this
    # process did next against a project it is no longer serving.
    with ExitStack() as scope:
        # Before profile resolution, because the manifest may be what supplies
        # the band, the site, the output root and the database those steps and
        # everything after them consume. Manifests are read here; nothing is
        # opened and nothing is bound yet, so a wrong one costs nothing.
        project_context: ProjectContext | None = None
        if project is not None:
            try:
                project_context = _resolve_project_context(
                    ctx,
                    project=project,
                    campaign=campaign,
                    band=band,
                    site=site,
                    output=output,
                    database=database,
                )
            except (ProjectError, ProvenanceError, FileNotFoundError, OSError) as exc:
                console.print(f"[bold red]Project could not be resolved:[/bold red] {exc}")
                raise typer.Exit(code=1) from exc
            for notice in project_context.notices:
                console.print(f"[green]Project:[/green] {notice}")
            band = project_context.band
            site = project_context.site
            output = project_context.output
            database = project_context.database
            if not _flag_was_typed(ctx, "center_frequency"):
                center_frequency = project_context.capture.get(
                    "center_frequency_hz", center_frequency
                )
            if not _flag_was_typed(ctx, "sample_rate"):
                sample_rate = project_context.capture.get("sample_rate_hz", sample_rate)
            if not _flag_was_typed(ctx, "duration"):
                duration = project_context.capture.get("duration_seconds", duration)

            # Bind, then open once. From here on this process may open exactly
            # one path, and only if it already exists and already carries this
            # project's claim -- so a missing, empty, non-SQLite or foreign
            # database fails startup right here, with no file and no directory
            # left behind. The binding is not a startup check that is then
            # forgotten: it sits on the single `sqlite3.connect` in the
            # codebase, so every later open -- /api/state, a survey, a solve, a
            # live drive -- is checked too.
            scope.enter_context(
                project_binding(
                    project_id=project_context.manifest.project_id,
                    analyzer=project_context.manifest.analyzer,
                    database=project_context.database,
                )
            )
            try:
                connect_database(project_context.database).close()
            except (ProjectError, sqlite3.Error) as exc:
                console.print(f"[bold red]Project database refused:[/bold red] {exc}")
                console.print(
                    "Create one with `dmr-surveyor project init --create`, or adopt an "
                    "existing database with `dmr-surveyor project init --adopt --write`."
                )
                raise typer.Exit(code=1) from exc

        # Resolved BEFORE FieldSettings, and before anything is paid for: a typo
        # here must fail now, not after the operator has driven somewhere and
        # recorded a 90 s stop against a profile that doesn't exist. The
        # resolved site profile also seeds the gain defaults below -- editing
        # `config/sites/<name>.yaml` is the one thing this project's whole field
        # guide tells an operator to do, so the app has to actually read it.
        try:
            resolve_band_profile(band)
            resolved_site_profile = resolve_site_profile(site)
        except (ProfileError, FileNotFoundError, OSError) as exc:
            console.print(f"[bold red]Profile could not be resolved:[/bold red] {exc}")
            console.print(
                "Band profiles live in config/bands/, site profiles in config/sites/, "
                "resolved relative to the current directory."
            )
            raise typer.Exit(code=1) from exc

        resolved_gain, resolved_lna, gain_notices = _resolve_capture_gain(
            if_gain_reduction, lna_state, resolved_site_profile
        )
        for notice in gain_notices:
            console.print(f"[yellow]Gain default:[/yellow] {notice}")
        if not gain_notices:
            console.print(
                f"[green]Gain from site profile[/green] {site!r}: "
                f"IF gain reduction {resolved_gain:g} dB, LNA state {resolved_lna}"
            )

        try:
            resolved = resolve_token(token, token_file)
        except TokenError as exc:
            console.print(f"[bold red]Token could not be resolved:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc
        resolved_token = resolved.value
        # At startup, with a message the operator can read. `FieldService` checks
        # again, but by then the process is already serving, and a campaign id
        # that only fails on the first capture fails ninety seconds into a stop
        # somebody drove to.
        try:
            campaign = normalise_campaign_id(campaign)
        except ProvenanceError as exc:
            console.print(f"[bold red]{exc}[/bold red]")
            raise typer.Exit(code=1) from exc
        settings = FieldSettings(
            database_path=database or DEFAULT_DATABASE_PATH,
            recordings_dir=recordings or (output / "recordings"),
            output_root=output,
            band=band,
            campaign_id=campaign,
            project_id=project_context.manifest.project_id if project_context else None,
            project_root=project_context.manifest.root if project_context else None,
            site_profile=site,
            center_frequency_hz=center_frequency,
            sample_rate_hz=sample_rate,
            duration_seconds=duration,
            if_gain_reduction_db=resolved_gain,
            lna_state=resolved_lna,
            driver=driver,
            allow_capture=capture_enabled,
            keep_recordings=keep_recordings,
            solve_after_capture=solve_after_capture,
            solve_resolution_m=solve_resolution,
            tile_url=tile_url,
            tile_attribution=tile_attribution,
            map_center=(map_latitude, map_longitude),
            map_zoom=map_zoom,
            token=resolved_token,
        )

        space = disk_status(
            settings.recordings_dir,
            sample_rate_hz=sample_rate,
            duration_seconds=duration,
            keep_recordings=keep_recordings,
        )
        console.print(
            f"[bold]Storage[/bold] {space.free_bytes / GIB:.2f} GiB free, "
            f"{space.per_capture_bytes / GIB:.2f} GiB per stop, keeping {keep_recordings} recording(s)"
        )
        if not space.ready:
            console.print(f"[bold red]Not enough space:[/bold red] {space.reason}")
        elif keep_recordings and space.captures_that_fit < 3:
            console.print(
                "[yellow]Little headroom.[/yellow] Consider --keep-recordings 0 or a shorter --duration."
            )

        certificate: Certificate | None = None
        if tls_cert or tls_key:
            if not (tls_cert and tls_key):
                console.print("[bold red]--tls-cert and --tls-key must be given together.[/bold red]")
                raise typer.Exit(code=1)
            try:
                certificate = load_certificate(tls_cert, tls_key)
            except TlsUnavailable as exc:
                console.print(f"[bold red]TLS could not be configured:[/bold red] {exc}")
                raise typer.Exit(code=1) from exc
        elif tls:
            try:
                certificate = ensure_self_signed(
                    tls_dir or (output / "tls"), hosts=list(tls_host or [])
                )
            except TlsUnavailable as exc:
                console.print(f"[bold red]TLS could not be configured:[/bold red] {exc}")
                raise typer.Exit(code=1) from exc

        scheme = "https" if certificate is not None else "http"
        suffix = token_query_suffix(resolved)
        console.print("[bold]Field app[/bold]")
        for address in _local_addresses(port, scheme):
            console.print(f"  {address}/{suffix}")
        if certificate is not None:
            console.print(
                f"[bold]TLS[/bold] {'issued' if certificate.generated else 'reusing'} "
                f"{certificate.certificate_path}"
            )
            console.print(f"  valid for: {', '.join(certificate.hosts)}")
            console.print(f"  expires:   {certificate.not_after}")
            console.print(f"  SHA-256:   {certificate.fingerprint_sha256}")
            console.print(
                "[dim]The certificate is self-signed, so the phone shows a warning once per "
                "device: Advanced -> Proceed. After that the page is a secure context and the "
                "browser will share GPS.[/dim]"
            )
        if resolved.source == "file":
            console.print(
                f"[bold]Token[/bold] read from {token_file}; it is deliberately not printed "
                "here, so it stays out of the service journal."
            )
        if host == "127.0.0.1":
            console.print(
                "[yellow]Bound to loopback only.[/yellow] Re-run with --host 0.0.0.0 to open it "
                "from a phone on the same network."
            )
        if not resolved_token and host != "127.0.0.1":
            console.print(
                "[yellow]No token set.[/yellow] Anyone on this network can start a capture; "
                "pass --token auto on a shared network."
            )
        if certificate is None:
            console.print(
                "[dim]The browser only exposes GPS over HTTPS or from localhost. Over plain HTTP "
                "from a phone, tap the map to place your position -- and a live drive cannot run "
                "at all. Re-run with --tls to enable it.[/dim]"
            )
        console.print("Press Ctrl+C to stop.")
        serve_forever(settings, host=host, port=port, verbose=verbose, certificate=certificate)


__all__ = ["web_app"]
