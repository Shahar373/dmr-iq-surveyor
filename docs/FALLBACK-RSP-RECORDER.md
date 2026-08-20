# Fallback recording path: rsp-recorder + `survey run`

`dmr-surveyor survey capture` is the intended one-command path. This document records the
independent fallback, so a field session is never blocked by a problem in the capture command — and
so the working configuration survives an SD card reflash, which is currently the single point of
failure for this knowledge.

The two paths share the entire analysis half. `survey run` is the same code `survey capture` calls
after recording, so nothing about the results depends on which recorder produced the file.

## When to use it

- SoapySDR or the SDRplay driver module will not install or enumerate the device.
- `survey capture` fails at the site and there is no time to debug.
- You need a userspace ring buffer to ride out storage stalls — see "Why this still matters" below.

## The tool

[github.com/fventuri/rsp-recorder](https://github.com/fventuri/rsp-recorder), built locally on the
Pi. It links `libsdrplay_api` directly and does not use SoapySDR at all, which is exactly why it is
a useful fallback: it shares no dependency with the primary path beyond the SDRplay API service
itself.

### Known upstream bug in the `-g` argument parser

The `-g` option is parsed with `sscanf(optarg, "%d,AGC", &gRdB) == 1`. `sscanf` returns 1 as soon as
the `%d` conversion succeeds, whether or not the literal `,AGC` suffix matched — so **any** numeric
`-g` value falls into the "AGC requested" branch and the tool exits with:

```
only one AGC value allowed in single tuner (or master/slave) mode
```

There is no numeric `-g` value that avoids this in an unpatched build. The fix is to require the
keyword to actually be present before running the AGC-branch `sscanf`, i.e. guard each AGC branch
with `strstr(optarg, "AGC") != NULL &&`. This patch was applied locally to the working copy on the
Pi; if you rebuild from a fresh clone you must reapply it. Verify with a numeric `-g` value: a
patched build prints the tuner settings and starts streaming, an unpatched one exits with the
message above.

### Working invocation

Confirmed working on the Pi 5 + RSP1B. Adjust rate, duration and output name; keep the rest.

```bash
cd ~/Projects/rsp-recorder/build
./rsp-recorder -v \
  -r 2000000 \
  -f 867881250 \
  -g 40,40 \
  -l 4 \
  -x 90 \
  -k 20000000 \
  -t SDRconnect \
  -o '{SDRCONNECT}_site.wav'
```

- `-r` sample rate, `-f` center frequency Hz, `-x` duration seconds.
- `-g 40,40` IF gain reduction in dB (higher = less sensitive), `-l 4` LNA state. These are *gain
  reduction* values, the SDRplay convention — not the same units as `survey capture --gain`.
- `-k` sample buffer capacity. Larger absorbs longer write stalls at the cost of RAM.
- `-t SDRconnect -o '{SDRCONNECT}_...'` produces the `SDRconnect_IQ_<date>_<time>_<freq>HZ` filename
  convention. **`{SDRCONNECT}` must be at the start of the name.** This matters: it is the fallback
  `survey/discovery.py::resolve_capture_time()` parses when there is no readable `auxi` chunk.

Watch the summary it prints. `blocks buffer full`, a `total samples` well below `rate x duration`, or
a large `max write elapsed` all mean the storage could not keep up and the capture is short.

## Analysis, with GPS

```bash
# Always check the first capture at a new site before committing to a long one
dmr-surveyor inspect ~/Projects/rsp-recorder/build/SDRconnect_IQ_*.wav -o runs/inspect_<site>

dmr-surveyor survey run ~/Projects/rsp-recorder/build/SDRconnect_IQ_*.wav \
  --band central_800_recon \
  --site config/sites/<site>.yaml \
  --run-id <site>_<YYYYMMDD> \
  --output runs/survey/<site>_<YYYYMMDD> \
  --gps-url http://<phone-hotspot-ip>:8765/location
```

`survey run` takes `--gps-url`, `--latitude` and `--longitude` exactly as `survey capture` does, so
this path is not second-class — the run lands in the database with coordinates either way. Note the
coordinates describe where you are when you *analyze*, so run it at the capture site, or pass
`--latitude`/`--longitude` explicitly afterwards.

## Why this still matters even when the primary path works

`survey capture` writes synchronously: it reads a chunk from the device and writes it before reading
the next, so its only shock absorber against a storage stall is whatever the SoapySDR driver holds
internally. rsp-recorder buffers in RAM behind a separate writer thread, which is why a 15-second
request at 10 MS/s — four times the SD card's throughput — still produced ~13.5 seconds of data
rather than ~4.

Below the storage's sustained rate the difference does not matter, and `survey capture` is the
better tool (correct filename, real `auxi` chunk, GPS, capture manifest, one command). Above it,
neither tool can record continuously, but rsp-recorder degrades more gracefully. Prefer setting a
sample rate the storage can actually sustain — `dmr-surveyor survey preflight` measures it — over
relying on either tool's buffering.
