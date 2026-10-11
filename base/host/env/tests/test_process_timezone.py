"""Explicit startup timezone delivery stays independent of Settings imports."""

from __future__ import annotations

import json
import os
import sys

import pytest

from base.host.proc import run_bounded


@pytest.mark.parametrize("name", [None, "Not/AZone", "", "Asia/Shanghai"])
def test_process_timezone_uses_only_explicit_name(name: str | None) -> None:
    result = run_bounded(
        [
            sys.executable,
            "-c",
            """
import json
import os
import sys
from base.host.env.dotenv_boot import apply_process_timezone

assert "base.config" not in sys.modules
name = json.loads(sys.argv[1])
apply_process_timezone(name)
expected = "Asia/Shanghai" if name == "Asia/Shanghai" else "UTC"
assert os.environ["TZ"] == expected
assert "base.config" not in sys.modules
""",
            json.dumps(name),
        ],
        env={**os.environ, "TZ": "UTC", "AVA_TIMEZONE": "Europe/London"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
