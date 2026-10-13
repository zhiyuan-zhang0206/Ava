"""A fresh process admits a registered model through the explicitly built catalog."""

import subprocess
import sys
from pathlib import Path


def test_validate_overlay_is_self_sufficient_in_a_fresh_process() -> None:
    """A fresh process admits a registered model through its explicit catalog."""
    result = subprocess.run(  # noqa: S603 — our own venv python + a fixed child script
        [sys.executable, str(Path(__file__).with_name("_overlay_child.py"))],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
