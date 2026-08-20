#!/usr/bin/env bash
# Install and verify everything `dmr-surveyor survey capture` needs to talk
# to an SDRplay RSP on a Raspberry Pi, and make it importable from this
# project's virtualenv.
#
# Run once, at home, on mains power and a real network -- NOT in the field.
#
#   bash scripts/pi_soapysdr_setup.sh
#
# It is safe to re-run: every step checks before it acts.
#
# The venv step is the one that is easy to miss. Debian's python3-soapysdr
# installs into /usr/lib/python3/dist-packages, which a virtualenv created
# without --system-site-packages does NOT see. `import SoapySDR` then works
# in system python and fails inside the venv where dmr-surveyor actually
# runs -- a failure that would otherwise surface at the capture site.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
BUILD_DIR="${SOAPY_BUILD_DIR:-$HOME/Projects}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mOK\033[0m  %s\n' "$*"; }
warn() { printf '    \033[33mWARN\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mFAILED:\033[0m %s\n' "$*" >&2; exit 1; }

say "1/6  Checking the SDRplay API service"
if ! systemctl is-active --quiet sdrplay 2>/dev/null && ! pgrep -f sdrplay_apiService >/dev/null 2>&1; then
    warn "the SDRplay API service does not look like it is running."
    warn "If capture later reports ServiceNotResponding, run:  sudo systemctl restart sdrplay"
else
    ok "SDRplay API service is running"
fi
if lsusb 2>/dev/null | grep -qi '1df7'; then
    ok "an SDRplay device is attached (USB vendor 1df7)"
else
    warn "no SDRplay device seen on USB right now -- fine if it is unplugged, plug it in before capturing"
fi

say "2/6  Installing SoapySDR and its Python bindings"
if python3 -c 'import SoapySDR' 2>/dev/null; then
    ok "system python can already import SoapySDR"
else
    sudo apt-get update -qq || die "apt-get update failed"
    sudo apt-get install -y git cmake g++ libsoapysdr-dev soapysdr-tools python3-soapysdr \
        || die "could not install SoapySDR packages"
    python3 -c 'import SoapySDR' 2>/dev/null \
        || die "SoapySDR still not importable from system python after install"
    ok "installed"
fi

say "3/6  Checking for the SDRplay driver module (SoapySDRPlay3)"
if SoapySDRUtil --info 2>/dev/null | grep -qi 'sdrplay'; then
    ok "the sdrplay driver module is registered with SoapySDR"
else
    warn "sdrplay driver module not found -- building SoapySDRPlay3 from source"
    warn "(it is not in the Debian archive because it links the proprietary SDRplay API)"
    mkdir -p "$BUILD_DIR" || die "cannot create $BUILD_DIR"
    if [ ! -d "$BUILD_DIR/SoapySDRPlay3/.git" ]; then
        git clone https://github.com/pothosware/SoapySDRPlay3.git "$BUILD_DIR/SoapySDRPlay3" \
            || die "git clone failed"
    fi
    mkdir -p "$BUILD_DIR/SoapySDRPlay3/build"
    (
        cd "$BUILD_DIR/SoapySDRPlay3/build" || exit 1
        cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig
    ) || die "SoapySDRPlay3 build failed -- read the output above; the usual cause is the SDRplay API headers not being installed"
    SoapySDRUtil --info 2>/dev/null | grep -qi 'sdrplay' \
        || die "built SoapySDRPlay3 but SoapySDR still does not list an sdrplay module"
    ok "built and registered"
fi

say "4/6  Making SoapySDR importable from the project virtualenv"
[ -d "$VENV" ] || die "no virtualenv at $VENV -- create it first: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
if "$VENV/bin/python" -c 'import SoapySDR' 2>/dev/null; then
    ok "the venv can already import SoapySDR"
else
    SITE_PACKAGES="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    DIST_PACKAGES="$(python3 -c 'import SoapySDR, os; print(os.path.dirname(os.path.abspath(SoapySDR.__file__)))')"
    [ -n "$DIST_PACKAGES" ] || die "could not locate the system SoapySDR module"
    linked=0
    for path in "$DIST_PACKAGES"/SoapySDR.py "$DIST_PACKAGES"/_SoapySDR*.so; do
        [ -e "$path" ] || continue
        ln -sf "$path" "$SITE_PACKAGES/" && linked=1
    done
    [ "$linked" = 1 ] || die "found no SoapySDR.py / _SoapySDR*.so to link from $DIST_PACKAGES"
    "$VENV/bin/python" -c 'import SoapySDR' 2>/dev/null \
        || die "linked the module but the venv still cannot import SoapySDR"
    ok "linked $DIST_PACKAGES into $SITE_PACKAGES"
fi

say "5/6  Enumerating devices through the exact code path capture uses"
"$VENV/bin/python" - <<'PYEOF'
import sys

from dmr_iq_surveyor.capture.device import probe_soapysdr

probe = probe_soapysdr("sdrplay")
if not probe.available:
    print(f"    device probe FAILED: {probe.probe_error}")
    sys.exit(1)
print(f"    device: {probe.resolved_label}")
for found in probe.devices_found:
    print(f"    {found}")
PYEOF
if [ $? -ne 0 ]; then
    warn "no device enumerated. If the RSP is plugged in, try:  sudo systemctl restart sdrplay"
    warn "Everything else is installed; re-run this script after the device appears."
else
    ok "the capture code path can see the device"
fi

say "6/6  Done"
cat <<'EOF'
    Next, run a preflight against the directory you will actually record into:

      dmr-surveyor survey preflight ~/Projects/dmr-iq-surveyor/runs/recordings \
        --band central_800_recon --sample-rate 2000000 --duration 90

    It measures real write throughput and tells you the highest sample rate
    this storage can sustain. Do that BEFORE leaving.
EOF
