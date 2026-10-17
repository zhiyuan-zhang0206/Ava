from __future__ import annotations

import threading
from pathlib import Path

import pytest

from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.native_process.os_platform import LockTimeoutError, file_lock
from base.sessions.pty import allocation_freeze, client
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from base.sessions.pty.tests.pty_shells import new, output_until, type_line, wait_for

pytestmark = pytest.mark.usefixtures("pty_service")
WRITER = AtomicWriter(SYSTEM_FS)


def test_a_frozen_allocation_reuses_a_live_name_but_refuses_a_missing_one_until_resume(
    unit_home: Path,
) -> None:
    assert new("meiva-agent-1-shell-0-existing", unit_home) is True
    frozen = allocation_freeze.freeze(
        holder="operator", reason="manifest and cleanup", writer=WRITER
    )
    assert frozen.generation is not None

    assert new("meiva-agent-1-shell-0-existing", unit_home) is False, "a live name stays usable"
    with pytest.raises(client.ServiceError, match="allocation refused") as refused:
        new("meiva-agent-1-shell-1-missing", unit_home)
    assert frozen.generation in str(refused.value)
    assert not client.has_session("meiva-agent-1-shell-1-missing")

    assert allocation_freeze.resume(frozen.generation, writer=WRITER)
    assert new("meiva-agent-1-shell-1-missing", unit_home) is True


def test_a_corrupt_marker_fails_closed(unit_home: Path) -> None:
    allocation_freeze.state_path().write_text("{broken-json")
    with pytest.raises(client.ServiceError, match=r"allocation refused.*invalid"):
        new("meiva-agent-1-shell-0-corrupt", unit_home)
    assert not client.has_session("meiva-agent-1-shell-0-corrupt")


def test_the_active_generation_rejects_a_live_name_from_a_prior_generation(
    unit_home: Path,
) -> None:
    name = "meiva-agent-1-shell-0-stale"
    new(name, unit_home)
    (before,) = client.list_sessions()
    assert before.generation is None
    frozen = allocation_freeze.freeze(holder="operator", reason="generation flip", writer=WRITER)
    assert frozen.generation is not None
    assert allocation_freeze.resume(frozen.generation, writer=WRITER)

    with pytest.raises(client.ServiceError, match="belongs to a prior generation"):
        new(name, unit_home)
    assert [s.pid for s in client.list_sessions()] == [before.pid], "the session was left alone"


def test_a_session_carries_the_generation_it_was_admitted_under(unit_home: Path) -> None:
    frozen = allocation_freeze.freeze(
        holder="operator", reason="desired-state cleanup", writer=WRITER
    )
    assert frozen.generation is not None
    assert allocation_freeze.resume(frozen.generation, writer=WRITER)
    names = ("meiva-agent-41-shell-2-page-dashboard", "meiva-schedule-7")
    for name in names:
        assert new(name, unit_home) is True
    assert {s.name: s.generation for s in client.list_sessions()} == dict.fromkeys(
        names, frozen.generation
    )
    started = {s.name: s.started_at for s in client.list_sessions()}
    for name in names:
        assert new(name, unit_home) is False, "retried reconciliation creates each name once"
    assert {s.name: s.started_at for s in client.list_sessions()} == started


def test_freeze_acknowledgement_has_no_later_session_start_during_concurrent_allocations(
    unit_home: Path,
) -> None:
    outcomes: dict[str, str] = {}
    lock = threading.Lock()

    def allocate(index: int) -> None:
        name = f"meiva-freeze-race-{index}"
        try:
            created = new(name, unit_home)
        except client.ServiceError as exc:
            result = f"refused: {exc}"
        else:
            result = "created" if created else "existing"
        with lock:
            outcomes[name] = result

    threads = [threading.Thread(target=allocate, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()

    def allocation_lock_is_held() -> bool:
        try:
            with file_lock(allocation_freeze.lock_path(), timeout_s=0):
                return False
        except LockTimeoutError:
            return True

    frozen: allocation_freeze.PtyAllocationFreeze | None = None
    try:
        wait_for(allocation_lock_is_held, timeout=10.0, interval=0.001)
        frozen = allocation_freeze.freeze(
            holder="ci-race", reason="prove freeze acknowledgement", writer=WRITER
        )
        assert frozen.generation is not None and frozen.created_at is not None
        for thread in threads:
            thread.join(timeout=60)
        assert any(result == "created" for result in outcomes.values())
        assert all(
            result == "created" or "allocation refused" in result for result in outcomes.values()
        ), outcomes
        sessions = client.list_sessions("meiva-freeze-race-")
        acknowledged_at = frozen.created_at.timestamp()
        assert sessions
        assert all(s.started_at <= acknowledged_at for s in sessions)
        with pytest.raises(client.ServiceError, match="allocation refused"):
            new("meiva-freeze-race-after-ack", unit_home)
        assert not client.has_session("meiva-freeze-race-after-ack")
    finally:
        for thread in threads:
            thread.join(timeout=60)
        if frozen is not None and frozen.generation is not None:
            assert allocation_freeze.resume(frozen.generation, writer=WRITER)


def test_a_frozen_session_still_serves_input(unit_home: Path) -> None:
    name = "meiva-test-frozen-use-1"
    new(name, unit_home)
    frozen = allocation_freeze.freeze(holder="operator", reason="inspection", writer=WRITER)
    try:
        type_line(name, "echo still-usable")
        output_until(name, "still-usable")
    finally:
        assert frozen.generation is not None
        assert allocation_freeze.resume(frozen.generation, writer=WRITER)
