"""Service entry cold imports retain their ordinary HTTPX dependency.

Each probe uses the process profile supplied by the roster and a fresh home.
ENTRIES names service entries that reach HTTPX while importing their modules.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# name -> (modules the entry imports, AVA_PROCESS_PROFILE the roster sets)
ENTRIES: dict[str, tuple[tuple[str, ...], str]] = {
    "heartbeat": (("services.wake.heartbeat.daemon",), "gateway"),
    "memory-indexer": (("services.derived.memory_indexer.daemon",), "gateway"),
    "memory-search": (("services.derived.memory_search.daemon",), "gateway"),
    "gateway": (("gateway.cluster.server", "gateway.app"), "gateway"),
    "ops": (("services.agent_runner.agent_ops.daemon",), "runner"),
    "browser-mcp": (("services.desktop.browser.mcp_daemon",), "runner"),
    "agent-host": (("services.agent_runner.agent_host.daemon",), "agent"),
}

_PROBE = """
import json
import sys

sys.path.insert(0, {repo!r})
for module in {modules!r}:
    __import__(module)
print(json.dumps({{"httpx_loaded": "httpx" in sys.modules}}))
"""


@pytest.mark.parametrize("entry", list(ENTRIES))
def test_service_entry_imports_httpx_in_a_fresh_process(entry: str, tmp_path: Path) -> None:
    modules, profile = ENTRIES[entry]
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.update(
        AVA_HOME=str(home),
        AVA_CONFIG_FETCH="skip",
        AVA_PROCESS_PROFILE=profile,
    )
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-X", "utf8", "-c", _PROBE.format(repo=str(_REPO_ROOT), modules=modules)],
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
