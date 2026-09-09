"""`scripts/pi_soapysdr_setup.sh` must honour an explicit VENV.

The script links Debian's system SoapySDR bindings into a virtualenv, because
a virtualenv created without --system-site-packages cannot see them and
`import SoapySDR` then fails exactly where captures run. It was written for
the batch-analysis install, where the virtualenv is this checkout's own
`.venv`, and hardcoded that path.

The deployed field service keeps its virtualenv outside the checkout
(`/opt/dmr-field/venv`), and both `scripts/install_field_service.sh` and
`docs/OPERATIONS.md` tell the operator to run this script with `VENV=` set.
Before the fix that variable was ignored: the script went looking for a
`.venv` beside the code that nothing runs from, and the install stopped.

The tests execute the script's own prologue rather than asserting on its
text, so they check the behaviour the operator gets. They stop before step 1,
so nothing here installs packages, touches apt, or opens an SDR.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = REPO_ROOT / "scripts" / "pi_soapysdr_setup.sh"
INSTALLER = REPO_ROOT / "scripts" / "install_field_service.sh"
OPERATIONS_DOC = REPO_ROOT / "docs" / "OPERATIONS.md"

FIRST_STEP = 'say "1/6'


def _prologue() -> str:
    """Everything the script does before its first real step: the variable
    assignments and helper definitions, and nothing that touches the system."""
    source = SETUP_SCRIPT.read_text(encoding="utf-8")
    head, separator, _ = source.partition(FIRST_STEP)
    assert separator, "the script no longer has a '1/6' step; update this test"
    return head


def _resolved_venv(tmp_path: Path, venv: str | None) -> str:
    """Run the prologue as the script would run it, and report the VENV it
    settled on."""
    checkout = tmp_path / "app"
    (checkout / "scripts").mkdir(parents=True)
    probe = checkout / "scripts" / "prologue.sh"
    # Written where the real script lives so that the BASH_SOURCE-based
    # REPO_ROOT resolves the same way it does in the real thing.
    probe.write_text(_prologue() + '\nprintf "%s\\n" "$VENV"\n', encoding="utf-8")

    environment = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    if venv is not None:
        environment["VENV"] = venv
    result = subprocess.run(
        ["bash", str(probe)],
        env=environment, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_an_explicit_venv_is_used(tmp_path: Path) -> None:
    """The regression. This is the contract the installer and the operations
    guide both tell the operator to use, and the old script ignored it --
    silently, resolving to a .venv inside the checkout instead."""
    assert _resolved_venv(tmp_path, "/opt/dmr-field/venv") == "/opt/dmr-field/venv"


def test_an_explicit_venv_outside_the_checkout_is_not_rewritten(
    tmp_path: Path,
) -> None:
    """A deployment's virtualenv is a sibling of the checkout, not a child."""
    target = tmp_path / "elsewhere" / "venv"
    assert _resolved_venv(tmp_path, str(target)) == str(target)


def test_without_venv_the_existing_behaviour_is_unchanged(tmp_path: Path) -> None:
    """The batch-analysis install passes nothing and must keep getting this
    checkout's own .venv."""
    assert _resolved_venv(tmp_path, None) == str(tmp_path / "app" / ".venv")


def test_an_empty_venv_falls_back_rather_than_resolving_to_nothing(
    tmp_path: Path,
) -> None:
    """`VENV= bash ...` is a plausible slip, and `${VENV:-...}` is chosen over
    `${VENV-...}` so it lands on the default instead of an empty path."""
    assert _resolved_venv(tmp_path, "") == str(tmp_path / "app" / ".venv")


# -- the three places that describe this contract must agree --------------


def test_the_installer_tells_the_operator_to_pass_venv() -> None:
    """The installer refuses to continue when the deployment virtualenv
    cannot import SoapySDR, and the command it prints has to be one that
    actually works."""
    source = INSTALLER.read_text(encoding="utf-8")
    assert "VENV=${VENV_DIR} bash" in source
    assert "pi_soapysdr_setup.sh" in source


def test_the_operations_guide_documents_the_same_contract() -> None:
    text = OPERATIONS_DOC.read_text(encoding="utf-8")
    assert "VENV=/opt/dmr-field/venv bash" in text
    assert "pi_soapysdr_setup.sh" in text


def test_the_script_documents_the_variable_it_now_honours() -> None:
    source = SETUP_SCRIPT.read_text(encoding="utf-8")
    assert 'VENV="${VENV:-$REPO_ROOT/.venv}"' in source
    header = source.split("set -uo pipefail", 1)[0]
    assert "VENV=" in header, "the usage header must show how to point it elsewhere"
