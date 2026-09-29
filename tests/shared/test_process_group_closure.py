"""The shared group-closure core on its default signal (PITR unowned launch, preparation)."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time

import pytest

from shared import process_group_closure

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

_LATE = 0x7FFFFFFF  # A member forked after the signal; never signalled by number.


def _leader() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-I", "-c", "import time;time.sleep(60)"], process_group=0
    )


def _late_listing(
    monkeypatch: pytest.MonkeyPatch, leader: int, late_rounds: int | None
) -> list[str]:
    """Record signals and listings; the listing names a late member for some rounds."""
    events: list[str] = []
    killpg = os.killpg
    listing = process_group_closure.group_members

    def signal_group(pid: int, sig: int) -> None:
        events.append("signal")
        killpg(pid, sig)

    def listed(pgid: int) -> list[int]:
        events.append("listing")
        real = listing(pgid)
        assert real == [leader]
        late = late_rounds is None or events.count("listing") <= late_rounds
        return [leader, _LATE] if late else real

    monkeypatch.setattr(os, "killpg", signal_group)
    monkeypatch.setattr(process_group_closure, "group_members", listed)
    return events


def _release(process: subprocess.Popen[bytes]) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, 9)
        process.wait(timeout=5)


def test_listed_member_besides_leader_forces_another_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _leader()
    try:
        events = _late_listing(monkeypatch, process.pid, late_rounds=1)
        process_group_closure.confirm_closure(process, time.monotonic() + 5)
        assert events == ["signal", "listing", "signal", "listing"]
        assert process.returncode is None
        assert process.wait(timeout=5) == -9
    finally:
        _release(process)


def test_unresolved_closure_keeps_the_leader_unreaped(monkeypatch: pytest.MonkeyPatch) -> None:
    held: list[subprocess.Popen[bytes]] = []
    monkeypatch.setattr(process_group_closure, "_UNRESOLVED", held)
    process = _leader()
    try:
        events = _late_listing(monkeypatch, process.pid, late_rounds=None)
        with pytest.raises(process_group_closure.GroupClosureUnresolvedError, match="besides"):
            process_group_closure.close_unadmitted(process, time.monotonic() + 0.5)
        assert events.count("signal") >= 2
        assert process.returncode is None
        assert held == [process]
    finally:
        _release(process)


def test_reaped_leader_group_is_never_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    process = subprocess.Popen([sys.executable, "-I", "-c", "pass"], process_group=0)
    process.wait(timeout=5)
    signals: list[int] = []

    def recorded(pid: int, _sig: int) -> None:
        signals.append(pid)

    monkeypatch.setattr(os, "killpg", recorded)
    with pytest.raises(RuntimeError, match="already reaped"):
        process_group_closure.confirm_closure(process, time.monotonic() + 1)
    assert signals == []
