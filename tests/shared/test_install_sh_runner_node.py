"""Agent-runner install must provision Node.js for its shared browser service."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_agent_runner_install(
    tmp_path: Path, *, node_exit: int = 0
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
