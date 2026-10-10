"""The gated child's independent deadline has a real stop/result owner."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import cast

import pytest

from agent.execution import owner_child


def test_normal_stop_joins_the_actual_watchdog() -> None:
    watchdog = owner_child._DeadlineWatchdog(datetime.now(UTC) + timedelta(seconds=10))
    watchdog.close()
    assert watchdog._finished.is_set()
    assert not watchdog._thread.is_alive()
    # Stopping is terminal: a later close cannot start another deadline decision.
    watchdog.close()


def test_primary_execution_error_retains_the_watchdog_secondary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary, secondary = (
        ValueError("private execution failure"),
        RuntimeError("private wait failure"),
    )
    previous = OSError("private original cause")
    primary.__cause__ = previous

    def failed_wait(_self: object) -> None:
        raise secondary

    def report(
        _kind: type[BaseException], _error: BaseException, _tb: TracebackType | None
    ) -> None:
        return None

    monkeypatch.setattr(owner_child._DeadlineWatchdog, "_wait_until_deadline", failed_wait)
    monkeypatch.setattr(owner_child.sys, "excepthook", report)
    with (
        pytest.raises(ValueError) as observed,
        owner_child._DeadlineWatchdog(datetime.now(UTC) + timedelta(seconds=10)) as watchdog,
    ):
        assert watchdog._finished.wait(1)
        raise primary
    assert observed.value is primary
    group = cast("BaseExceptionGroup[BaseException]", primary.__cause__)
    assert isinstance(group, BaseExceptionGroup)
    assert group.exceptions == (previous, secondary)


def test_uncooperative_worker_is_not_reported_as_joined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocked(_self: object) -> None:
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(owner_child._DeadlineWatchdog, "_wait_until_deadline", blocked)
    monkeypatch.setattr(owner_child, "_WATCHDOG_JOIN_S", 0.02)
    watchdog = owner_child._DeadlineWatchdog(datetime.now(UTC) + timedelta(seconds=10))
    try:
        assert entered.wait(1)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="join remains unfinished"):
            watchdog.close()
        assert time.monotonic() - started < 0.5
        assert watchdog._thread.is_alive() and not watchdog._finished.is_set()
    finally:
        release.set()
        watchdog.close()


def test_worker_unknown_is_reported_immediately_and_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RuntimeError("private deadline wait failed")
    reported: list[BaseException] = []

    def failed_wait(_self: object) -> None:
        raise failure

    def report(_kind: type[BaseException], error: BaseException, _tb: TracebackType | None) -> None:
        reported.append(error)

    monkeypatch.setattr(owner_child._DeadlineWatchdog, "_wait_until_deadline", failed_wait)
    monkeypatch.setattr(owner_child.sys, "excepthook", report)
    watchdog = owner_child._DeadlineWatchdog(datetime.now(UTC) + timedelta(seconds=10))
    assert watchdog._finished.wait(1)
    assert reported == [failure]
    assert watchdog._finished.is_set() and watchdog._error is failure
    with pytest.raises(RuntimeError) as observed:
        watchdog.close()
    assert observed.value is failure
    assert not watchdog._thread.is_alive()
