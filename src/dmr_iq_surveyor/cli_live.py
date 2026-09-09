"""`dmr-surveyor live` -- measure without recording anything.

A stationary stop today writes a WAV: 90 s at 5 MS/s is 1.68 GiB, and the
Pi in the field does not have many of those. This command runs the same
detector over the same samples as they stream past and keeps only the
result -- about 8 KiB, measured. What lands in the database is an ordinary
`survey_runs` row with its observations, so `geo measurements`, `geo solve`,
the stop list and the planner consume it without knowing how it was made.

What is lost is the recording itself: a live measurement cannot be
re-analysed later with different settings, because the samples are gone.
That is the trade, and it is the right one when the alternative is not
measuring at all for want of a card.

Mounted additively; no existing command changes.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.live.session import LiveSession, LiveSettings, Position
from dmr_iq_surveyor.survey.pipeline import DEFAULT_DATABASE_PATH
from dmr_iq_surveyor.survey.profiles import (
    ProfileError,
    resolve_band_profile,
    resolve_site_profile,
)

live_app = typer.Typer(
    no_args_is_help=True,
    help="Live (streaming) survey: measure now, keep the result, keep no IQ.",
)
console = Console()

# One window's spectrum products, in bytes per FFT bin, measured rather than
# assumed from the field list: one float64 frequency axis (8), four float32
# arrays -- average, percentile, noise, occupancy (16) -- and two boolean
# masks (2). Used to state the memory a requested duration will actually
# hold, rather than discovering it as an OOM on the Pi.
_BYTES_PER_BIN = 8 + 4 * 4 + 2 * 1
# Above this, a stationary measurement is refused rather than attempted. It
# is set at roughly what an offline survey run already holds for its 40
# segments at a 65536-point FFT (about 68 MB), so this command is never the
# reason a Pi runs out of memory.
_MEMORY_LIMIT_MB = 128.0


@live_app.command("stop")
def live_stop(
    latitude: Annotated[float, typer.Option("--latitude", help="Where the receiver is")],
    longitude: Annotated[float, typer.Option("--longitude", help="Where the receiver is")],
    seconds: Annotated[
        float,
        typer.Option(
            "--seconds",
            help=(
                "How long to measure. Each second is one window, and the windows are "
                "averaged into a single measurement -- so this is the averaging length, "
                "not a recording length"
            ),
        ),
    ] = 30.0,
    band: Annotated[str, typer.Option(help="Band profile name or path")] = "central_800_narrow",
    site: Annotated[str, typer.Option(help="Site profile name or path")] = "home",
    database: Annotated[
        Path | None, typer.Option("--database", help="Persistent inventory SQLite path")
    ] = None,
    center_frequency: Annotated[
        float, typer.Option("--center-frequency", help="Tuner centre frequency in Hz")
    ] = 867_406_250.0,
    sample_rate: Annotated[
        float, typer.Option("--sample-rate", help="Sample rate in samples/s")
    ] = 5_000_000.0,
    if_gain_reduction: Annotated[
        float,
        typer.Option(
            "--if-gain-reduction",
            help="SDRplay IF gain reduction in dB; keep it identical across the campaign",
        ),
    ] = 26.0,
    lna_state: Annotated[int, typer.Option("--lna-state", help="SDRplay LNA state")] = 8,
    driver: Annotated[str, typer.Option(help="SoapySDR driver name")] = "sdrplay",
    window_seconds: Annotated[
        float, typer.Option("--window-seconds", help="Length of one averaging window")
    ] = 1.0,
    fft_size: Annotated[
        int,
        typer.Option(
            "--fft-size",
            help=(
                "FFT length per frame. 16384 is 305 Hz at 5 MS/s; measured against 65536 the "
                "reported SNR agrees within 0.2 dB, at a quarter of the memory"
            ),
        ),
    ] = 16_384,
    label: Annotated[str, typer.Option("--label", help="Name for this stop")] = "",
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            help=(
                "Give up after this many seconds if the stream is not delivering. Defaults "
                "to four times --seconds plus 30, which allows for a slow SDR to open and "
                "for driver overflows, each of which costs seconds rather than milliseconds"
            ),
        ),
    ] = None,
) -> None:
    """Measure at one place, write the result, keep no recording."""
    if seconds <= 0 or window_seconds <= 0:
        console.print("[bold red]--seconds and --window-seconds must be positive.[/bold red]")
        raise typer.Exit(code=1)
    windows = max(1, round(seconds / window_seconds))
    held_mb = windows * fft_size * _BYTES_PER_BIN / 1_048_576
    if held_mb > _MEMORY_LIMIT_MB:
        console.print(
            f"[bold red]That would hold {held_mb:.0f} MB of spectra in RAM[/bold red] "
            f"({windows} windows at a {fft_size}-point FFT), over the {_MEMORY_LIMIT_MB:.0f} MB "
            "this command will use on a Pi."
        )
        console.print(
            f"Measure for less time (--seconds {(_MEMORY_LIMIT_MB / held_mb) * seconds:.0f}) "
            f"or use a coarser --fft-size ({fft_size // 4} holds a quarter as much)."
        )
        raise typer.Exit(code=1)

    try:
        band_profile = resolve_band_profile(band)
        site_profile = resolve_site_profile(site)
    except (ProfileError, FileNotFoundError, OSError) as exc:
        console.print(f"[bold red]Profile could not be resolved:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    settings = LiveSettings(
        band=band,
        site_id=site_profile.site_id,
        center_frequency_hz=center_frequency,
        sample_rate_hz=sample_rate,
        if_gain_reduction_db=if_gain_reduction,
        lna_state=lna_state,
        driver=driver,
        window_seconds=window_seconds,
        fft_size=fft_size,
        # One place, one bin: the grid is anchored here so every window of
        # this measurement lands in the same square whatever the bin size.
        grid_anchor_latitude=latitude,
        grid_anchor_longitude=longitude,
        min_windows_per_bin=min(3, windows),
        max_windows_per_bin=windows,
        # Standing still, so nothing is travelling and no window can be
        # dropped for covering too much ground.
        max_window_travel_m=1_000_000.0,
    )
    settings.validate()

    fix = Position(latitude=latitude, longitude=longitude, at=time.monotonic())

    def position() -> Position:
        # Re-stamped each call: the fix is not getting older, the receiver is
        # standing on it. A stationary measurement must not be dropped by the
        # staleness guard that exists to catch a phone that stopped reporting.
        fix.at = time.monotonic()
        return fix

    console.print(
        f"[bold]Live stop[/bold] {windows} window(s) of {window_seconds:g} s at "
        f"{center_frequency / 1e6:.4f} MHz, {sample_rate / 1e6:.2f} MS/s "
        f"(holding {held_mb:.0f} MB, writing no IQ)"
    )

    # The run id is the place AND the session. A fixed session id made a
    # second stop at the same coordinates overwrite the first in silence;
    # timestamped, the earlier one stays, superseded, and the digest can say
    # whether the two agreed.
    session = LiveSession(
        session_id=label or f"live_stop_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}",
        settings=settings,
        band=band_profile,
        site=site_profile,
        database_path=database or DEFAULT_DATABASE_PATH,
        position_provider=position,
    )
    started = time.monotonic()

    deadline = seconds * 4 + 30.0 if timeout is None else float(timeout)

    def stop() -> bool:
        # The window cap ends the measurement; this is the wall-clock
        # backstop for a stream that stalls and never delivers them.
        return session.stats.bins_written > 0 or time.monotonic() - started > deadline

    connection = connect_geo_database(Path(database or DEFAULT_DATABASE_PATH))
    try:
        stats = session.run(stop=stop, connection=connection)
    finally:
        connection.close()

    table = Table(title="Live stop", show_header=False)
    table.add_row("windows measured", str(stats.windows_recorded))
    table.add_row("measurement written", "yes" if stats.bins_written else "no")
    table.add_row("observations", str(stats.observations_written))
    table.add_row("driver overflows", str(stats.overflow_count))
    if stats.windows_without_position:
        table.add_row("windows with no fix", str(stats.windows_without_position))
    if stats.bins_too_short:
        table.add_row("discarded, too few windows", str(stats.bins_too_short))
    console.print(table)
    if not stats.bins_written:
        console.print(
            "[yellow]Nothing was written.[/yellow] The stream delivered fewer than "
            f"{settings.min_windows_per_bin} usable window(s); check the SDR is not held by "
            "another process."
        )
        raise typer.Exit(code=1)
    console.print(
        "[green]Written.[/green] Run `geo measurements` and `geo solve` to fold it in, "
        "or let the field app do it."
    )


__all__ = ["live_app"]
