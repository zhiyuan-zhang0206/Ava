"""Agent-runner install must provision Node.js for its shared browser service."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_agent_runner_install(
    tmp_path: Path, *, node_exit: int = 0, os_name: str | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run a scratch agent-runner install with a controllable Node provisioner."""
    home = tmp_path / "home"
    checkout = home / "source"
    provision = checkout / "scripts" / "provision"
    provision.mkdir(parents=True)
    shutil.copy(_REPO_ROOT / "scripts" / "install.sh", checkout / "scripts" / "install.sh")

    calls = tmp_path / "calls.log"
    node = provision / "node.sh"
    node.write_text(f'#!/bin/sh\necho "node" >> "{calls}"\nexit {node_exit}\n')
    node.chmod(0o755)
    toolchain = provision / "toolchain.sh"
    toolchain.write_text(f'#!/bin/sh\necho "toolchain" >> "{calls}"\n')
    toolchain.chmod(0o755)

    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    uv = stub_bin / "uv"
    uv.write_text(f'#!/bin/sh\necho "uv $*" >> "{calls}"\n')
    uv.chmod(0o755)
    if os_name is not None:
        uname = stub_bin / "uname"
        uname.write_text(f"#!/bin/sh\necho {os_name}\n")
        uname.chmod(0o755)
    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\necho "birth $*" >> "{calls}"\n')
    python.chmod(0o755)

    proc = subprocess.run(
        ["bash", "scripts/install.sh", "--role", "agent-runner"],
        cwd=checkout,
        env={
            "AVA_HOME": str(home),
            "HOME": str(home),
            "PATH": f"{stub_bin}:/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    recorded = calls.read_text().splitlines() if calls.exists() else []
    return proc, recorded


def test_agent_runner_install_provisions_node_before_cluster_birth(tmp_path: Path) -> None:
    """A runner install drives the shared Node provisioner before its birth step.

    Removing the provisioner call leaves a runner permanently unable to launch
    ava-browser when its host starts without npx, even though install completes.
    """
    proc, recorded = _run_agent_runner_install(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "node" in recorded
    assert any(line.startswith("birth -m cli.install_cluster") for line in recorded)


def test_agent_runner_install_warns_and_completes_when_node_provisioning_fails(
    tmp_path: Path,
) -> None:
    """A failed best-effort install still reaches birth and names the npx repair.

    Leaving the provisioner as a bare command under install.sh's `set -e` would
    abort before the warning, returning the silent-skip problem as a failed
    install instead of an actionable operator outcome.
    """
    proc, recorded = _run_agent_runner_install(tmp_path, node_exit=1)
    assert proc.returncode == 0, proc.stderr
    assert "node" in recorded
    assert any(line.startswith("birth -m cli.install_cluster") for line in recorded)
    assert "WARNING ava-browser needs Node.js (npx)" in proc.stderr


def test_agent_runner_install_gives_a_macos_command_that_puts_npx_on_path(
    tmp_path: Path,
) -> None:
    """A failed macOS provisioner points to a linked Node formula, not keg-only Node 22."""
    proc, _recorded = _run_agent_runner_install(tmp_path, node_exit=1, os_name="Darwin")
    assert proc.returncode == 0, proc.stderr
    assert (
        "Run `brew install node` (or `brew install node@22 && brew link --force node@22`)"
        in proc.stderr
    )


def test_macos_node_provisioner_force_links_the_keg_only_fallback(tmp_path: Path) -> None:
    """A failed linked-formula install falls back to a force-linked node@22.

    If the macOS provisioner installs keg-only node@22 without linking it, npx
    remains absent from PATH and every later browser repair repeats the same
    ineffective install. This scratch harness executes the real provisioner
    while faking only Homebrew, the platform probe, and the resulting node.
    """
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "brew-calls.log"

    (fake_bin / "uname").write_text("#!/bin/sh\necho Darwin\n")
    (fake_bin / "brew").write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$BREW_CALLS"\n'
        'if [ "$1 $2" = "install node" ]; then exit 1; fi\n'
        'if [ "$1 $2" = "install node@22" ]; then exit 0; fi\n'
        'if [ "$1 $2 $3" = "link --force node@22" ]; then exit 0; fi\n'
        "exit 2\n"
    )
    (fake_bin / "node").write_text("#!/bin/sh\necho v22.0.0\n")
    for command in fake_bin.iterdir():
        command.chmod(0o755)

    proc = subprocess.run(  # noqa: S603 — fixed repository-owned script path
        ["bash", str(_REPO_ROOT / "scripts" / "provision" / "node.sh")],
        env={
            "BREW_CALLS": str(calls),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert calls.read_text().splitlines() == [
        "install node",
        "install node@22",
        "link --force node@22",
    ]
