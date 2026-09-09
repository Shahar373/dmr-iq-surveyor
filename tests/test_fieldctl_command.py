"""`scripts/fieldctl`: the launch command it builds, and what it refuses to do.

These tests run the real script through `bash`. That is a deliberate first
for this suite -- nothing else here spawns a process -- and the reason is
that the alternative tests nothing: a Python reimplementation of the argv
would keep passing while the shipped script was broken, and the shipped
script is what systemd runs.

It stays hermetic. `bash scripts/fieldctl print-command` resolves
configuration, assembles an argv and prints it; it does not exec anything,
open an SDR, bind a socket, read /etc, or call sudo or systemctl. The
environment is built from scratch rather than inherited, FIELD_ENV_FILE
points at /dev/null so a real /etc/dmr-field on the machine running the
suite cannot change the result, and stubs on PATH prove that the commands
which would touch the system are never reached.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIELDCTL = REPO_ROOT / "scripts" / "fieldctl"
ENV_EXAMPLE = REPO_ROOT / "deploy" / "field.env.example"

TOKEN_VALUE = "s3cret-token-value"


def _stub_bin(tmp_path: Path) -> Path:
    """A PATH entry holding stubs for every command that would touch the
    system. Each records that it ran, so a test can assert it did not."""
    directory = tmp_path / "bin"
    directory.mkdir()
    for name in ("sudo", "systemctl", "tailscale", "journalctl"):
        stub = directory / name
        stub.write_text(
            f'#!/bin/sh\necho "$@" >> "{tmp_path}/{name}.called"\nexit 0\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
    return directory


def _config(tmp_path: Path, **overrides: str) -> dict[str, str]:
    site = tmp_path / "g4_field.yaml"
    site.write_text("site_id: g4\nlabel: g4\n", encoding="utf-8")
    token = tmp_path / "token"
    token.write_text(TOKEN_VALUE + "\n", encoding="utf-8")
    token.chmod(0o600)

    environment = {
        # Built from scratch: nothing of the developer's environment leaks in.
        "PATH": f"{_stub_bin(tmp_path)}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "FIELD_ENV_FILE": "/dev/null",
        "FIELD_TAILSCALE_IP": "100.90.110.54",
        "FIELD_SITE": str(site),
        "FIELD_TOKEN_FILE": str(token),
        "FIELD_SURVEYOR_BIN": "/opt/dmr-field/venv/bin/dmr-surveyor",
    }
    environment.update(overrides)
    return environment


def _run(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(FIELDCTL), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
        check=False,
    )


def _argv(tmp_path: Path, **overrides: str) -> list[str]:
    result = _run(_config(tmp_path, **overrides), "print-command")
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


# -- the command it builds ------------------------------------------------


def test_print_command_emits_the_argv_the_service_would_exec(tmp_path: Path) -> None:
    argv = _argv(tmp_path)
    assert argv[0] == "/opt/dmr-field/venv/bin/dmr-surveyor"
    assert argv[1:3] == ["web", "serve"]


def test_the_default_host_is_the_tailscale_address_not_every_interface(
    tmp_path: Path,
) -> None:
    argv = _argv(tmp_path)
    assert argv[argv.index("--host") + 1] == "100.90.110.54"
    assert "0.0.0.0" not in argv


def test_an_explicit_host_wins_and_no_address_lookup_happens(tmp_path: Path) -> None:
    """An operator on a hotspot with no tailnet sets FIELD_HOST and the
    Tailscale lookup is skipped entirely."""
    argv = _argv(tmp_path, FIELD_HOST="127.0.0.1", FIELD_TAILSCALE_IP="")
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert not (tmp_path / "tailscale.called").exists()


def test_the_token_value_never_appears_in_the_generated_argv(tmp_path: Path) -> None:
    """The point of the whole exercise: the service names a file, so the
    secret is not in /proc/<pid>/cmdline for `systemctl status` to print."""
    argv = _argv(tmp_path)
    assert "--token-file" in argv
    assert "--token" not in argv
    assert TOKEN_VALUE not in " ".join(argv)


def test_verbose_request_logging_is_never_enabled(tmp_path: Path) -> None:
    """--verbose logs request lines, and those carry ?token= for the event
    stream and the export download -- straight into the journal."""
    assert "--verbose" not in _argv(tmp_path)


def test_the_tls_pair_is_pinned_rather_than_reissued_on_demand(tmp_path: Path) -> None:
    """Bare --tls would self-sign, and the self-signing helper folds the
    machine's current default-route address into the names it wants -- so a
    Pi that changed networks would get a new certificate with a new
    fingerprint, and every phone that had accepted the old one would be sent
    back to the browser warning."""
    argv = _argv(tmp_path)
    assert "--tls-cert" in argv
    assert "--tls-key" in argv
    assert "--tls" not in argv


@pytest.mark.parametrize(
    "flag", ["--site", "--database", "--output", "--token-file", "--tls-cert", "--tls-key"]
)
def test_every_path_argument_is_absolute(tmp_path: Path, flag: str) -> None:
    """Band and site names, and the default database path, all resolve
    relative to the current directory in the library, so a relative value
    here would silently depend on WorkingDirectory."""
    argv = _argv(tmp_path)
    assert argv[argv.index(flag) + 1].startswith("/")


def test_a_value_containing_spaces_survives_as_a_single_argument(
    tmp_path: Path,
) -> None:
    """The argv is built as a bash array and exec'd, so quoting is structural
    rather than textual. --null is what lets this be asserted unambiguously."""
    spaced = tmp_path / "a site with spaces.yaml"
    spaced.write_text("site_id: g4\nlabel: g4\n", encoding="utf-8")
    result = _run(_config(tmp_path, FIELD_SITE=str(spaced)), "print-command", "--null")
    assert result.returncode == 0, result.stderr
    argv = result.stdout.split("\0")[:-1]
    assert str(spaced) in argv
    assert argv[argv.index("--site") + 1] == str(spaced)


def test_the_environment_file_is_parsed_the_way_systemd_parses_it(
    tmp_path: Path,
) -> None:
    """EnvironmentFile= is not shell: no expansion, no substitution, and a
    value with spaces is just that value. Sourcing it instead would make
    fieldctl disagree with the running service about its own configuration."""
    env_file = tmp_path / "field.env"
    env_file.write_text(
        "# a comment\n\nFIELD_BAND=central_800 narrow\nFIELD_PORT=9999\n",
        encoding="utf-8",
    )
    argv = _argv(tmp_path, FIELD_ENV_FILE=str(env_file))
    assert argv[argv.index("--band") + 1] == "central_800 narrow"
    assert argv[argv.index("--port") + 1] == "9999"


# -- what it refuses to do ------------------------------------------------


def test_without_any_address_it_fails_instead_of_binding_every_interface(
    tmp_path: Path,
) -> None:
    """No FIELD_HOST and no Tailscale address is a failure, never a fallback.
    Failing lets systemd retry and bind correctly once the tailnet appears; a
    successful start on 0.0.0.0 -- or on loopback -- would never re-bind."""
    result = _run(_config(tmp_path, FIELD_TAILSCALE_IP=""), "print-command")
    assert result.returncode != 0
    assert "0.0.0.0" not in result.stdout
    assert "Tailscale" in result.stderr


def test_a_missing_site_profile_is_refused_with_a_usable_message(
    tmp_path: Path,
) -> None:
    """There is no default site profile: it records the antenna, receiver and
    fixed gain, and guessing one records every stop against the wrong
    equipment context."""
    result = _run(_config(tmp_path, FIELD_SITE=""), "print-command")
    assert result.returncode != 0
    assert "FIELD_SITE" in result.stderr


def test_a_site_profile_that_does_not_exist_is_refused_before_the_service_starts(
    tmp_path: Path,
) -> None:
    result = _run(_config(tmp_path, FIELD_SITE=str(tmp_path / "absent.yaml")), "print-command")
    assert result.returncode != 0


def test_building_the_command_touches_neither_sudo_nor_systemctl(
    tmp_path: Path,
) -> None:
    _argv(tmp_path)
    for name in ("sudo", "systemctl", "tailscale", "journalctl"):
        assert not (tmp_path / f"{name}.called").exists(), f"{name} was invoked"


def test_an_unknown_subcommand_exits_non_zero_with_usage(tmp_path: Path) -> None:
    result = _run(_config(tmp_path), "frobnicate")
    assert result.returncode != 0
    assert "usage: fieldctl" in result.stderr


def test_no_subcommand_at_all_exits_non_zero_with_usage(tmp_path: Path) -> None:
    result = _run(_config(tmp_path))
    assert result.returncode != 0
    assert "usage: fieldctl" in result.stderr


# -- wait-network ---------------------------------------------------------


def test_wait_network_fails_when_no_address_ever_appears(tmp_path: Path) -> None:
    result = _run(
        _config(tmp_path, FIELD_TAILSCALE_IP="", FIELD_TAILSCALE_WAIT="0"), "wait-network"
    )
    assert result.returncode != 0


def test_wait_network_returns_at_once_when_the_host_is_pinned(tmp_path: Path) -> None:
    """A pinned FIELD_HOST means there is nothing to wait for, and a Pi with
    no tailnet must not be held at the start gate for 90 seconds."""
    result = _run(
        _config(tmp_path, FIELD_HOST="127.0.0.1", FIELD_TAILSCALE_IP="", FIELD_TAILSCALE_WAIT="0"),
        "wait-network",
    )
    assert result.returncode == 0


def test_wait_network_succeeds_once_an_address_exists(tmp_path: Path) -> None:
    result = _run(_config(tmp_path), "wait-network")
    assert result.returncode == 0
    assert "100.90.110.54" in result.stderr


# -- the two shipped files stay in step -----------------------------------


def test_the_env_example_documents_every_variable_the_wrapper_reads() -> None:
    """The drift test. Six months from now the failure mode is a new
    FIELD_* knob that works on the machine it was written on and is
    undocumented everywhere else."""
    # FIELD_ENV_FILE and FIELD_UNIT are how fieldctl finds the env file and
    # names the unit; they cannot be configured from inside the file they
    # locate, so they are set in the environment or not at all.
    bootstrap = {"FIELD_ENV_FILE", "FIELD_UNIT"}
    # @FIELD_USER@ and friends are the unit template's placeholders, which
    # render-unit substitutes; they are not configuration this script reads.
    source = re.sub(r"@FIELD_[A-Z0-9_]+@", "", FIELDCTL.read_text(encoding="utf-8"))
    read = set(re.findall(r"FIELD_[A-Z0-9_]+", source)) - bootstrap
    documented = set(
        re.findall(r"^#?\s*(FIELD_[A-Z0-9_]+)=", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M)
    )
    assert read - documented == set(), "read by fieldctl but not documented"
    assert documented - read == set(), "documented but never read"


# -- conventions ----------------------------------------------------------


def test_fieldctl_is_executable_and_follows_the_repository_shell_conventions() -> None:
    """docs/install-raspberry-pi.md runs `chmod +x scripts/*.sh`, which an
    extensionless fieldctl would miss, so the committed mode has to be right."""
    assert os.access(FIELDCTL, os.X_OK), "scripts/fieldctl must be committed executable"
    lines = FIELDCTL.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in lines
