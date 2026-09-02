"""`dmr-surveyor web` -- serve the field control app from the Raspberry Pi.

Mounted additively; no existing command changes.
"""

from __future__ import annotations

import secrets
import socket
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dmr_iq_surveyor.survey.pipeline import DEFAULT_DATABASE_PATH
from dmr_iq_surveyor.web.server import serve_forever
from dmr_iq_surveyor.web.service import FieldSettings

web_app = typer.Typer(
    no_args_is_help=True,
    help="Field web app: mark a position, record, watch progress, view geolocation results.",
)
console = Console()


def _local_addresses(port: int) -> list[str]:
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
        addresses.append(f"http://{probe.getsockname()[0]}:{port}")
    except OSError:
        pass
    finally:
        probe.close()
    addresses.append(f"http://localhost:{port}")
    return addresses


@web_app.command("serve")
def web_serve(
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
    band: Annotated[str, typer.Option(help="Band profile name or path")] = "central_800",
    site: Annotated[
        str, typer.Option(help="Site profile name or path, providing the fixed equipment context")
    ] = "home",
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
    duration: Annotated[float, typer.Option(help="Default capture duration in seconds")] = 120.0,
    if_gain_reduction: Annotated[
        float,
        typer.Option(
            "--if-gain-reduction",
            help="Default SDRplay IF gain reduction in dB; keep it identical at every stop",
        ),
    ] = 40.0,
    lna_state: Annotated[int, typer.Option("--lna-state", help="Default SDRplay LNA state")] = 2,
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
    verbose: Annotated[bool, typer.Option("--verbose", help="Log every HTTP request")] = False,
) -> None:
    """Serve the field app.

    The API can start a real SDR capture, so the server binds to loopback
    unless `--host` says otherwise. On an open network, pass `--token auto`
    and use the printed URL.
    """
    resolved_token = secrets.token_urlsafe(12) if token == "auto" else token
    settings = FieldSettings(
        database_path=database or DEFAULT_DATABASE_PATH,
        recordings_dir=recordings or (output / "recordings"),
        output_root=output,
        band=band,
        site_profile=site,
        center_frequency_hz=center_frequency,
        sample_rate_hz=sample_rate,
        duration_seconds=duration,
        if_gain_reduction_db=if_gain_reduction,
        lna_state=lna_state,
        driver=driver,
        allow_capture=capture_enabled,
        solve_after_capture=solve_after_capture,
        solve_resolution_m=solve_resolution,
        tile_url=tile_url,
        tile_attribution=tile_attribution,
        map_center=(map_latitude, map_longitude),
        map_zoom=map_zoom,
        token=resolved_token,
    )

    suffix = f"?token={resolved_token}" if resolved_token else ""
    console.print("[bold]Field app[/bold]")
    for address in _local_addresses(port):
        console.print(f"  {address}/{suffix}")
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
    console.print(
        "[dim]The browser only exposes GPS over HTTPS or from localhost. Over plain HTTP from "
        "a phone, tap the map to place your position.[/dim]"
    )
    console.print("Press Ctrl+C to stop.")
    serve_forever(settings, host=host, port=port, verbose=verbose)


__all__ = ["web_app"]
