"""Service entry cold imports retain their ordinary HTTPX dependency.

Each probe uses the process profile supplied by the roster and a fresh home.
ENTRIES names service entries that reach HTTPX while importing their modules;
_PROBE owns the modules each entry imports.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# name -> AVA_PROCESS_PROFILE the roster sets
ENTRIES: dict[str, str] = {
    "heartbeat": "gateway",
    "memory-indexer": "gateway",
    "memory-search": "gateway",
    "gateway": "gateway",
    "ops": "runner",
    "browser-mcp": "runner",
    "agent-host": "agent",
}

# Literal source with literal imports, so test selection can read each entry's
# modules. argv: the repo root, then the entry name.
_PROBE = """
import json
import sys

sys.path.insert(0, sys.argv[1])
match sys.argv[2]:
    case "heartbeat":
        import services.wake.heartbeat.daemon
    case "memory-indexer":
        import services.derived.memory_indexer.daemon
    case "memory-search":
        import services.derived.memory_search.daemon
    case "gateway":
        import gateway.cluster.server
        import gateway.app
    case "ops":
        import services.agent_runner.agent_ops.daemon
    case "browser-mcp":
        import services.desktop.browser.mcp_daemon
    case "agent-host":
        import services.agent_runner.agent_host.daemon
    case unknown:
        raise SystemExit(f"no probe imports for entry {unknown!r}")
print(json.dumps({"httpx_loaded": "httpx" in sys.modules}))
"""


@pytest.mark.parametrize("entry", list(ENTRIES))
def test_service_entry_imports_httpx_in_a_fresh_process(entry: str, tmp_path: Path) -> None:
    profile = ENTRIES[entry]
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.update(
        AVA_HOME=str(home),
        AVA_CONFIG_FETCH="skip",
        AVA_PROCESS_PROFILE=profile,
    )
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-X", "utf8", "-c", _PROBE, str(_REPO_ROOT), entry],
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
        f"{entry} no longer imports httpx at boot: remove it from ENTRIES and _PROBE in "
        "services/tests/test_service_entries_httpx_import.py"
    )
