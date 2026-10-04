"""The page-server occupant scan survives a process-table read failure.

`_page_server_occupants` maps live page-server processes by port before every
reconcile pass; one process whose cmdline read raises must be skipped, not
abort the pass (task #4964 — the psutil 7.2.2 macOS cmdline read raised
SystemError mid-iteration and killed the whole round).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import services.agent_runner.page_server.daemon as psd


def test_occupants_skips_a_process_whose_cmdline_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cmdline read that raises skips that process, not the scan: the other
    occupants still report."""

    class _ReadFails:
        """Unreadable through both the attrs shape and the direct read, so a
        regression to `process_iter(attrs)` stays caught."""

        pid = 424242

        @property
        def info(self) -> dict[str, object]:
            raise SystemError("psutil: cmdline read failed mid-iteration")

        def cmdline(self) -> list[str]:
            raise SystemError("psutil: cmdline read failed mid-iteration")

    class _Occupant:
        def __init__(self, pid: int, port: int, home: str) -> None:
            self.pid = pid
            self._cmdline = ["python", "-m", psd._PAGE_SERVER_MODULE, "--port", str(port)]
            self._home = home

        def cmdline(self) -> list[str]:
            return list(self._cmdline)

        def environ(self) -> dict[str, str]:
            return {"AVA_HOME": self._home}

    procs: list[object] = [
        _ReadFails(),
        _Occupant(5001, 12001, "/home/a"),
        _Occupant(5002, 12002, "/home/b"),
    ]

    def process_iter(*args: object, **kwargs: object) -> Iterator[object]:
        del args, kwargs
        return iter(procs)

    monkeypatch.setattr(psd.psutil, "process_iter", process_iter)
    assert psd._page_server_occupants() == {
        12001: (5001, "/home/a"),
        12002: (5002, "/home/b"),
    }
