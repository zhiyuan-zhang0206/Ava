"""The task-maintenance singleton guard checks only the pidfile its daemon writes, once.

The fleet plugin's counterpart of `test_daemon_guard_checks_only_current_pidfile` in
`services/tests/test_pidfile.py` (which covers the core daemons).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ava_builtins.plugins.ava_fleet.task_maintenance import daemon


def test_daemon_guard_checks_only_current_pidfile(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, str]] = []

    def holds_daemon(path: Path, module: str) -> bool:
        calls.append((path, module))
        return False

    monkeypatch.setattr(daemon, "pidfile_holds_daemon", holds_daemon)
    assert daemon._is_running() is False
    assert calls == [(daemon._pidfile(), daemon.__name__)]
