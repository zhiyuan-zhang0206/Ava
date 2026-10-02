"""Keep httpx's command-line client, and the three libraries it drags in, out of a process.

`import httpx` also imports `httpx._main`, the code behind the `httpx` shell
command, whose first lines import `click`, `pygments` and `rich`. No Ava process
runs that command, but every process that imports httpx pays for the three
dependency trees: 5.6 MiB of private memory (USS) in a bare interpreter, and
2.7 to 4.2 MiB in each of the eight service entries that reach httpx at boot.
`block_httpx_cli()` removes that cost.

Measured on macOS arm64, Python 3.12, httpx 0.28.1, warm bytecode cache, one
fresh process per run, median of three (without the guard -> with it):

    import httpx (bare)                21.55 -> 15.95 MiB
    services.heartbeat.daemon          58.08 -> 53.83 MiB
    services.agent_ops.daemon          60.38 -> 56.19 MiB
    services.agent_host.daemon         93.59 -> 89.66 MiB

Why it works, and what it depends on. `httpx/__init__.py` (0.28.x) ends with

    try:
        from ._main import main
    except ImportError:
        def main() -> None: ...   # prints "install httpx[cli]" and exits 1

so that a missing CLI extra never breaks `import httpx`. Python raises
`ImportError` for a module whose `sys.modules` entry is `None`, which is how
this function declines the import without touching site-packages. That is
httpx's private layout (`_main`, the `try/except ImportError` around it), not a
public API. `httpx.main` becomes the stub; nothing in this repo calls it.

How it can stop working, and how each case surfaces:

- httpx stops tolerating the missing CLI (an unguarded `from ._main import
  main`): `import httpx` raises `ImportError` when the process starts. Loud.
- httpx renames or moves the CLI module: the block is a no-op and the three
  libraries load again. Silent in production; caught by
  `test_httpx_cli_guard.py` (the post-guard `sys.modules` assertion).
- httpx stops importing the CLI at import time: the block is dead weight; the
  "without the guard" test in the same file fails and says to delete this module.
- the installed httpx leaves the reviewed 0.28.x range: the version test fails
  with the checklist for re-reading `httpx/__init__.py` (an httpx upgrade needs
  manual approval and must re-check this guard).

It acts only if it runs before the first `import httpx` of the process: the
dict entry never replaces a module that is already loaded. Its single call is
in `base/host/net/__init__.py`, which every service entry imports (through the
Settings load) before anything reaches httpx, including the third-party
libraries that import it (langsmith, langgraph_sdk); the entry-import test in
`services/tests/test_service_entries_skip_httpx_cli.py` is what proves that
order for each service, and fails when a new early `import httpx` breaks it.
"""

from __future__ import annotations

import sys
from typing import cast

_HTTPX_CLI_MODULE = "httpx._main"


def block_httpx_cli() -> None:
    """Make `import httpx` skip `httpx._main` (idempotent; a loaded module is left alone)."""
    cast("dict[str, object]", sys.modules).setdefault(_HTTPX_CLI_MODULE, None)
