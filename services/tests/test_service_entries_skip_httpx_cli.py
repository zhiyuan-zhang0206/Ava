"""Each service entry imports `base.host.net` (the httpx CLI guard) before its first `import httpx`.

`block_httpx_cli()` (`base/host/net/httpx_cli_guard.py`) works only when it runs
before the process's first `import httpx`. Its single call is in
`base/host/net/__init__.py`, so what must hold is an import order: the guard's
package is imported before any code, ours or a third-party library's, reaches
httpx. A new `import httpx` that sorts ahead of the first-party imports of an
entry (ruff puts third-party imports first) breaks that order without breaking
anything else, and the process just keeps 2.7 to 4.2 MiB it does not need. This
test imports each entry in a fresh process, the way `python -m <entry>` does, and
fails with the import chain that reached httpx first.

ENTRIES is closed: the service entries whose boot imports reach httpx. A service
that gains an httpx import at boot belongs here; one that loses it must leave
(the test fails when an entry never loads httpx, so the list cannot go stale).
The profile is the `AVA_PROCESS_PROFILE` the roster launches the service with
(`ops/roster/service_spec.py: profile_marker`), because the import itself differs
by profile.
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

# Records, at the first import of httpx, the repo frames that asked for it.
_PROBE = """
import importlib.abc
import json
import sys
import traceback

sys.path.insert(0, {repo!r})
first_importer: list[str] = []


class _Spy(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "httpx" and not first_importer:
            first_importer.extend(
                f"{{frame.filename[len({repo!r}) + 1:]}}:{{frame.lineno}}"
                for frame in traceback.extract_stack()
                if frame.filename.startswith({repo!r}) and "/.venv/" not in frame.filename
            )
        return None


sys.meta_path.insert(0, _Spy())
for module in {modules!r}:
    __import__(module)
print(json.dumps({{
    "httpx_loaded": "httpx" in sys.modules,
    "cli_blocked": "httpx._main" in sys.modules and sys.modules["httpx._main"] is None,
    "first_importer": first_importer,
}}))
"""


@pytest.mark.parametrize("entry", list(ENTRIES))
def test_entry_imports_the_guard_before_httpx(entry: str, tmp_path: Path) -> None:
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
        "services/tests/test_service_entries_skip_httpx_cli.py"
    )
    chain = " <- ".join(reversed(report["first_importer"]))
    assert report["cli_blocked"], (
        f"{entry} imported httpx before base.host.net ran the httpx CLI guard, so it carries "
        f"click, pygments and rich it never uses. First repo frames that reached httpx: {chain}. "
        "Import base.host.net (or anything that imports it, such as base.config) earlier, or "
        "make that httpx import lazy"
    )
