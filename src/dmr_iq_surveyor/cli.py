from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from dmr_iq_surveyor.batch import BatchConfigError, run_batch_inspection
from dmr_iq_surveyor.inspection import run_inspection
from dmr_iq_surveyor.iq.metadata import WaveIQError
from dmr_iq_surveyor.iq.reader import UnsupportedSampleFormatError
from dmr_iq_surveyor.spectrum import SpectrumSettings, run_spectrum, run_spectrum_batch

app = typer.Typer(
    no_args_is_help=True,
    help="Offline survey tools for SDRconnect wideband IQ recordings.",
)
console = Console()


@app.callback()
def main() -> None:
    """DMR IQ Surveyor command group."""


@app.command()
def inspect(
    recording: Annotated[Path, typer.Argument(help="Path to the SDRconnect RIFF/RF64 IQ file")],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Directory for the inspection artifacts"),
    ] = Path("runs/inspect"),
    iq_order: Annotated[
        str,
        typer.Option(help="Assumed channel order: IQ or QI"),
    ] = "IQ",
    statistics_window_frames: Annotated[
        int,
        typer.Option(help="Frames sampled at beginning, middle, and end"),
    ] = 250_000,
    diagnostic_plot_frames: Annotated[
        int,
        typer.Option(help="Frames used in diagnostic plots"),
    ] = 20_000,
    skip_hash: Annotated[
        bool,
        typer.Option(help="Skip the full-file SHA-256 pass"),
    ] = False,
) -> None:
    """Inspect metadata and bounded sample windows without loading the full IQ file into RAM."""
    try:
        result = run_inspection(
            recording,
            output,
            assumed_iq_order=iq_order,
            statistics_window_frames=statistics_window_frames,
            diagnostic_plot_frames=diagnostic_plot_frames,
            compute_sha256=not skip_hash,
        )
    except (FileNotFoundError, WaveIQError, UnsupportedSampleFormatError, OSError) as exc:
        console.print(f"[bold red]Inspection failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    info = result["recording"]
    table = Table(title="IQ recording inspection")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Container", f"{info['container']}/{info['wave_format']}")
    table.add_row("Sample rate", f"{info['fmt']['sample_rate_hz']:,} samples/s")
    table.add_row("Center frequency", str(info["center_frequency_hz"] or "unknown"))
    table.add_row("Center source", info.get("center_frequency_source", "unknown"))
    table.add_row("Duration", f"{info['duration_seconds']:.6f} s")
    table.add_row("Frames", f"{info['frame_count']:,}")
    table.add_row("Encoding", info["sample_encoding"])
    table.add_row("IQ order", f"{info['iq_order']} (assumed)")
    table.add_row("Warnings", str(len(info["warnings"])))
    console.print(table)
    console.print(f"[green]Artifacts written to:[/green] {Path(output).resolve()}")
    console.print(f"Open: {Path(output).resolve() / 'report.md'}")


@app.command("inspect-batch")
def inspect_batch(
    config: Annotated[
        Path,
        typer.Argument(help="YAML file listing one or more SDRconnect IQ recordings"),
    ],
) -> None:
    """Inspect several recordings and create a shared consistency summary."""
    try:
        result = run_batch_inspection(config)
    except (FileNotFoundError, BatchConfigError, OSError, ValueError) as exc:
        console.print(f"[bold red]Batch inspection failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title="Batch IQ inspection")
    table.add_column("Recording")
    table.add_column("Status")
    table.add_column("Center Hz")
    table.add_column("Source")
    table.add_column("Sample rate")
    table.add_column("Duration")
    for row in result["rows"]:
        duration_value = row.get("duration_seconds")
        table.add_row(
            str(row["recording_id"]),
            str(row["status"]),
            str(row.get("center_frequency_hz") or "-"),
            str(row.get("center_frequency_source") or "-"),
            str(row.get("sample_rate_hz") or "-"),
            f"{float(duration_value):.6f} s" if duration_value not in {"", None} else "-",
        )
    console.print(table)
    consistency = result["consistency"]
    console.print(
        "Consistency: "
        f"center={consistency['same_center_frequency']}, "
        f"sample_rate={consistency['same_sample_rate']}, "
        f"encoding={consistency['same_encoding']}"
    )
    output_root = Path(result["output_root"])
    console.print(f"[green]Batch artifacts written to:[/green] {output_root}")
    console.print(f"Open: {output_root / 'batch_report.md'}")


@app.command()
def spectrum(
    recording: Annotated[Path, typer.Argument(help="Path to the SDRconnect IQ file")],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Directory for spectrum artifacts"),
    ] = Path("runs/spectrum"),
    iq_order: Annotated[str, typer.Option(help="Assumed channel order: IQ or QI")] = "IQ",
    fft_size: Annotated[int, typer.Option(help="FFT size (power of two)")] = 65_536,
    overlap_ratio: Annotated[float, typer.Option(help="FFT overlap ratio")] = 0.5,
    waterfall_time_bins: Annotated[
        int, typer.Option(help="Number of time rows in the saved waterfall")
    ] = 500,
    waterfall_frequency_bins: Annotated[
        int, typer.Option(help="Number of frequency columns in the saved waterfall")
    ] = 8_192,
    occupancy_threshold_db: Annotated[
        float, typer.Option(help="Occupancy threshold above local noise floor")
    ] = 8.0,
) -> None:
    """Generate spectrum, noise-floor, occupancy, and waterfall artifacts."""
    settings = SpectrumSettings(
        fft_size=fft_size,
        overlap_ratio=overlap_ratio,
        waterfall_time_bins=waterfall_time_bins,
        waterfall_frequency_bins=waterfall_frequency_bins,
        occupancy_threshold_db=occupancy_threshold_db,
    )
    try:
        result = run_spectrum(
            recording,
            output,
            settings=settings,
            assumed_iq_order=iq_order,
        )
    except (
        FileNotFoundError,
        WaveIQError,
        UnsupportedSampleFormatError,
        OSError,
        ValueError,
    ) as exc:
        console.print(f"[bold red]Spectrum analysis failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    summary = result["summary"]
    metrics = summary["metrics"]
    table = Table(title="IQ spectrum analysis")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("FFT count", f"{metrics['fft_count']:,}")
    table.add_row("FFT size", f"{metrics['fft_size']:,}")
    table.add_row("Resolution", f"{metrics['frequency_resolution_hz']:.3f} Hz")
    table.add_row("Elapsed", f"{summary['elapsed_seconds']:.3f} s")
    table.add_row("Peak RSS", f"{summary['peak_rss_bytes'] / (1024 ** 2):.1f} MiB")
    console.print(table)
    console.print(f"[green]Spectrum artifacts written to:[/green] {Path(output).resolve()}")
    console.print(f"Open: {Path(output).resolve() / 'report.md'}")


@app.command("spectrum-batch")
def spectrum_batch(
    config: Annotated[
        Path,
        typer.Argument(help="YAML file listing SDRconnect IQ recordings"),
    ],
) -> None:
    """Run spectrum analysis independently for every recording in a batch config."""
    try:
        result = run_spectrum_batch(config)
    except (FileNotFoundError, BatchConfigError, OSError, ValueError) as exc:
        console.print(f"[bold red]Batch spectrum analysis failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"Spectrum batch complete: {result['successful_recordings']} succeeded, "
        f"{result['failed_recordings']} failed"
    )
    output_root = Path(result["output_root"])
    console.print(f"[green]Batch artifacts written to:[/green] {output_root}")
    console.print(f"Open: {output_root / 'spectrum_batch_report.md'}")


if __name__ == "__main__":
    app()
