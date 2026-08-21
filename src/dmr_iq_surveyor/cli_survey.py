from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from dmr_iq_surveyor.capture.core import CaptureSettings, run_capture_and_survey
from dmr_iq_surveyor.capture.device import probe_soapysdr
from dmr_iq_surveyor.capture.gps import resolve_gps
from dmr_iq_surveyor.capture.preflight import run_preflight
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
    site_id: Annotated[
        str | None,
        typer.Option(
            "--site-id",
            help=(
                "Override the profile's site_id for this run. Use one profile "
                "(same equipment) across a mobile session and give each stop its own id"
            ),
        ),
    ] = None,
    gps_url: Annotated[
        str | None,
        typer.Option(
            "--gps-url",
            help=(
                "HTTP URL of a phone-hosted GPS JSON endpoint, fetched once now. "
                "Records where the recording is being ANALYZED from, so only use it "
                "when analyzing at the capture site"
            ),
        ),
    ] = None,
    gps_timeout: Annotated[
        float,
        typer.Option("--gps-timeout", help="Seconds to wait for the GPS server before giving up"),
    ] = 10.0,
    latitude: Annotated[
        float | None,
        typer.Option("--latitude", help="Manual latitude for this run; takes precedence over --gps-url"),
    ] = None,
    longitude: Annotated[
        float | None,
        typer.Option("--longitude", help="Manual longitude for this run; takes precedence over --gps-url"),
    ] = None,
) -> None:
    """Discover RF observations in a wideband recording and store them."""
    if (latitude is None) != (longitude is None):
        console.print("[bold red]--latitude and --longitude must be given together[/bold red]")
        raise typer.Exit(code=1)

    gps = resolve_gps(
        gps_url=gps_url,
        gps_timeout_seconds=gps_timeout,
        latitude=latitude,
        longitude=longitude,
    )
    if gps["source"] == "fetch_failed":
        console.print(f"[yellow]GPS fetch failed:[/yellow] {gps['error']} -- continuing without it")

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
            gps_latitude=gps["latitude"],
            gps_longitude=gps["longitude"],
            gps_altitude_m=gps["altitude_m"],
            gps_accuracy_m=gps["accuracy_m"],
            gps_source=gps["source"],
            gps_fetched_at_utc=gps["fetched_at_utc"],
            site_id_override=site_id,
        )
    except (FileNotFoundError, OSError, ValueError, ProfileError, sqlite3.Error) as exc:
        console.print(f"[bold red]Survey run failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title="RF survey run")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", str(result["run_id"]))
    table.add_row("Observations", str(result["observation_count"]))
    if gps["latitude"] is not None:
        table.add_row("GPS", f"{gps['latitude']:.6f}, {gps['longitude']:.6f} (source: {gps['source']})")
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


@survey_app.command("preflight")
def survey_preflight(
    output: Annotated[
        Path,
        typer.Argument(help="Directory the capture would write into (checked for space and speed)"),
    ],
    band: Annotated[
        str,
        typer.Option(help="Band profile name (config/bands/<name>.yaml) or a path to one"),
    ],
    center_frequency: Annotated[
        float,
        typer.Option("--center-frequency", help="Tuner center frequency in Hz"),
    ] = 868_000_000.0,
    sample_rate: Annotated[
        float,
        typer.Option("--sample-rate", help="IQ sample rate in samples/s"),
    ] = 2_000_000.0,
    duration: Annotated[
        float,
        typer.Option(help="Capture duration in seconds"),
    ] = 90.0,
    driver: Annotated[
        str,
        typer.Option(help="SoapySDR driver name"),
    ] = "sdrplay",
    gps_url: Annotated[
        str | None,
        typer.Option("--gps-url", help="GPS endpoint to test, same value you would pass to capture"),
    ] = None,
    gps_timeout: Annotated[
        float,
        typer.Option("--gps-timeout", help="Seconds to wait for the GPS server"),
    ] = 10.0,
    probe_megabytes: Annotated[
        int,
        typer.Option("--probe-mb", help="Size of the write-throughput probe file in MiB"),
    ] = 128,
    skip_throughput: Annotated[
        bool,
        typer.Option("--skip-throughput", help="Skip the write-speed probe (faster, less certain)"),
    ] = False,
) -> None:
    """Check that a capture would actually succeed, before making it.

    Verifies the SDR is reachable, the output filesystem is writable, has
    room, and is fast enough for the requested sample rate, that the tuning
    covers the band profile, and that GPS (if configured) responds.
    """
    try:
        band_profile = resolve_band_profile(band)
    except ProfileError as exc:
        console.print(f"[bold red]Invalid band profile:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    result = run_preflight(
        output,
        band=band_profile,
        center_frequency_hz=center_frequency,
        sample_rate_hz=sample_rate,
        duration_seconds=duration,
        driver=driver,
        gps_url=gps_url,
        gps_timeout_seconds=gps_timeout,
        probe_megabytes=probe_megabytes,
        skip_throughput=skip_throughput,
    )

    styles = {"pass": "[green]PASS[/green]", "warn": "[yellow]WARN[/yellow]", "fail": "[bold red]FAIL[/bold red]"}
    table = Table(title="Capture preflight")
    table.add_column("Check", style="bold")
    table.add_column("Result")
    table.add_column("Detail")
    for check in result["checks"]:
        table.add_row(check["name"], styles[check["status"]], check["detail"])
    console.print(table)

    size_gib = result["capture_size_bytes"] / 1024**3
    console.print(
        f"Planned capture: {duration:.0f} s at {sample_rate / 1e6:.3f} MS/s "
        f"= {size_gib:.2f} GiB, needs {result['required_bytes_per_second'] / 1e6:.1f} MB/s sustained"
    )

    if result["verdict"] == "fail":
        console.print("[bold red]NO-GO[/bold red] -- fix the FAIL rows above before capturing.")
        raise typer.Exit(code=1)
    if result["verdict"] == "warn":
        console.print("[yellow]GO, with caveats[/yellow] -- read the WARN rows above.")
        return
    console.print("[bold green]GO[/bold green] -- all checks passed.")


@survey_app.command("capture")
def survey_capture(
    output: Annotated[
        Path,
        typer.Argument(help="Directory to write the captured WAV recording into"),
    ],
    band: Annotated[
        str,
        typer.Option(help="Band profile name (config/bands/<name>.yaml) or a path to one"),
    ],
    site: Annotated[
        str,
        typer.Option(help="Site profile name (config/sites/<name>.yaml) or a path to one"),
    ],
    center_frequency: Annotated[
        float,
        typer.Option("--center-frequency", help="Tuner center frequency in Hz"),
    ],
    sample_rate: Annotated[
        float,
        typer.Option("--sample-rate", help="IQ sample rate in samples/s"),
    ],
    duration: Annotated[
        float,
        typer.Option(help="Capture duration in seconds"),
    ] = 90.0,
    if_gr: Annotated[
        float | None,
        typer.Option(
            "--if-gr",
            help=(
                "IF gain REDUCTION in dB (SDRplay units, typically 20-59; higher = less "
                "sensitive). Required unless --agc"
            ),
        ),
    ] = None,
    lna_state: Annotated[
        int | None,
        typer.Option(
            "--lna-state",
            help="LNA state index (0-9 on an RSP1B; higher = less sensitive). Applies with or without AGC",
        ),
    ] = None,
    bandwidth: Annotated[
        float | None,
        typer.Option(
            "--bandwidth",
            help="IF filter bandwidth in Hz. Defaults to the driver's choice for the sample rate",
        ),
    ] = None,
    agc: Annotated[
        bool,
        typer.Option("--agc/--no-agc", help="Enable SDR AGC (mutually exclusive with --if-gr)"),
    ] = False,
    antenna: Annotated[
        str | None,
        typer.Option(help="SoapySDR antenna name, only needed if the device exposes more than one"),
    ] = None,
    driver: Annotated[
        str,
        typer.Option(help="SoapySDR driver name"),
    ] = "sdrplay",
    serial: Annotated[
        str | None,
        typer.Option("--serial", help="Pin to one device by serial number, if more than one is attached"),
    ] = None,
    survey_output: Annotated[
        Path | None,
        typer.Option(help="Survey run output directory; defaults to <output>/survey"),
    ] = None,
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
            help="Compute a SHA-256 of the captured recording (slow on large files)",
        ),
    ] = False,
    write_auxi: Annotated[
        bool,
        typer.Option(
            "--write-auxi/--no-write-auxi",
            help="Write an SDRplay-style auxi metadata chunk (else rely on the SDRconnect filename fallback)",
        ),
    ] = True,
    site_id: Annotated[
        str | None,
        typer.Option(
            "--site-id",
            help=(
                "Override the profile's site_id for this run. Use one profile "
                "(same equipment) across a mobile session and give each stop its own id"
            ),
        ),
    ] = None,
    gps_url: Annotated[
        str | None,
        typer.Option(
            "--gps-url",
            help=(
                "HTTP URL of a phone-hosted GPS JSON endpoint (e.g. "
                "scripts/phone_gps_server.py on Termux), fetched once at capture start"
            ),
        ),
    ] = None,
    gps_timeout: Annotated[
        float,
        typer.Option("--gps-timeout", help="Seconds to wait for the GPS server before giving up"),
    ] = 10.0,
    latitude: Annotated[
        float | None,
        typer.Option("--latitude", help="Manual latitude override; takes precedence over --gps-url"),
    ] = None,
    longitude: Annotated[
        float | None,
        typer.Option("--longitude", help="Manual longitude override; takes precedence over --gps-url"),
    ] = None,
) -> None:
    """Capture live IQ from an SDRplay device via SoapySDR and immediately
    survey it -- one command instead of a separate recorder plus `survey run`.

    Authorized as a one-off, explicit exception to this project's "no
    premature live acquisition" principle (see CLAUDE.md) for field-capture
    friction with existing tools; it changes nothing else about the pipeline.
    """
    probe = probe_soapysdr(driver)
    if not probe.available:
        console.print(f"[bold red]SoapySDR device unavailable:[/bold red] {probe.probe_error}")
        raise typer.Exit(code=1)

    if (latitude is None) != (longitude is None):
        console.print("[bold red]--latitude and --longitude must be given together[/bold red]")
        raise typer.Exit(code=1)

    try:
        capture_settings = CaptureSettings(
            center_frequency_hz=center_frequency,
            sample_rate_hz=sample_rate,
            duration_seconds=duration,
            if_gain_reduction_db=if_gr,
            lna_state=lna_state,
            agc=agc,
            antenna=antenna,
            driver=driver,
            write_auxi=write_auxi,
            bandwidth_hz=bandwidth,
            serial=serial,
        )
    except ValueError as exc:
        console.print(f"[bold red]Invalid capture settings:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    resolved_survey_output = survey_output or (output / "survey")
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold]Capturing[/bold]"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task_id = progress.add_task("capture", total=1000)

            def report(frames: int, target: int, _elapsed: float) -> None:
                progress.update(task_id, completed=min(1000, round(1000 * frames / max(target, 1))))

            result = run_capture_and_survey(
                output,
                resolved_survey_output,
                capture=capture_settings,
                band=band,
                site=site,
                run_id=run_id,
                database_path=database,
                assumed_iq_order=iq_order,
                compute_source_hash=hash_source,
                gps_url=gps_url,
                gps_timeout_seconds=gps_timeout,
                gps_latitude=latitude,
                gps_longitude=longitude,
                on_progress=report,
                site_id_override=site_id,
            )
    except (FileNotFoundError, OSError, ValueError, ProfileError, RuntimeError, sqlite3.Error) as exc:
        console.print(f"[bold red]Capture/survey failed:[/bold red] {exc}")
        # A capture that dies mid-stream still leaves a valid, closed WAV
        # behind. Say so: without this the operator has no way to know a
        # salvageable recording is sitting on disk, and a usable field
        # capture gets thrown away as a failed run.
        salvage = sorted(
            output.glob("SDRconnect_IQ_*.wav"), key=lambda path: path.stat().st_mtime, reverse=True
        )
        if salvage:
            console.print(
                f"[yellow]A partial recording was still written:[/yellow] {salvage[0]}\n"
                "It is a valid WAV. Analyze it with:\n"
                f"  dmr-surveyor survey run {salvage[0]} --band {band} --site {site}"
            )
        raise typer.Exit(code=1) from exc

    table = Table(title="Live capture + RF survey")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Recording", str(result["capture"]["wav_path"]))
    table.add_row(
        "Actual duration",
        f"{result['capture']['actual_duration_seconds']:.3f} s "
        f"(requested {result['capture']['requested_duration_seconds']:.3f} s)",
    )
    overflows = result["capture"].get("overflow_count", 0)
    if overflows:
        table.add_row(
            "[yellow]Dropped buffers[/yellow]",
            f"[yellow]{overflows}[/yellow] -- storage or CPU could not keep up; the recording "
            "has gaps. Lower --sample-rate or use faster storage.",
        )
    applied = result["capture"].get("device_settings_applied") or {}
    if applied.get("gains"):
        table.add_row(
            "Gain applied",
            ", ".join(f"{name}={value:g}" for name, value in sorted(applied["gains"].items())),
        )
    table.add_row("Survey run ID", str(result["survey"]["run_id"]))
    table.add_row("Observations", str(result["survey"]["observation_count"]))
    gps = result["gps"]
    if gps["latitude"] is not None:
        table.add_row("GPS", f"{gps['latitude']:.6f}, {gps['longitude']:.6f} (source: {gps['source']})")
    elif gps["source"] == "fetch_failed":
        table.add_row("GPS", f"[yellow]fetch failed:[/yellow] {gps['error']}")
    else:
        table.add_row("GPS", "not configured")
    console.print(table)
    console.print(f"[green]Recording written to:[/green] {result['capture']['wav_path']}")
    console.print(f"[green]Survey artifacts written to:[/green] {result['survey']['output_dir']}")
    console.print(f"Open: {Path(result['survey']['output_dir']) / 'reports' / 'report.md'}")


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
    except (FileNotFoundError, OSError, ValueError, ProfileError, sqlite3.Error) as exc:
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
