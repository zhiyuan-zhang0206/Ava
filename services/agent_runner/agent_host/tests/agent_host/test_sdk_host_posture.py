"""The shared agent host's startup refuses a launched-agent SDK posture."""

from __future__ import annotations

import sys
from pathlib import Path

from base.host.proc import run_bounded

_HOST_POSTURE = Path(__file__).with_name("child_sdk_host_posture.py")


def _run_host_posture(scenario: str) -> None:
    result = run_bounded(
        [sys.executable, "-I", str(_HOST_POSTURE), scenario],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_host_startup_refuses_env_context_and_attachment() -> None:
    _run_host_posture("host_refuses_env_context_and_attachment")


def test_executable_host_rejects_inherited_child_identity_before_sdk_import() -> None:
    _run_host_posture("host_rejects_inherited_child_identity")
