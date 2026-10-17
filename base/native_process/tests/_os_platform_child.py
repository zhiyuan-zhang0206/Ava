"""Fresh-interpreter probes of ``base.native_process.os_platform``; never imported by a production entry.

Run as ``python [-I] _os_platform_child.py <scenario> [repo-root]``.
"""

from __future__ import annotations

import sys
import time


def cold_import_does_not_probe_uname() -> None:
    import platform
    import uuid  # noqa: F401  # pyright: ignore[reportUnusedImport]

    # Linux's stdlib UUID initialization probes the OS independently of Meiva.
    assert "base.native_process" not in sys.modules
    assert "base.native_process.os_platform" not in sys.modules

    def refuse_probe() -> None:
        raise AssertionError("platform import must not probe uname")

    platform.uname = refuse_probe  # type: ignore[assignment]
    import base.native_process.os_platform  # noqa: F401  # pyright: ignore[reportUnusedImport]


def pipe_stdout_streams_before_exit() -> None:
    sys.path.insert(0, sys.argv[2])
    from base.native_process.os_platform import ensure_line_buffered_stdio

    ensure_line_buffered_stdio()
    sys.stdout.write("[update] header\n")
    time.sleep(30)


SCENARIOS = {
    "cold_import_does_not_probe_uname": cold_import_does_not_probe_uname,
    "pipe_stdout_streams_before_exit": pipe_stdout_streams_before_exit,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
