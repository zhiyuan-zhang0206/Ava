"""Ordinary Python shutdown reports refresh failures without an exit-code guarantee."""

import os
import subprocess
import sys
from pathlib import Path

_CHILD = Path(__file__).with_name("child_sampling_atexit.py")


def test_installation_atexit_reports_failure_without_promising_a_nonzero_exit() -> None:
    result = subprocess.run(  # noqa: S603 — fixed interpreter and child script
        [sys.executable, str(_CHILD)],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Exception ignored in atexit callback" in result.stderr
    assert "ValueError: ordinary SDK refresh defect" in result.stderr
