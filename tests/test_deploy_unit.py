"""The shipped systemd unit template and its environment example.

Neither file is executed by the test suite -- installing a real service is
exactly what these tests must not do -- so they are checked the way
tests/test_web_bootstrap.py checks the shipped static assets: as text, for
the properties that would otherwise only be discovered on a Raspberry Pi in
a car park.

Several of these are silent failures rather than loud ones, which is why
they are worth a test at all. StartLimitIntervalSec in the wrong section is
accepted and ignored. A Requires= on a unit that is not installed refuses
the whole service. PrivateDevices=yes starts cleanly and then fails every
capture.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO_ROOT / "deploy" / "dmr-field.service"
ENV_EXAMPLE_PATH = REPO_ROOT / "deploy" / "field.env.example"

# The paths on the Pi that this deployment must never point at: one is the
# operator's dirty working checkout, the other a detached staging checkout.
FORBIDDEN_CHECKOUTS = (
    "/home/shahar/Projects/dmr-iq-surveyor",
    "/home/shahar/Projects/dmr-iq-surveyor-stage",
)


def _directives() -> list[tuple[str, str, str]]:
    """Parse the unit into (section, key, value) triples.

    Hand-rolled rather than configparser because systemd allows the same key
    many times in one section -- After= and ExecStartPre= both rely on it --
    and configparser keeps only the last occurrence, which would quietly
    discard most of what these tests are checking.
    """
    found: list[tuple[str, str, str]] = []
    section = ""
    for raw in UNIT_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, separator, value = line.partition("=")
        if separator:
            found.append((section, key.strip(), value.strip()))
    return found


def _values(key: str, section: str | None = None) -> list[str]:
    return [
        value
        for found_section, found_key, value in _directives()
        if found_key == key and (section is None or found_section == section)
    ]


def _env_example() -> dict[str, str]:
    """The example env file as systemd's EnvironmentFile= would read it:
    plain KEY=VALUE, no expansion, `#` comments only at the start of a line."""
    settings: dict[str, str] = {}
    for raw in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            settings[key.strip()] = value.strip()
    return settings


# -- ordering and dependencies -------------------------------------------


def test_the_unit_waits_for_the_network_to_be_usable_not_merely_configured() -> None:
    """The app binds one specific address. network.target only promises the
    stack is up, so it can be reached before that address exists."""
    assert "network-online.target" in _values("Wants", "Unit")
    assert "network-online.target" in " ".join(_values("After", "Unit"))


def test_the_unit_is_ordered_after_tailscale_and_the_sdrplay_api() -> None:
    after = " ".join(_values("After", "Unit"))
    assert "tailscaled.service" in after
    assert "sdrplay.service" in after


def test_nothing_is_required_so_a_missing_dependency_cannot_refuse_the_start() -> None:
    """Wants=, never Requires=. sdrplay.service may not exist under that name
    on every install -- scripts/pi_soapysdr_setup.sh also pgreps for the
    daemon -- and a Pi with no tailnet must still serve the app."""
    assert _values("Requires") == []
    wants = " ".join(_values("Wants", "Unit"))
    assert "tailscaled.service" in wants
    assert "sdrplay.service" in wants


# -- stopping and restarting ---------------------------------------------


def test_the_server_is_stopped_with_sigint_so_it_can_release_the_sdr() -> None:
    """serve_forever() installs no signal handler: it cleans up (shutdown()
    then server_close(), which releases the probe child) only on
    KeyboardInterrupt. Default SIGTERM would kill it before any of that."""
    assert _values("KillSignal", "Service") == ["SIGINT"]


def test_only_the_main_process_is_signalled_so_it_can_reap_its_own_child() -> None:
    """Under the default control-group mode systemd would SIGINT the probe
    subprocess directly, racing the parent's own bounded close()."""
    assert _values("KillMode", "Service") == ["mixed"]


def test_a_clean_stop_and_a_startup_interrupt_are_not_treated_as_crashes() -> None:
    """A SIGINT landing during startup -- inside the openssl call that issues
    a certificate, say -- propagates out and exits 130."""
    assert "130" in " ".join(_values("SuccessExitStatus", "Service"))


def test_a_usage_error_is_never_retried() -> None:
    """Exit 2 is click's usage error: a flag the unit passes is wrong, and
    retrying cannot make it right."""
    assert _values("RestartPreventExitStatus", "Service") == ["2"]


def test_the_restart_limit_is_declared_where_systemd_actually_reads_it() -> None:
    """systemd moved StartLimit* to [Unit] in v229 and silently ignores them
    in [Service] -- a misconfiguration with no error message, which is
    exactly the kind worth a test."""
    assert _values("StartLimitIntervalSec", "Unit"), "must be in [Unit]"
    assert _values("StartLimitBurst", "Unit"), "must be in [Unit]"
    assert _values("StartLimitIntervalSec", "Service") == []
    assert _values("StartLimitBurst", "Service") == []


def test_a_slow_tailnet_keeps_retrying_instead_of_hitting_the_start_limit() -> None:
    """The load-bearing arithmetic. `fieldctl wait-network` waits
    FIELD_TAILSCALE_WAIT seconds before failing, so a Pi whose Tailscale is
    slow or absent retries on a (wait + RestartSec) cycle. Fewer than
    StartLimitBurst of those must fit inside StartLimitIntervalSec, or the
    service ends up parked in start-limit-hit and never comes back when the
    tailnet does."""
    burst = int(_values("StartLimitBurst", "Unit")[0])
    interval = int(_values("StartLimitIntervalSec", "Unit")[0])
    restart_sec = int(_values("RestartSec", "Service")[0])
    wait = int(_env_example()["FIELD_TAILSCALE_WAIT"])
    assert burst * (wait + restart_sec) > interval


def test_a_fast_failure_still_stops_instead_of_looping() -> None:
    """The other half. A start that fails immediately -- an unreadable token
    file, a site profile that does not resolve -- cycles at RestartSec, and
    StartLimitBurst of those must fit inside the interval so the limit trips
    and systemd gives up."""
    burst = int(_values("StartLimitBurst", "Unit")[0])
    interval = int(_values("StartLimitIntervalSec", "Unit")[0])
    restart_sec = int(_values("RestartSec", "Service")[0])
    assert burst * restart_sec < interval
    assert _values("Restart", "Service") == ["on-failure"]
    assert restart_sec >= 5, "a shorter delay is a hot loop against the SDR"


# -- what the unit must not do -------------------------------------------


@pytest.mark.parametrize(
    ("directive", "why"),
    [
        ("PrivateDevices", "implies DevicePolicy=closed; hides /dev/bus/usb"),
        ("DeviceAllow", "any DeviceAllow= flips DevicePolicy to closed"),
        ("ProtectHome", "FIELD_OUTPUT may point at a home directory"),
        ("PrivateNetwork", "this is a network server"),
        ("PrivateTmp", "unverified against the SDRplay API's client handoff"),
        ("DynamicUser", "needs a stable uid for plugdev and the state directory"),
    ],
)
def test_the_unit_avoids_sandboxing_that_would_break_the_sdr(
    directive: str, why: str
) -> None:
    assert _values(directive) == [], why


def test_the_unit_never_binds_every_interface() -> None:
    """The API can start a capture. Binding 0.0.0.0 by default is the one
    thing the field-app deployment must not do."""
    assert "0.0.0.0" not in UNIT_PATH.read_text(encoding="utf-8")
    assert _env_example()["FIELD_HOST"] == ""


def test_no_token_value_appears_in_either_shipped_file() -> None:
    """The token is named by path and never by value. Anything set in the env
    file is visible to `systemctl show`."""
    settings = _env_example()
    assert "FIELD_TOKEN" not in settings, "the token must be given as a path, not a value"
    assert settings["FIELD_TOKEN_FILE"].startswith("/")
    assert "FIELD_TOKEN=" not in UNIT_PATH.read_text(encoding="utf-8")


def test_neither_file_carries_operational_data_that_belongs_outside_git() -> None:
    """No live Tailscale address, no GPS coordinates, no real site profile."""
    text = UNIT_PATH.read_text(encoding="utf-8") + ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    assert not re.search(r"\b100\.(?:[6-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b", text)
    assert not re.search(r"\b3[12]\.\d{3,}\b", text), "looks like a latitude"
    assert _env_example()["FIELD_SITE"] == "", "the site profile is given at install time"


def test_the_service_never_points_at_the_operators_existing_checkouts() -> None:
    text = UNIT_PATH.read_text(encoding="utf-8")
    for path in FORBIDDEN_CHECKOUTS:
        assert path not in text


# -- template shape -------------------------------------------------------


def test_the_template_placeholders_are_exactly_the_four_the_installer_fills() -> None:
    found = set(re.findall(r"@[A-Z_]+@", UNIT_PATH.read_text(encoding="utf-8")))
    assert found == {"@FIELD_USER@", "@FIELD_GROUP@", "@FIELD_WORKDIR@", "@FIELDCTL@"}


def test_the_unit_runs_and_gates_on_fieldctl_rather_than_an_inline_command() -> None:
    """One builder for the launch argv, shared by the service and by
    `fieldctl print-command`; a second copy inline here would drift."""
    assert _values("ExecStart", "Service") == ["@FIELDCTL@ exec"]
    assert _values("ExecStartPre", "Service") == ["@FIELDCTL@ wait-network"]


def test_the_unit_is_installable_and_starts_at_boot() -> None:
    assert _values("WantedBy", "Install") == ["multi-user.target"]
    assert _values("Type", "Service") == ["exec"]


def test_the_banner_is_not_lost_to_block_buffering() -> None:
    """Without PYTHONUNBUFFERED the startup banner only reaches the journal
    when the process exits, which is when it stops being useful."""
    assert "PYTHONUNBUFFERED=1" in " ".join(_values("Environment", "Service"))
