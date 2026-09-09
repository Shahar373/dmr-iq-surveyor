#!/usr/bin/env bash
# Install (or remove) the dmr-iq-surveyor field service on a Raspberry Pi.
#
#   sudo ./scripts/install_field_service.sh --site /etc/dmr-field/sites/g4_field.yaml
#   ./scripts/install_field_service.sh --dry-run --site /path/to/site.yaml
#   sudo ./scripts/install_field_service.sh --uninstall [--purge]
#
# Paths: --prefix (default /opt/dmr-field), --conf-dir (/etc/dmr-field),
# --state-dir (/var/lib/dmr-field), --user (shahar).
#
# Run it from the deployment checkout, not from a development machine. It is
# safe to re-run: the token and the TLS pair are created once and then left
# alone, which is exactly what lets a phone bookmark survive an update.
#
# --dry-run prints every action and changes nothing. It never calls sudo,
# systemctl, openssl or install(1), so it is the safe way to read what this
# would do -- including on a laptop.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PREFIX="/opt/dmr-field"
CONF_DIR="/etc/dmr-field"
STATE_DIR="/var/lib/dmr-field"
UNIT_NAME="dmr-field.service"
FIELDCTL_TARGET="/usr/local/bin/fieldctl"
SERVICE_USER="shahar"
SERVICE_GROUP=""
SITE_PROFILE=""
DRY_RUN=0
UNINSTALL=0
PURGE=0
ASSUME_YES=0

# The two checkouts on the Pi that this deployment must never become: the
# operator's working copy, and a detached staging clone. A service pointing
# at either would be restarted by an unrelated `git checkout`.
FORBIDDEN_PATHS=(
    "/home/shahar/Projects/dmr-iq-surveyor"
    "/home/shahar/Projects/dmr-iq-surveyor-stage"
)

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mOK\033[0m   %s\n' "$*"; }
warn() { printf '    \033[33mWARN\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mFAILED:\033[0m %s\n' "$*" >&2; exit 1; }

run() {
    if (( DRY_RUN )); then
        printf '    would: %s\n' "$*"
    else
        "$@"
    fi
}

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --site)      SITE_PROFILE="${2:?--site needs a path}"; shift 2 ;;
        --prefix)    PREFIX="${2:?--prefix needs a path}"; shift 2 ;;
        --conf-dir)  CONF_DIR="${2:?--conf-dir needs a path}"; shift 2 ;;
        --state-dir) STATE_DIR="${2:?--state-dir needs a path}"; shift 2 ;;
        --user)      SERVICE_USER="${2:?--user needs a name}"; shift 2 ;;
        --group)     SERVICE_GROUP="${2:?--group needs a name}"; shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --purge)     PURGE=1; shift ;;
        --yes|-y)    ASSUME_YES=1; shift ;;
        -h|--help)   usage ;;
        *)           die "unknown argument: $1" ;;
    esac
done

: "${SERVICE_GROUP:=$SERVICE_USER}"
APP_DIR="${PREFIX}/app"
VENV_DIR="${PREFIX}/venv"

# --- removal ------------------------------------------------------------

if (( UNINSTALL )); then
    say "Removing ${UNIT_NAME}"
    run systemctl disable --now "$UNIT_NAME"
    run rm -f "/etc/systemd/system/${UNIT_NAME}"
    run systemctl daemon-reload
    run rm -f "$FIELDCTL_TARGET"
    if (( PURGE )); then
        warn "--purge also removes ${CONF_DIR} (token included) and ${STATE_DIR} (recordings, database, TLS pair)"
        run rm -rf "$CONF_DIR" "$STATE_DIR"
    else
        ok "kept ${CONF_DIR} and ${STATE_DIR}; pass --purge to remove them too"
    fi
    say "Done"
    exit 0
fi

# --- preconditions ------------------------------------------------------

say "1/7  Checking where this would install"

for forbidden in "${FORBIDDEN_PATHS[@]}"; do
    # Equal to, or anywhere beneath: `--prefix /home/shahar/Projects/dmr-iq-surveyor`
    # would put APP_DIR one level down and is just as wrong. A permanent
    # service must not live in a checkout that an unrelated `git checkout`
    # can change underneath it.
    if [[ "$APP_DIR" == "$forbidden" || "$APP_DIR" == "$forbidden"/* \
       || "$PREFIX" == "$forbidden" || "$PREFIX" == "$forbidden"/* ]]; then
        die "refusing to deploy into ${APP_DIR}: ${forbidden} is an existing working checkout, not a deployment target"
    fi
done
ok "deployment target ${APP_DIR}"

if (( ! DRY_RUN )); then
    [[ "$(id -u)" == "0" ]] || die "must run as root (use sudo), or pass --dry-run to see what it would do"
    if (( ! ASSUME_YES )); then
        [[ -f /etc/rpi-issue ]] || die "this does not look like a Raspberry Pi (no /etc/rpi-issue). Installing a real service on a development machine is almost never intended; pass --yes to override."
    fi
fi

[[ -n "$SITE_PROFILE" ]] || die "--site is required: the absolute path of this campaign's site profile. There is no default, because the profile records the antenna, receiver and fixed gain, and guessing one records every stop against the wrong equipment context."
[[ "$SITE_PROFILE" == /* ]] || die "--site must be an absolute path"

say "2/7  Checking the deployment checkout and its virtualenv"
if (( DRY_RUN )) && [[ ! -d "$APP_DIR" ]]; then
    warn "${APP_DIR} does not exist yet; a real run would refuse until it does"
else
    [[ -d "$APP_DIR/.git" ]] || die "no checkout at ${APP_DIR}. Create it first:
      sudo mkdir -p ${PREFIX}
      sudo git clone https://github.com/Shahar373/dmr-iq-surveyor.git ${APP_DIR}
      sudo python3 -m venv ${VENV_DIR}
      sudo ${VENV_DIR}/bin/pip install -e ${APP_DIR}"
    [[ -x "$VENV_DIR/bin/dmr-surveyor" ]] || die "no dmr-surveyor in ${VENV_DIR}. Run: sudo ${VENV_DIR}/bin/pip install -e ${APP_DIR}"

    # The failure this exists to prevent: a service that starts, serves the
    # UI, and fails every capture. A virtualenv created without
    # --system-site-packages cannot see Debian's python3-soapysdr, and
    # nothing else in the startup path notices.
    if "$VENV_DIR/bin/python" -c 'import SoapySDR' 2>/dev/null; then
        ok "the deployment venv can import SoapySDR"
    else
        die "${VENV_DIR} cannot import SoapySDR, so every capture would fail while the app looked healthy. Link the system bindings in first:
      sudo VENV=${VENV_DIR} bash ${APP_DIR}/scripts/pi_soapysdr_setup.sh"
    fi
fi

# --- directories --------------------------------------------------------

say "3/7  Creating configuration and state directories"
run install -d -m 0755 -o root -g root "$CONF_DIR"
run install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STATE_DIR"
run install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "${STATE_DIR}/tls"
run install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "${STATE_DIR}/inventory"
ok "${CONF_DIR} and ${STATE_DIR}"

# --- the token ----------------------------------------------------------

say "4/7  Ensuring the shared API token"
TOKEN_FILE="${CONF_DIR}/token"
if [[ -f "$TOKEN_FILE" ]]; then
    # Never regenerated. A new token invalidates the bookmark on the phone,
    # which is the exact problem this whole service exists to fix.
    ok "keeping the existing token at ${TOKEN_FILE}"
    run chmod 600 "$TOKEN_FILE"
    run chown "${SERVICE_USER}:${SERVICE_GROUP}" "$TOKEN_FILE"
elif (( DRY_RUN )); then
    printf '    would: generate a token into %s (mode 0600)\n' "$TOKEN_FILE"
else
    # hex, not base64: the token is delivered as ?token=... and base64
    # produces +, / and = , none of which survive a URL query string
    # unescaped. 24 bytes is 192 bits.
    ( umask 077 && openssl rand -hex 24 > "$TOKEN_FILE" ) \
        || die "could not generate a token with openssl"
    chmod 600 "$TOKEN_FILE"
    chown "${SERVICE_USER}:${SERVICE_GROUP}" "$TOKEN_FILE"
    ok "generated ${TOKEN_FILE} (mode 0600)"
fi

# --- the environment file ----------------------------------------------

say "5/7  Writing ${CONF_DIR}/field.env"
ENV_FILE="${CONF_DIR}/field.env"
if [[ -f "$ENV_FILE" ]]; then
    ok "keeping the existing ${ENV_FILE}; delete it to regenerate from the example"
else
    render_env() {
        sed -e "s|^FIELD_SURVEYOR_BIN=.*|FIELD_SURVEYOR_BIN=${VENV_DIR}/bin/dmr-surveyor|" \
            -e "s|^FIELD_SITE=.*|FIELD_SITE=${SITE_PROFILE}|" \
            -e "s|^FIELD_TOKEN_FILE=.*|FIELD_TOKEN_FILE=${TOKEN_FILE}|" \
            -e "s|^FIELD_OUTPUT=.*|FIELD_OUTPUT=${STATE_DIR}|" \
            -e "s|^FIELD_DATABASE=.*|FIELD_DATABASE=${STATE_DIR}/inventory/dmr_inventory.sqlite3|" \
            -e "s|^FIELD_TLS_CERT=.*|FIELD_TLS_CERT=${STATE_DIR}/tls/field-app.crt|" \
            -e "s|^FIELD_TLS_KEY=.*|FIELD_TLS_KEY=${STATE_DIR}/tls/field-app.key|" \
            "${ROOT_DIR}/deploy/field.env.example"
    }
    if (( DRY_RUN )); then
        printf '    would: write %s containing --\n' "$ENV_FILE"
        render_env | grep -E '^FIELD_' | sed 's/^/           /'
    else
        render_env > "$ENV_FILE"
        chmod 0644 "$ENV_FILE"
        ok "wrote ${ENV_FILE}"
    fi
fi

[[ -f "$SITE_PROFILE" ]] || warn "site profile ${SITE_PROFILE} does not exist yet; the service will refuse to start until it does"

# --- TLS ----------------------------------------------------------------

say "6/7  Ensuring the pinned TLS certificate"
CERT_FILE="${STATE_DIR}/tls/field-app.crt"
if [[ -f "$CERT_FILE" ]]; then
    # Reissuing changes the fingerprint, and every phone that had accepted
    # the old certificate is sent back to the browser's warning page.
    ok "keeping the existing certificate at ${CERT_FILE}"
elif (( DRY_RUN )); then
    printf '    would: issue a certificate into %s/tls covering the Tailscale address and MagicDNS name\n' "$STATE_DIR"
else
    TS_HOSTS=()
    if command -v tailscale >/dev/null 2>&1; then
        while read -r address; do
            [[ -n "$address" ]] && TS_HOSTS+=("$address")
        done < <(tailscale ip -4 2>/dev/null || true)
        DNS_NAME="$(tailscale status --json 2>/dev/null \
            | "$VENV_DIR/bin/python" -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' \
            2>/dev/null || true)"
        [[ -n "${DNS_NAME:-}" ]] && TS_HOSTS+=("$DNS_NAME")
    fi
    TS_HOSTS+=("$(hostname)" "$(hostname).local")
    printf '    covering: %s\n' "${TS_HOSTS[*]}"
    FIELD_TLS_HOSTS="${TS_HOSTS[*]}" "$VENV_DIR/bin/python" - <<'PYEOF' || die "could not issue a certificate"
import os
import sys

from dmr_iq_surveyor.web.tls import ensure_self_signed

hosts = [h for h in os.environ.get("FIELD_TLS_HOSTS", "").split() if h]
certificate = ensure_self_signed(sys.argv[1] if len(sys.argv) > 1 else "/var/lib/dmr-field/tls", hosts=hosts)
print(f"    certificate {certificate.certificate_path}")
print(f"    valid for:  {', '.join(certificate.hosts)}")
print(f"    expires:    {certificate.not_after}")
print(f"    SHA-256:    {certificate.fingerprint_sha256}")
PYEOF
    chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${STATE_DIR}/tls"
    chmod 600 "${STATE_DIR}/tls/field-app.key"
    ok "issued ${CERT_FILE}"
fi

# --- the unit -----------------------------------------------------------

say "7/7  Installing ${UNIT_NAME}"
run install -m 0755 "${ROOT_DIR}/scripts/fieldctl" "$FIELDCTL_TARGET"
if (( DRY_RUN )); then
    printf '    would: render the unit with user=%s group=%s workdir=%s fieldctl=%s\n' \
        "$SERVICE_USER" "$SERVICE_GROUP" "$APP_DIR" "$FIELDCTL_TARGET"
    bash "${ROOT_DIR}/scripts/fieldctl" render-unit \
        "$SERVICE_USER" "$SERVICE_GROUP" "$APP_DIR" "$FIELDCTL_TARGET" \
        | grep -E '^(ExecStart|User|Group|WorkingDirectory)' | sed 's/^/           /'
else
    bash "${ROOT_DIR}/scripts/fieldctl" render-unit \
        "$SERVICE_USER" "$SERVICE_GROUP" "$APP_DIR" "$FIELDCTL_TARGET" \
        > "/etc/systemd/system/${UNIT_NAME}"
    chmod 0644 "/etc/systemd/system/${UNIT_NAME}"
fi
run systemctl daemon-reload
run systemctl enable "$UNIT_NAME"

say "Done"
if (( DRY_RUN )); then
    printf '    Nothing was changed. Re-run with sudo and without --dry-run to install.\n'
else
    cat <<EOF
    Start it and check:

      sudo systemctl start ${UNIT_NAME}
      fieldctl status
      fieldctl logs -n 50

    The phone URL, token included, is printed by:

      fieldctl url
EOF
fi
