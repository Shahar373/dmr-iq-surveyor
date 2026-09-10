"""`scripts/install_field_service.sh`: its guards, and that --dry-run is inert.

Installing a real service is precisely what this suite must never do, so
every test here runs with --dry-run or stops at a guard that fires before the
first mutating step. Stubs for sudo, systemctl, openssl and install(1) sit on
PATH and record being called; a dry run that touched any of them would be a
bug, because --dry-run is what an operator is told to run first.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install_field_service.sh"

# The two checkouts on the Pi a permanent service must never be run from.
FORBIDDEN = "/home/shahar/Projects/dmr-iq-surveyor"


def _stub_bin(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    for name in ("sudo", "systemctl", "openssl", "install", "tailscale", "git"):
        stub = directory / name
        stub.write_text(
            f'#!/bin/sh\necho "$@" >> "{tmp_path}/{name}.called"\nexit 0\n', encoding="utf-8"
        )
        stub.chmod(0o755)
    return directory


def _run(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER), *arguments],
        env={"PATH": f"{_stub_bin(tmp_path)}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        check=False,
    )


def _sandbox(tmp_path: Path) -> list[str]:
    """Arguments that keep every path this installer touches inside tmp_path."""
    return [
        "--site", str(tmp_path / "site.yaml"),
        "--prefix", str(tmp_path / "prefix"),
        "--conf-dir", str(tmp_path / "etc"),
        "--state-dir", str(tmp_path / "state"),
    ]


def test_a_dry_run_succeeds_and_says_it_changed_nothing(tmp_path: Path) -> None:
    result = _run(tmp_path, "--dry-run", *_sandbox(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "Nothing was changed" in result.stdout


def test_a_dry_run_invokes_none_of_the_commands_that_would_change_the_system(
    tmp_path: Path,
) -> None:
    """This is what makes --dry-run safe to run on a development machine."""
    _run(tmp_path, "--dry-run", *_sandbox(tmp_path))
    for name in ("sudo", "systemctl", "openssl", "install", "tailscale"):
        assert not (tmp_path / f"{name}.called").exists(), f"{name} was invoked"


def test_a_dry_run_creates_no_files(tmp_path: Path) -> None:
    _run(tmp_path, "--dry-run", *_sandbox(tmp_path))
    assert not (tmp_path / "etc").exists()
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "prefix").exists()


def test_the_site_profile_must_be_given(tmp_path: Path) -> None:
    """No default: the profile records the antenna, receiver and fixed gain,
    and a guessed one records every stop against the wrong equipment."""
    result = _run(tmp_path, "--dry-run", "--prefix", str(tmp_path / "prefix"))
    assert result.returncode != 0
    assert "--site is required" in result.stderr


def test_the_site_profile_must_be_absolute(tmp_path: Path) -> None:
    """The service runs with a WorkingDirectory the operator does not see."""
    result = _run(tmp_path, "--dry-run", "--site", "config/sites/g4.yaml")
    assert result.returncode != 0
    assert "absolute" in result.stderr


def test_it_refuses_to_deploy_into_the_operators_working_checkout(
    tmp_path: Path,
) -> None:
    """A permanent service must not live in a checkout that an unrelated
    `git checkout` can change underneath it."""
    result = _run(tmp_path, "--dry-run", "--site", "/etc/dmr-field/sites/g4.yaml",
                  "--prefix", FORBIDDEN)
    assert result.returncode != 0
    assert FORBIDDEN in result.stderr


def test_it_refuses_to_deploy_into_the_detached_staging_checkout(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, "--dry-run", "--site", "/etc/dmr-field/sites/g4.yaml",
                  "--prefix", f"{FORBIDDEN}-stage")
    assert result.returncode != 0
    assert result.stderr.strip() != ""


def test_a_real_run_stops_at_a_guard_before_touching_anything(tmp_path: Path) -> None:
    """Without --dry-run it still refuses: not root, or not a Raspberry Pi, or
    no deployment checkout at the prefix. Whichever fires, it fires before the
    first mutating step, so this test cannot install a service anywhere."""
    result = _run(tmp_path, *_sandbox(tmp_path))
    assert result.returncode != 0
    assert "FAILED" in result.stderr
    assert not (tmp_path / "etc").exists()
    assert not (tmp_path / "state").exists()


def test_an_existing_token_is_kept_rather_than_regenerated(tmp_path: Path) -> None:
    """The whole point of the service is a bookmark that survives a reboot,
    and a regenerated token breaks exactly that. Idempotence here is a
    feature, not an optimisation."""
    conf = tmp_path / "etc"
    conf.mkdir()
    (conf / "token").write_text("existing-token\n", encoding="utf-8")
    result = _run(tmp_path, "--dry-run", *_sandbox(tmp_path))
    assert "keeping the existing token" in result.stdout
    assert "existing-token" not in result.stdout, "the value must never be printed"


def test_an_existing_certificate_is_kept_rather_than_reissued(tmp_path: Path) -> None:
    """Reissuing changes the fingerprint, and every phone that had accepted
    the old certificate is sent back to the browser's warning page."""
    tls = tmp_path / "state" / "tls"
    tls.mkdir(parents=True)
    (tls / "field-app.crt").write_text("not a real certificate\n", encoding="utf-8")
    result = _run(tmp_path, "--dry-run", *_sandbox(tmp_path))
    assert "keeping the existing certificate" in result.stdout


def test_the_generated_environment_file_names_the_site_profile_it_was_given(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, "--dry-run", *_sandbox(tmp_path))
    assert f"FIELD_SITE={tmp_path / 'site.yaml'}" in result.stdout


def test_the_token_is_generated_as_hex_rather_than_base64() -> None:
    """base64 emits +, / and =, none of which survive a URL query string
    unescaped -- and the token is delivered as ?token=..., so the app refuses
    such a value outright. Pinned here because the mistake is easy to make and
    only shows up at the first field start."""
    source = INSTALLER.read_text(encoding="utf-8")
    assert "openssl rand -hex" in source
    assert "openssl rand -base64" not in source


def test_the_installer_follows_the_repository_shell_conventions() -> None:
    assert os.access(INSTALLER, os.X_OK)
    lines = INSTALLER.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in lines


# -- capture settings the installer can set -------------------------------


@pytest.mark.parametrize(
    ("flag", "value", "variable"),
    [
        ("--band", "/etc/dmr-field/bands/p25_868_smoke.yaml", "FIELD_BAND"),
        ("--center-frequency", "868200000", "FIELD_CENTER_FREQUENCY"),
        ("--sample-rate", "768000", "FIELD_SAMPLE_RATE"),
        ("--duration", "30", "FIELD_DURATION"),
        ("--driver", "sdrplay", "FIELD_DRIVER"),
        ("--solve-resolution-m", "250", "FIELD_SOLVE_RESOLUTION_M"),
    ],
)
def test_each_capture_setting_can_be_chosen_at_install_time(
    tmp_path: Path, flag: str, value: str, variable: str
) -> None:
    """The values belong to a campaign and to the storage measured by
    preflight, not to the repository, so they are given here rather than
    baked into the example."""
    result = _run(tmp_path, "--dry-run", *_sandbox(tmp_path), flag, value)
    assert result.returncode == 0, result.stderr
    assert f"{variable}={value}" in result.stdout


def test_an_omitted_capture_setting_keeps_the_documented_value(
    tmp_path: Path,
) -> None:
    """Blanking it instead would put the service back on the CLI's implicit
    defaults, which is the failure these options exist to prevent."""
    result = _run(tmp_path, "--dry-run", *_sandbox(tmp_path), "--sample-rate", "768000")
    assert "FIELD_SAMPLE_RATE=768000" in result.stdout
    assert "FIELD_DURATION=90" in result.stdout, "unset settings keep the example's value"
    assert "FIELD_DURATION=\n" not in result.stdout


def test_gain_and_lna_are_not_installer_options(tmp_path: Path) -> None:
    """They come from the site profile; a second place to set them would be a
    second thing to keep in step."""
    result = _run(tmp_path, "--dry-run", *_sandbox(tmp_path),
                  "--if-gain-reduction", "25")
    assert result.returncode != 0
    assert "unknown argument" in result.stderr


# -- the certificate must follow --state-dir ------------------------------


def _embedded_python_program() -> str:
    """The heredoc the installer feeds to the deployment venv's interpreter."""
    source = INSTALLER.read_text(encoding="utf-8")
    # Everything after the newline that ends the invocation line, up to the
    # closing delimiter -- the rest of that line is shell, not Python.
    after_delimiter = source.split("<<'PYEOF'", 1)[1]
    return after_delimiter.split("\n", 1)[1].split("\nPYEOF", 1)[0]


def test_the_certificate_is_issued_into_the_configured_state_directory(
    tmp_path: Path,
) -> None:
    """The dry run has to name the directory the real run would use. This one
    was already correct before the fix -- the three tests below are what
    distinguish the broken real path from the repaired one -- but it is what
    an operator reads before committing to an install, so it is pinned too."""
    result = _run(tmp_path, "--dry-run", *_sandbox(tmp_path))
    assert f"{tmp_path / 'state' / 'tls'}" in result.stdout
    assert "/var/lib/dmr-field/tls" not in result.stdout


def test_the_installer_passes_the_state_directory_to_the_interpreter() -> None:
    """The old implementation ran `python -` with no argument at all, so no
    amount of correctness inside the program could have saved it."""
    source = INSTALLER.read_text(encoding="utf-8")
    assert '"$VENV_DIR/bin/python" - "${STATE_DIR}/tls" <<' in source


def test_the_embedded_program_has_no_hardcoded_certificate_directory() -> None:
    source = _embedded_python_program()
    assert "/var/lib/dmr-field/tls" not in source
    assert "sys.argv[1]" in source


def test_the_embedded_program_refuses_to_guess_when_given_no_directory(
    tmp_path: Path,
) -> None:
    """Behavioural, and the sharpest distinction between the old code and the
    new: run the program the installer actually embeds, with no argument. The
    old one silently issued into /var/lib/dmr-field/tls; this one exits."""
    program = tmp_path / "issue_cert.py"
    program.write_text(_embedded_python_program(), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(program)],
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT, check=False,
    )
    assert result.returncode != 0
    assert "TLS directory was not passed" in (result.stdout + result.stderr)


# -- project and collection round -----------------------------------------


def test_an_existing_environment_file_is_kept_rather_than_re_rendered(
    tmp_path: Path,
) -> None:
    """The promise an update makes to a Pi already in the field. Re-rendering
    would discard whatever the operator edited by hand -- and after this
    change that includes FIELD_PROJECT and FIELD_CAMPAIGN, which the
    installer has no way to guess."""
    conf = tmp_path / "etc"
    conf.mkdir()
    existing = conf / "field.env"
    existing.write_text(
        "FIELD_BAND=hand_edited\nFIELD_PROJECT=/etc/dmr-field/projects/p25/project.yaml\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--dry-run", *_sandbox(tmp_path))

    assert "keeping the existing" in result.stdout
    assert existing.read_text(encoding="utf-8").startswith("FIELD_BAND=hand_edited")


def test_the_example_ships_both_variables_empty(tmp_path: Path) -> None:
    """Empty and uncommented, not commented out: the installer's own `sed`
    anchors on `^FIELD_X=`, so a commented key could never be substituted."""
    result = _run(tmp_path, "--dry-run", *_sandbox(tmp_path))

    assert "FIELD_PROJECT=" in result.stdout
    assert "FIELD_CAMPAIGN=" in result.stdout
    assert "#FIELD_PROJECT" not in result.stdout


def test_a_project_and_campaign_given_to_the_installer_are_written(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        "--dry-run",
        *_sandbox(tmp_path),
        "--project", "/etc/dmr-field/projects/p25/project.yaml",
        "--campaign", "2026-09_day1",
    )

    assert "FIELD_PROJECT=/etc/dmr-field/projects/p25/project.yaml" in result.stdout
    assert "FIELD_CAMPAIGN=2026-09_day1" in result.stdout


def test_omitting_them_leaves_the_example_empty_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """A project deployment is opt-in. An installer that invented one would
    bind the service to a database that may not exist."""
    result = _run(tmp_path, "--dry-run", *_sandbox(tmp_path))

    lines = [line.strip() for line in result.stdout.splitlines()]
    assert "FIELD_PROJECT=" in lines
    assert "FIELD_CAMPAIGN=" in lines
