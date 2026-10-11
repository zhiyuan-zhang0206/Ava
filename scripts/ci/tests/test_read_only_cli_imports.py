"""Read-only CLI startup excludes owner-only operations in a fresh interpreter."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_CHILD = Path(__file__).with_name("child_read_only_cli.py")


@pytest.mark.parametrize("arguments", [["42"], ["42", "--wait"]])
def test_read_only_cli_does_not_load_owner_operations(arguments: list[str]) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and in-repo helper
        [sys.executable, "-I", str(_CHILD), str(Path.cwd()), json.dumps(arguments)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "green" in result.stdout
