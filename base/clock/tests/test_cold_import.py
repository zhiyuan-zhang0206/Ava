from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# meiva:link base/clock/docs/clock.meiva.okf.md
def test_clock_capabilities_stay_cold_until_the_settings_factory_runs(tmp_path: Path) -> None:
    child = Path(__file__).with_name("child_cold_import.py")
    untouched_home = tmp_path / "unadmitted"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-B", str(child)],
        env={**os.environ, "MEIVA_HOME": str(untouched_home), "MEIVA_CONFIG_FETCH": "skip"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not untouched_home.exists()
