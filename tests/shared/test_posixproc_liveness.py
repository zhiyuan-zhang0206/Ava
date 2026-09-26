"""Unit-level tests for shared.posixproc's liveness primitives.

Split out of test_posixproc.py (which spawns real child processes for the
double-fork reparent path): these instead stub psutil/os to exercise
`_process_is_live` and `_group_empty` directly, with no real subprocess.
"""

from __future__ import annotations

import psutil
import pytest

from shared import posixproc


def test_process_is_live_false_for_zombie(monkeypatch: pytest.MonkeyPatch) -> None:
    """_process_is_live counts a zombie as dead awaiting its parent's reap. The
    graceful verdict must not wait on init/launchd reap latency (macmini
    failure on #1301; #1303 class)."""
    import types

    proc = types.SimpleNamespace()
    proc.is_running = lambda: True
    proc.status = lambda: psutil.STATUS_ZOMBIE
    assert posixproc._process_is_live(proc) is False  # type: ignore[arg-type]


def _group_exists(_pgid: int, _sig: int) -> None:
    """os.killpg stub: the probe group exists (no ProcessLookupError)."""
    return


def _same_group(_pid: int) -> int:
    """os.getpgid stub: every probed process belongs to the group under test."""
    return 999


def test_group_empty_ignores_zombie_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """A group whose only occupants are zombies is empty for the graceful
    verdict — killpg(pgid, 0) would still succeed on it, so the fast probe is
    followed by a member walk that exempts zombies."""
    import types

    zombie = types.SimpleNamespace(pid=424242)
    zombie.status = lambda: psutil.STATUS_ZOMBIE  # type: ignore[attr-defined]
    monkeypatch.setattr(posixproc.os, "killpg", _group_exists)
    monkeypatch.setattr(posixproc.psutil, "process_iter", lambda: iter([zombie]))  # type: ignore[arg-type]
    monkeypatch.setattr(posixproc.os, "getpgid", _same_group)
    assert posixproc._group_empty(999) is True


def test_group_empty_false_with_live_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live member keeps the group occupied."""
    import types

    live = types.SimpleNamespace(pid=1)
    live.status = lambda: psutil.STATUS_RUNNING  # type: ignore[attr-defined]
    monkeypatch.setattr(posixproc.os, "killpg", _group_exists)
    monkeypatch.setattr(posixproc.psutil, "process_iter", lambda: iter([live]))  # type: ignore[arg-type]
    monkeypatch.setattr(posixproc.os, "getpgid", _same_group)
    assert posixproc._group_empty(999) is False
