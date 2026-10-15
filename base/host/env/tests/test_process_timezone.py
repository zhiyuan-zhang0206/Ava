from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded

CHILD = Path(__file__).with_name("_process_timezone_child.py")


@pytest.mark.parametrize("name", [None, "Not/AZone", "", "Asia/Shanghai"])
def test_process_timezone_uses_only_explicit_name(name: str | None) -> None:
    result = run_bounded(
        [
            sys.executable,
            str(CHILD),
            json.dumps(name),
        ],
        env={**os.environ, "TZ": "UTC", "AVA_TIMEZONE": "Europe/London"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
