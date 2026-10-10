"""Actual refresh attempts retain their errors across finite shutdown."""

import threading
import time

import pytest

from base.agents.sdk import call_policy


def test_stop_retains_late_failure_and_actual_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()
    original = RuntimeError("late policy bug")

    def read() -> call_policy.SamplingPolicy:
        entered.set()
        assert release.wait(5)
        raise original

    monkeypatch.setattr(call_policy, "_read_policy", read)
    owner = call_policy.SamplingPolicyOwner()
    owner.read()
    assert entered.wait(2)
    worker = owner.worker
    assert worker is not None
    started = time.monotonic()
    assert owner.stop(0.02) is False
    assert time.monotonic() - started < 1
    assert owner.worker is worker and worker.thread.is_alive()
    with pytest.raises(RuntimeError, match="has stopped"):
        owner.read()
    release.set()
    assert worker.completed.wait(2)
    with pytest.raises(RuntimeError) as caught:
        owner.stop()
    assert caught.value is original
    assert not worker.thread.is_alive()


def test_stop_precedes_success_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()
    original = ValueError("invalid refresh")

    def read() -> call_policy.SamplingPolicy:
        entered.set()
        assert release.wait(5)
        raise original

    monkeypatch.setattr(call_policy, "_read_policy", read)
    owner = call_policy.SamplingPolicyOwner()
    owner.read()
    assert entered.wait(2)
    worker = owner.worker
    assert worker is not None
    release.set()
    assert worker.completed.wait(2)
    receipts: list[str] = []
    with pytest.raises(ValueError) as caught:
        owner.stop()
        receipts.append("done")
    assert caught.value is original
    assert receipts == []


def test_stop_collects_refresh_boundary_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = call_policy.SamplingPolicyOwner()
    original = KeyboardInterrupt("refresh interrupted")

    def refresh() -> None:
        raise original

    monkeypatch.setattr(owner, "refresh", refresh)
    try:
        owner.read()
    except KeyboardInterrupt as exc:
        assert exc is original
    worker = owner.worker
    assert worker is not None and worker.completed.wait(2)
    owner.next_refresh = 0
    with pytest.raises(KeyboardInterrupt) as read_failure:
        owner.read()
    assert read_failure.value is original and owner.worker is worker
    with pytest.raises(KeyboardInterrupt) as caught:
        owner.stop()
    assert caught.value is original
    assert worker.error is original and not worker.thread.is_alive()
