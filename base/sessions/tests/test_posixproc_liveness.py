"""Unit-level tests for base.sessions.posixproc's liveness primitives.

Stubbed native reads cover zombie handling; a retained real child also checks
the public process-group observation before exit and while awaiting reap.
"""

from __future__ import annotations

import subprocess
import sys
import time

import psutil
import pytest

from base.native_process.os_platform import is_windows
from base.sessions import posixproc


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


def test_group_observation_ignores_zombie_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """A group whose only occupants are zombies is empty for the graceful
    verdict — killpg(pgid, 0) would still succeed on it, so the fast probe is
    followed by a member walk that exempts zombies."""
    import types

    zombie = types.SimpleNamespace(pid=424242)
    zombie.status = lambda: psutil.STATUS_ZOMBIE  # type: ignore[attr-defined]
    monkeypatch.setattr(posixproc.os, "killpg", _group_exists)
    monkeypatch.setattr(posixproc.psutil, "process_iter", lambda: iter([zombie]))  # type: ignore[arg-type]
    monkeypatch.setattr(posixproc.os, "getpgid", _same_group)
    assert posixproc.process_group_has_live_members(999) is False


def test_group_observation_detects_live_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live member keeps the group occupied."""
    import types

    live = types.SimpleNamespace(pid=1)
    live.status = lambda: psutil.STATUS_RUNNING  # type: ignore[attr-defined]
    monkeypatch.setattr(posixproc.os, "killpg", _group_exists)
    monkeypatch.setattr(posixproc.psutil, "process_iter", lambda: iter([live]))  # type: ignore[arg-type]
    monkeypatch.setattr(posixproc.os, "getpgid", _same_group)
    assert posixproc.process_group_has_live_members(999) is True


@pytest.mark.parametrize("pgid", [None, 0, -1])
def test_unknown_group_has_no_observed_live_member(pgid: int | None) -> None:
    assert posixproc.process_group_has_live_members(pgid) is False


@pytest.mark.skipif(is_windows(), reason="process groups require POSIX")
def test_group_observation_of_live_then_unreaped_child() -> None:
    """An exited leader awaiting reap is not a live group member."""
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        start_new_session=True,
        stdin=subprocess.PIPE,
    ) as child:
        try:
            assert posixproc.process_group_has_live_members(child.pid) is True
            assert child.stdin is not None
            child.stdin.close()
            process = psutil.Process(child.pid)
            deadline = time.monotonic() + 5
            while process.status() != psutil.STATUS_ZOMBIE:
                assert time.monotonic() < deadline, "test child did not exit before its deadline"
                time.sleep(0.01)
            assert child.returncode is None
            assert posixproc.process_group_has_live_members(child.pid) is False
        finally:
            child.kill()
            child.wait(timeout=5)
