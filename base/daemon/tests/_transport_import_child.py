"""Imports the daemon HTTP transport with configuration owners denied; never imported by a production entry.

Run as ``python -I _transport_import_child.py <repo-root>``.
"""

from __future__ import annotations

import importlib.abc
import sys


def emit(text: object) -> None:
    sys.stdout.write(f"{text}\n")


class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, *args: object) -> None:
        if fullname.startswith(
            (
                "base.config",
                "base.host.env.bootstrap",
                "plugins",
                "services.agent_runner.agent_ops.daemon",
            )
        ):
            raise AssertionError(fullname)


sys.path.insert(0, sys.argv[1])
sys.meta_path.insert(0, Deny())
import base.daemon.http_transport  # noqa: E402,F401  # pyright: ignore[reportUnusedImport]

emit("TRANSPORT_ONLY")
