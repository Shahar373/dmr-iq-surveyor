from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from dmr_iq_surveyor.cli import app, console
from dmr_iq_surveyor.decode import (
    DecoderSettings,
    ExtractionSettings,
    run_channel_extraction,
    run_decode_batch,
    run_decoder_profiles,
)


@app.command("extract-channel")
def extract_channel(
    recording: Annotated[
        Path,
        typer.Argument(help="Path to the wideband SDRconnect IQ file"),
    ],
    frequency_hz: Annotated[
        float,
        typer.Option(
            "--frequency",
            help="RF channel center frequency in Hz",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Directory for discriminator WAV artifacts",
        ),
    ] = Path("runs/channel"),
    iq_order: Annotated[
        str,
        typer.Option(help="Assumed source channel order: IQ or QI"),
    ] = "IQ",
    chunk_frames: Annotated[
        int,
        typer.Option(help="Wideband complex samples processed per chunk"),
    ] = 1_000_000,
    channel_lowpass_hz: Annotated[
        float,
        typer.Option(help="Final complex-baseband low-pass cutoff"),
    ] = 7_500.0,
) -> None:
    """Extract one narrowband channel as 48 kHz discriminator audio."""
    settings = ExtractionSettings(
        chunk_frames=chunk_frames,
        channel_lowpass_hz=channel_lowpass_hz,
    )
    try:
        result = run_channel_extraction(
            recording,
            output,
            candidate_frequency_hz=frequency_hz,
            settings=settings,
            assumed_iq_order=iq_order,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        console.print(
            f"[bold red]Channel extraction failed:[/bold red] {exc}"
        )
        raise typer.Exit(code=1) from exc

    table = Table(title="Narrowband channel extraction")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row(
        "Frequency",
        f"{result['candidate_frequency_hz'] / 1e6:.6f} MHz",
    )
    table.add_row(
        "Mixer offset",
        f"{result['frequency_offset_hz']:,.3f} Hz",
    )
    table.add_row("IQ order", str(result["iq_order"]))
    table.add_row(
        "Output",
        (
            f"{result['output_sample_rate_hz']:,} Hz mono PCM16, "
            f"{result['output_duration_seconds']:.6f} s"
        ),
    )
    table.add_row(
        "Clipped samples",
        str(result["normalization"]["clipped_samples"]),
    )
    table.add_row(
        "Elapsed",
        f"{result['elapsed_seconds']:.3f} s",
    )
    console.print(table)
    console.print(
        f"[green]Extraction artifacts written to:[/green] "
        f"{result['output_dir']}"
    )


@app.command("decode-channel")
def decode_channel(
    discriminator_wav: Annotated[
        Path,
        typer.Argument(help="48 kHz mono discriminator WAV"),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Directory for DSD-FME logs and reports",
        ),
    ] = Path("runs/decode"),
    binary: Annotated[
        str,
        typer.Option(help="DSD-FME binary name or path"),
    ] = "dsd-fme",
    timeout_seconds: Annotated[
        float,
        typer.Option(help="Maximum duration of each decoder attempt"),
    ] = 120.0,
    try_inverted: Annotated[
        bool,
        typer.Option(
            "--try-inverted/--normal-only",
            help="Also retry DMR with DSD-FME -xr inversion",
        ),
    ] = True,
) -> None:
    """Run DSD-FME against an extracted discriminator WAV."""
    inversions = ["normal", "inverted"] if try_inverted else ["normal"]
    settings = DecoderSettings(
        binary=binary,
        timeout_seconds=timeout_seconds,
        inversions=inversions,
    )
    try:
        result = run_decoder_profiles(
            discriminator_wav,
            output,
            settings=settings,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        console.print(
            f"[bold red]Decoder execution failed:[/bold red] {exc}"
        )
        raise typer.Exit(code=1) from exc

    table = Table(title="DSD-FME result")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Status", str(result["status"]))
    table.add_row("Best inversion", str(result["best_inversion"]))
    table.add_row(
        "Decoder available",
        str(result["probe"]["available"]),
    )
    best = next(
        attempt
        for attempt in result["attempts"]
        if attempt["inversion"] == result["best_inversion"]
    )
    evidence = best["evidence"]
    table.add_row("DMR syncs", str(evidence["dmr_sync_count"]))
    table.add_row("Color Codes", str(evidence["color_codes"]))
    table.add_row("Talkgroups", str(evidence["talkgroup_ids"]))
    table.add_row("Radio IDs", str(evidence["radio_ids"]))
    console.print(table)
    console.print(
        f"[green]Decoder artifacts written to:[/green] "
        f"{Path(output).resolve()}"
    )


@app.command("decode-batch")
def decode_batch(
    config: Annotated[
        Path,
        typer.Argument(
            help="YAML configuration and Phase 3 candidate run"
        ),
    ],
) -> None:
    """Extract and decode the highest-ranked Phase 3 candidates."""
    try:
        result = run_decode_batch(config)
    except (FileNotFoundError, OSError, ValueError) as exc:
        console.print(
            f"[bold red]Phase 4 batch failed:[/bold red] {exc}"
        )
        raise typer.Exit(code=1) from exc

    console.print(
        f"Phase 4 complete: {result['attempt_count']} attempts, "
        f"{result['confirmed_dmr_attempts']} with explicit DMR sync"
    )
    console.print(
        f"[green]Phase 4 artifacts written to:[/green] "
        f"{result['output_dir']}"
    )
    console.print(
        f"Open: {Path(result['output_dir']) / 'decode_batch_report.md'}"
    )


__all__ = ["app"]
