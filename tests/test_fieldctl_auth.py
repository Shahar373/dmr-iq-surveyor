"""`fieldctl auth`: turning the API token off and on, on purpose and only where safe.

Run through the real script with the same stubs as `test_fieldctl_campaign.py`:
nothing real is reached. What is pinned: it reports by default and changes
only with --write and as root; it refuses to turn the token off unless the
service listens on Tailscale and Tailscale Funnel does not put this app on
the internet (or when that cannot be checked); a Funnel for *other* ports, or
Serve that stays on the tailnet, does not count; the service is restarted
and verified, and the previous setting comes back if it does not answer;
and a start with no token re-checks the Funnel every time.
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
    (pi.bin / "tailscale").write_text(
        f'''#!/bin/sh
echo "$@" >> "{pi.root}/tailscale.called"
if [ "$1 $2" = "serve status" ]; then
  {"exit 1" if fails else f'cat "{pi.root}/serve.json"; exit 0'}
fi
exit 0
''',
        encoding="utf-8",
    )
    (pi.bin / "tailscale").chmod(0o755)


# The owner's Pi on 2026-11-12: port 8080 served to the tailnet only.
TAILNET_ONLY_8080 = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {"raspberrypi-1.tail17e2fe.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}},
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
    deployment.campaign("day1", label="Day one")
    deployment.set_local("FIELD_CAMPAIGN=day1\n# the operator's note\n")
    _serve_status(deployment, TAILNET_ONLY_8080)
    deployment.set_service("active")
    deployment.set_api_campaign("day1")
    return deployment


def test_status_reports_the_token_and_what_funnel_exposes(pi: Deployment) -> None:
    result = pi.run("auth", "status")
    assert result.returncode == 0, result.stderr
    assert "auth     token" in result.stdout
    assert "funnel" not in result.stdout, "tailnet-only Serve is not exposure"

    _serve_status(pi, FUNNEL_8080)
    shown = pi.run("auth", "status").stdout
    assert "on for other ports only" in shown and "not exposed" in shown


def test_off_without_write_reports_and_changes_nothing(pi: Deployment) -> None:
    result = pi.run("auth", "off")
    assert result.returncode == 0, result.stderr
    assert "sudo fieldctl auth off --write" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()
    assert pi.calls("systemctl") == [] or all("restart" not in c for c in pi.calls("systemctl"))


def test_off_with_write_needs_root(pi: Deployment) -> None:
    pi.set_uid(1000)
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "sudo fieldctl auth off --write" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()


def test_off_with_write_sets_the_one_line_restarts_and_verifies_without_a_token(
    pi: Deployment,
) -> None:
    pi.forget_calls()
    result = pi.run("auth", "off", "--write")
    assert result.returncode == 0, result.stderr
    text = pi.local_file.read_text()
    assert "FIELD_AUTH=none" in text
    assert "FIELD_CAMPAIGN=day1" in text and "# the operator's note" in text
    assert any(call.startswith("restart") for call in pi.calls("systemctl"))
    # The check after the restart asked the API with no token at all.
    state_calls = [call for call in pi.calls("curl") if "/api/state" in call]
    assert state_calls and "--config" not in state_calls[-1]


def test_off_is_refused_when_funnel_puts_this_app_on_the_internet(pi: Deployment) -> None:
    _serve_status(pi, FUNNEL_THIS_APP)
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "Funnel puts port 8765 on the internet" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()


def test_a_funnel_for_another_port_does_not_block_it(pi: Deployment) -> None:
    _serve_status(pi, FUNNEL_8080)
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


def test_off_is_refused_while_a_job_runs(pi: Deployment) -> None:
    pi.set_jobs([{"job_id": "abc", "kind": "live", "status": "running"}])
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "a job is running" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()


def test_the_previous_setting_comes_back_if_the_service_does_not_answer(pi: Deployment) -> None:
    pi.unreachable_api()
    pi.set_service("inactive")
    result = pi.run("auth", "off", "--write")
    assert result.returncode != 0
    assert "previous setting was restored" in result.stderr
    assert "FIELD_AUTH" not in pi.local_file.read_text()
    assert "FIELD_CAMPAIGN=day1" in pi.local_file.read_text()


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


def test_on_with_write_puts_the_token_back(pi: Deployment) -> None:
    pi.set_local("FIELD_CAMPAIGN=day1\nFIELD_AUTH=none\n")
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
