"""`base/host/net/httpx_cli_guard.py` — the guard, the httpx layout it depends on, and its failure signals.

The guard rests on a private detail of httpx 0.28.x (`httpx/__init__.py` imports
`httpx._main` inside `try/except ImportError`). Three tests keep that dependency
checkable: the guard still keeps the CLI libraries out while httpx works, httpx
still imports its CLI when unguarded (so the guard is still needed), and the
installed httpx is a version someone reviewed.

Every import probe runs in a fresh interpreter. The pytest process imported httpx
long before this file runs, so an in-process probe would observe a warm module
cache, not a process start.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]

# The module httpx imports for its command line, and the libraries that import drags in.
_CLI_MODULES = ("httpx._main", "click", "pygments", "rich")

_REVIEWED_HTTPX_SERIES = "0.28."


def _probe(body: str) -> dict[str, object]:
    """Run `body` in a fresh interpreter and return the JSON object it prints last."""
    code = f"import json\nimport sys\n\nsys.path.insert(0, {str(_REPO_ROOT)!r})\n{body}"
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-I", "-X", "utf8", "-c", code],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


_REPORT_CLI_MODULES = f"""
loaded = [name for name in {_CLI_MODULES!r} if sys.modules.get(name) is not None]
print(json.dumps({{"loaded": loaded, "blocked": "httpx._main" in sys.modules and sys.modules["httpx._main"] is None}}))
"""


def test_guard_keeps_the_cli_libraries_out_and_httpx_works() -> None:
    report = _probe(
        """
import asyncio

import base.host.net  # the package import runs the guard
import httpx


def _handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"path": request.url.path})


with httpx.Client(transport=httpx.MockTransport(_handler)) as client:
    assert client.get("http://x.test/sync").json() == {"path": "/sync"}


async def _async_get() -> object:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        return (await client.get("http://x.test/async")).json()


assert asyncio.run(_async_get()) == {"path": "/async"}
"""
        + _REPORT_CLI_MODULES
    )
    assert report["loaded"] == [], (
        f"importing httpx after the guard still loaded {report['loaded']}: httpx's CLI layout "
        "changed. Re-read the `try: from ._main import main` block in httpx/__init__.py and "
        "update base/host/net/httpx_cli_guard.py (or delete it)"
    )
    assert report["blocked"] is True, "the guard did not leave `sys.modules['httpx._main'] = None`"


def test_httpx_without_the_guard_still_imports_its_cli() -> None:
    report = _probe("import httpx\n" + _REPORT_CLI_MODULES)
    assert report["loaded"] == list(_CLI_MODULES), (
        f"a plain `import httpx` loaded only {report['loaded']} of {list(_CLI_MODULES)}: httpx no "
        "longer imports its CLI at import time, so base/host/net/httpx_cli_guard.py has nothing "
        "left to do. Delete it, its call in base/host/net/__init__.py, and these tests"
    )


def test_installed_httpx_is_in_the_reviewed_range() -> None:
    installed = version("httpx")
    assert installed.startswith(_REVIEWED_HTTPX_SERIES), (
        f"httpx {installed} is installed but base/host/net/httpx_cli_guard.py was only reviewed "
        f"against httpx {_REVIEWED_HTTPX_SERIES}x. When upgrading httpx, re-check the guard: read "
        "the `try: from ._main import main / except ImportError` block in httpx/__init__.py, "
        "confirm the other tests in this file pass, then update _REVIEWED_HTTPX_SERIES here and "
        "the versions named in the guard's docstring"
    )
