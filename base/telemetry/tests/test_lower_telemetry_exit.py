"""Real process exit hooks keep the cold path cold and stop loaded owners."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CHILD = Path(__file__).with_name("_lower_exit_child.py")


def _child(scenario: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MEIVA_HOME"] = str(home)
    return subprocess.run(  # noqa: S603 — fixed interpreter and test-owned child script
        [sys.executable, "-I", str(CHILD), scenario],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def test_cold_exit_does_not_import_or_start_lower_exporters(tmp_path: Path) -> None:
    child = _child("cold", tmp_path / "cold-home")
    assert child.returncode == 0, child.stderr
    assert child.stdout == "cold exit observed\n", child.stderr
    assert "Exception ignored in atexit callback" not in child.stderr


def test_loaded_trace_owner_is_stopped_by_its_registered_exit_hook(tmp_path: Path) -> None:
    child = _child("loaded", tmp_path / "hot-home")
    assert child.returncode == 0, child.stderr
    assert child.stdout == "loaded trace exit observed\n", child.stderr
    assert "Exception ignored in atexit callback" not in child.stderr
