"""Contract: the host converge step wires the bare ava launcher script, scripts/ava-launcher.sh."""

from __future__ import annotations

from pathlib import Path

from cli.commands.converge import host as converge_host

_REPO_ROOT = Path(__file__).resolve().parents[3]


_CLI_LINK_STEPS = tuple(step for step in converge_host.CONVERGE_STEPS if step.name == "ava on PATH")


def test_one_host_global_step_wires_the_cli() -> None:
    (step,) = _CLI_LINK_STEPS
    assert step.host_global
    assert not any("CLI link" in s.name for s in converge_host.CONVERGE_STEPS)
    assert not (_REPO_ROOT / "scripts" / "ava-launcher.sh").exists()
