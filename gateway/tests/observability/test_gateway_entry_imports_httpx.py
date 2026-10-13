"""The gateway entry's cold import retains its ordinary HTTPX dependency.

The probe uses the process profile the roster gives the gateway and a fresh home.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROBE = Path(__file__).with_name("child_gateway_httpx_probe.py")


def test_gateway_entry_imports_httpx_in_a_fresh_process(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.update(AVA_HOME=str(home), AVA_CONFIG_FETCH="skip", AVA_PROCESS_PROFILE="gateway")
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-X", "utf8", str(_PROBE)],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])

    assert report["httpx_loaded"], (
        "the gateway entry no longer imports httpx at boot: delete "
        "gateway/tests/observability/test_gateway_entry_imports_httpx.py"
    )
