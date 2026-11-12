"""`fieldctl auth`: turning the API token off and on, on purpose and only where safe.

Run through the real script with the same stubs as `test_fieldctl_campaign.py`:
nothing real is reached. What is pinned: it reports by default and changes
only with --write and as root; it refuses to turn the token off unless the
service listens on Tailscale and Tailscale Funnel does not put this app on
the internet (or when that cannot be checked, foreground sessions included);
a Funnel for *other* ports, or Serve that stays on the tailnet, does not
count; it never restarts blind over a job, never starts a stopped service,
and leaves one that is still starting alone; the switch is verified by the
status codes of the mode asked for, and the previous setting comes back if
they are wrong; a failed backup changes nothing; and a start with no token
re-checks the Funnel every time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_fieldctl_campaign import TOKEN_VALUE, Deployment

PORT = "8765"


def _serve_status(pi: Deployment, document: dict | None, *, fails: bool = False) -> None:
    """What `tailscale serve status --json` answers on this pretend Pi."""
    body = json.dumps(document) if document is not None else ""
    (pi.root / "serve.json").write_text(body, encoding="utf-8")
    answer = "exit 1" if fails else f'cat "{pi.root}/serve.json"; exit 0'
    (pi.bin / "tailscale").write_text(
        f'''#!/bin/sh
echo "$@" >> "{pi.root}/tailscale.called"
if [ "$1 $2" = "serve status" ]; then
  {answer}
fi
exit 0
''',
        encoding="utf-8",
    )
    (pi.bin / "tailscale").chmod(0o755)


def _answering(pi: Deployment, mode: str | None) -> None:
    """How the pretend service answers `curl --write-out %{http_code}`:
    `none` is 200 to anyone, `token` is 401 without the token and 200 with
    it, None is nothing answering at all."""
    path = pi.root / "api.auth"
    if mode is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(mode, encoding="utf-8")


def _install_curl(pi: Deployment) -> None:
    """The campaign suite's curl stub, plus status codes for --write-out."""
    (pi.bin / "curl").write_text(
        f'''#!/bin/sh
echo "$@" >> "{pi.root}/curl.called"
code=0; token=0
for argument in "$@"; do
  case "$argument" in
    --write-out) code=1 ;;
    --config) token=1 ;;
  esac
done
if [ $code = 1 ]; then
  mode=$(cat "{pi.root}/api.auth" 2>/dev/null) || {{ printf 000; exit 7; }}
  if [ "$mode" = none ] || [ $token = 1 ]; then printf 200; else printf 401; fi
  exit 0
fi
for argument in "$@"; do
  case "$argument" in
    */api/jobs)  cat "{pi.root}/api.jobs" 2>/dev/null || exit 7; exit 0 ;;
    */api/state) cat "{pi.root}/api.state" 2>/dev/null || exit 7; exit 0 ;;
  esac
done
exit 7
''',
        encoding="utf-8",
    )
    (pi.bin / "curl").chmod(0o755)


# The owner's Pi on 2026-11-12: port 8080 served to the tailnet only.
TAILNET_ONLY_8080 = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {"raspberrypi-1.tail17e2fe.ts.net:443": {
        "Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}},
}
FUNNEL_8080 = {**TAILNET_ONLY_8080, "AllowFunnel": {"raspberrypi-1.tail17e2fe.ts.net:443": True}}
FUNNEL_THIS_APP = {
    "TCP": {"8443": {"HTTPS": True}},
    "Web": {"raspberrypi-1.tail17e2fe.ts.net:8443": {
        "Handlers": {"/": {"Proxy": f"https+insecure://100.90.110.54:{PORT}"}}}},
    "AllowFunnel": {"raspberrypi-1.tail17e2fe.ts.net:8443": True},
}


@pytest.fixture()
def pi(tmp_path: Path) -> Deployment:
    deployment = Deployment(tmp_path)
    _install_curl(deployment)
    deployment.campaign("day1", label="Day one")
    deployment.set_local("FIELD_CAMPAIGN=day1\n# the operator's note\n")
    _serve_status(deployment, TAILNET_ONLY_8080)
    deployment.set_service("active")
    deployment.set_api_campaign("day1")
    _answering(deployment, "token")
    return deployment


# -- reporting ------------------------------------------------------------------


def test_status_reports_the_token_and_what_funnel_exposes(pi: Deployment) -> None:
    result = pi.run("auth", "status")
    assert result.returncode == 0, result.stderr
    assert "auth     token" in result.stdout
    assert "funnel" not in result.stdout, "tailnet-only Serve is not exposure"

    _serve_status(pi, FUNNEL_8080)
    shown = pi.run("auth", "status").stdout
    assert "on for other ports only" in shown and "not exposed" in shown


def test_off_without_write_reports_and_changes_nothing(pi: Deployment) -> None:
    pi.forget_calls()
    result = pi.run("auth", "off")
    assert result.returncode == 0, result.stderr
    assert "sudo fieldctl auth off --write" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()
    assert not any("restart" in call for call in pi.calls("systemctl"))


def test_off_with_write_needs_root(pi: Deployment) -> None:
    pi.set_uid(1000)
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "sudo fieldctl auth off --write" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()


# -- switching off --------------------------------------------------------------


def test_off_with_write_sets_the_one_line_restarts_and_verifies_without_a_token(
    pi: Deployment,
) -> None:
    pi.forget_calls()
    _answering(pi, "none")
    result = pi.run("auth", "off", "--write")
    assert result.returncode == 0, result.stderr
    text = pi.local_file.read_text()
    assert "FIELD_AUTH=none" in text
    assert "FIELD_CAMPAIGN=day1" in text and "# the operator's note" in text
    assert any(call.startswith("restart") for call in pi.calls("systemctl"))
    # Verified by the status code of an unauthenticated request, not by
    # "something answered".
    state_calls = [call for call in pi.calls("curl") if "/api/state" in call]
    assert state_calls and "--config" not in state_calls[-1]
    assert "--write-out" in state_calls[-1]


def test_off_is_refused_when_funnel_puts_this_app_on_the_internet(pi: Deployment) -> None:
    _serve_status(pi, FUNNEL_THIS_APP)
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "Funnel puts port 8765 on the internet" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()


def test_a_foreground_funnel_session_counts(pi: Deployment) -> None:
    """`tailscale funnel` without --bg is stored under Foreground.<session>."""
    _serve_status(pi, {"Foreground": {"session-1": FUNNEL_THIS_APP}})
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "Funnel puts port 8765 on the internet" in result.stderr


def test_a_funnel_for_another_port_does_not_block_it(pi: Deployment) -> None:
    _serve_status(pi, FUNNEL_8080)
    _answering(pi, "none")
    result = pi.run("auth", "off", "--write")
    assert result.returncode == 0, result.stderr
    assert "FIELD_AUTH=none" in pi.local_file.read_text()


def test_off_is_refused_when_the_funnel_state_cannot_be_read(pi: Deployment) -> None:
    _serve_status(pi, None, fails=True)
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "could not read" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()


def test_off_is_refused_on_an_address_that_is_not_tailscale(pi: Deployment) -> None:
    pi.set_local("FIELD_CAMPAIGN=day1\nFIELD_HOST=192.168.1.20\n")
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "not a Tailscale address" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()


# -- never restart blind, never start what was stopped --------------------------------


def test_off_is_refused_while_a_job_runs(pi: Deployment) -> None:
    pi.set_jobs([{"job_id": "abc", "kind": "live", "status": "running"}])
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "a job has not finished" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()


def test_an_api_that_cannot_be_asked_about_jobs_is_a_refusal(pi: Deployment) -> None:
    pi.unreachable_api()
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "Refusing to restart it blind" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()


def test_a_stopped_service_stays_stopped(pi: Deployment) -> None:
    pi.set_service("inactive")
    pi.forget_calls()
    result = pi.run("auth", "off", "--write")
    assert result.returncode == 0, result.stderr
    assert "FIELD_AUTH=none" in pi.local_file.read_text()
    assert not any(call.startswith(("restart", "start")) for call in pi.calls("systemctl"))
    assert "next start" in result.stderr


def test_a_service_that_is_starting_up_is_left_alone(pi: Deployment) -> None:
    pi.set_service("activating")
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "Wait for it to settle" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()


# -- verification and rollback ---------------------------------------------------------


def test_the_previous_setting_comes_back_if_the_service_does_not_answer(pi: Deployment) -> None:
    _answering(pi, None)
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "previous setting was restored" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()
    assert "FIELD_CAMPAIGN=day1" in pi.local_file.read_text()


def test_an_answer_in_the_wrong_mode_is_not_success(pi: Deployment) -> None:
    """Asked to turn the token on, a service that still answers without one
    has not done it -- whatever it answers."""
    pi.set_local("FIELD_CAMPAIGN=day1\nFIELD_AUTH=none\n")
    _answering(pi, "none")
    result = pi.run("auth", "on", "--write")
    assert result.returncode != 0
    assert "previous setting was restored" in result.stderr
    assert "FIELD_AUTH=none" in pi.local_file.read_text()


def test_a_failed_backup_changes_nothing_and_deletes_nothing(pi: Deployment) -> None:
    """A failed backup read as "there was no file" made a rollback delete the
    operator's whole local configuration."""
    # Readable by the shell, but not as UTF-8 text, which the backup needs.
    original = b"FIELD_CAMPAIGN=day1\n# caf\xe9 note\n"
    pi.local_file.write_bytes(original)
    _answering(pi, None)   # had it got as far as a restart, it would roll back
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "could not back up" in result.stderr
    assert pi.local_file.read_bytes() == original


# -- running without a token -----------------------------------------------------------


def test_with_the_token_off_the_command_and_the_bookmark_carry_no_token(pi: Deployment) -> None:
    pi.set_local("FIELD_CAMPAIGN=day1\nFIELD_AUTH=none\n")
    command = pi.run("print-command").stdout.splitlines()
    assert "--no-auth" in command and "--token-file" not in command
    url = pi.run("url", "--locate")
    assert url.returncode == 0, url.stderr
    assert url.stdout.strip() == "https://100.90.110.54:8765/locate"
    assert TOKEN_VALUE not in url.stdout + url.stderr
    assert "auth     OFF" in pi.run("auth", "status").stdout


def test_a_start_without_a_token_rechecks_the_funnel_every_time(pi: Deployment) -> None:
    """Funnel turned on after the token was turned off must not put a
    token-less receiver on the internet: the start itself refuses."""
    pi.set_local("FIELD_CAMPAIGN=day1\nFIELD_AUTH=none\n")
    _serve_status(pi, FUNNEL_THIS_APP)
    result = pi.run("exec")
    assert result.returncode != 0
    assert "refusing to start without a token" in result.stderr


# -- switching on ------------------------------------------------------------------------


def test_on_with_write_puts_the_token_back(pi: Deployment) -> None:
    pi.set_local("FIELD_CAMPAIGN=day1\nFIELD_AUTH=none\n")
    _answering(pi, "token")
    result = pi.run("auth", "on", "--write")
    assert result.returncode == 0, result.stderr
    assert "FIELD_AUTH=token" in pi.local_file.read_text()
    assert "--token-file" in pi.run("print-command").stdout.splitlines()


def test_on_is_refused_when_the_token_file_is_empty(pi: Deployment) -> None:
    pi.set_local("FIELD_CAMPAIGN=day1\nFIELD_AUTH=none\n")
    (pi.root / "token").write_text("", encoding="utf-8")
    result = pi.run("auth", "on", "--write")
    assert result.returncode != 0
    assert "missing or empty" in result.stderr
    assert "FIELD_AUTH=none" in pi.local_file.read_text()


def test_an_unknown_auth_value_is_refused_rather_than_guessed(pi: Deployment) -> None:
    pi.set_local("FIELD_CAMPAIGN=day1\nFIELD_AUTH=off\n")
    result = pi.run("print-command")
    assert result.returncode != 0
    assert "FIELD_AUTH must be 'token' or 'none'" in result.stderr
