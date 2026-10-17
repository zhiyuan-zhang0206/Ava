"""Service entry cold imports retain their ordinary HTTPX dependency.

Each probe uses the process profile supplied by the roster and a fresh home.
ENTRIES names service entries that reach HTTPX while importing their modules. The gateway
entry is probed by `gateway/tests/observability/test_gateway_entry_imports_httpx.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# name -> MEIVA_PROCESS_PROFILE the roster sets; `child_httpx_probe.py` owns the modules each
# entry imports.
ENTRIES: dict[str, str] = {
    "heartbeat": "gateway",
    "memory-indexer": "gateway",
    "memory-search": "gateway",
    "ops": "runner",
    "browser-mcp": "runner",
    "agent-host": "agent",
}
_PROBE = Path(__file__).with_name("child_httpx_probe.py")


@pytest.mark.parametrize("entry", list(ENTRIES))
def test_service_entry_imports_httpx_in_a_fresh_process(entry: str, tmp_path: Path) -> None:
    profile = ENTRIES[entry]
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env.update(
        MEIVA_HOME=str(home),
        MEIVA_CONFIG_FETCH="skip",
        MEIVA_PROCESS_PROFILE=profile,
    )
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-X", "utf8", str(_PROBE), entry],
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
        f"{entry} no longer imports httpx at boot: remove it from ENTRIES in "
        "services/tests/test_service_entries_httpx_import.py"
    )
