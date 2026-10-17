"""Ordinary Python shutdown reports refresh failures without an exit-code guarantee."""

import sys
from pathlib import Path

from base.host.proc import run_bounded

_CHILD = Path(__file__).with_name("child_sampling_atexit.py")
_UNINSTALLED_CHILD = Path(__file__).with_name("child_sampling_atexit_uninstalled.py")


def test_installation_atexit_reports_failure_without_promising_a_nonzero_exit() -> None:
    result = run_bounded(
        [sys.executable, str(_CHILD)],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stderr
    assert "Exception ignored in atexit callback" in result.stderr
    assert "ValueError: ordinary SDK refresh defect" in result.stderr


def test_uninstalled_surface_leaves_no_atexit_callback() -> None:
    result = run_bounded(
        [sys.executable, str(_UNINSTALLED_CHILD)],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "uninstalled"
    assert "Exception ignored in atexit callback" not in result.stderr
