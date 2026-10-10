"""Actual trace workers retain bounded stop receipts and late failures."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from base.telemetry import tracing
from base.telemetry.otlp import trace_workers as lifecycle


@pytest.fixture
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("TRACELOOP_TRACE_CONTENT", "")
    state: dict[str, Any] = {
        "initialized": False,
        "collector_offline_reported": False,
        "retry_thread": None,
        "arm_thread": None,
        "init_resolved": threading.Event(),
        "arm_failed": False,
        "timeout_reported": False,
    }
    monkeypatch.setattr(tracing, "_state", state)
    yield state
    if "_expected_error" in state:
        with pytest.raises(BaseException) as raised:
            tracing.shutdown(timeout=5)
        assert raised.value is state["_expected_error"]
    else:
        tracing.shutdown(timeout=5)
    assert all(
        not state[key]._thread.is_alive()
        for key in ("arm_owner", "retry_owner")
        if state.get(key) is not None
    )


def test_first_use_timeout_still_allows_late_arm_success(
    monkeypatch: pytest.MonkeyPatch, fresh_state: dict[str, Any]
) -> None:
    entered, release = threading.Event(), threading.Event()

    def initialize(_endpoint: str, stopped: threading.Event) -> None:
        entered.set()
        assert release.wait(5)
        if not stopped.is_set():
            fresh_state["initialized"] = True

    monkeypatch.setattr(tracing, "_arm_tracing", initialize)
    monkeypatch.setattr(tracing, "_INIT_RESOLVED_TIMEOUT_S", 0.01)
    tracing._start_arm_thread("http://collector")
    owner = fresh_state["arm_owner"]
    try:
        assert entered.wait(5)
        tracing.ensure_init_resolved()
        assert fresh_state["timeout_reported"]
        assert owner._thread.is_alive()
        release.set()
        assert owner.finished.wait(5)
        assert fresh_state["initialized"]
        assert tracing.shutdown() == ()
        tracing._start_arm_thread("http://replacement")
        assert fresh_state["arm_owner"] is owner
    finally:
        release.set()


def test_real_sdk_arm_block_does_not_hold_stop_lock(
    monkeypatch: pytest.MonkeyPatch, fresh_state: dict[str, Any]
) -> None:
    entered, release = threading.Event(), threading.Event()

    def initialize(**_kwargs: object) -> None:
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", initialize)
    tracing._start_arm_thread("http://collector")
    owner = fresh_state["arm_owner"]
    try:
        assert entered.wait(5)
        started = time.monotonic()
        assert tracing.shutdown(timeout=0.01) == ("trace-arm",)
        assert time.monotonic() - started < 1
        assert owner._thread.is_alive()
        release.set()
        assert owner.finished.wait(5)
        assert not fresh_state["initialized"]
        assert tracing.shutdown() == ()
    finally:
        release.set()


def test_unknown_late_arm_error_is_visible_then_original_stop_raises(
    monkeypatch: pytest.MonkeyPatch, fresh_state: dict[str, Any]
) -> None:
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    error = RuntimeError("unexpected owner defect")
    seen: list[BaseException] = []

    def initialize(_endpoint: str, _stopped: threading.Event) -> None:
        entered.set()
        assert release.wait(5)
        raise error

    def report(_message: str, **details: Any) -> None:
        seen.append(details["exc"])
        observed.set()

    monkeypatch.setattr(tracing, "_arm_tracing", initialize)
    monkeypatch.setattr(lifecycle, "report_no_pipeline", report)
    tracing._start_arm_thread("http://collector")
    owner = fresh_state["arm_owner"]
    try:
        assert entered.wait(5)
        assert tracing.shutdown(timeout=0.01) == ("trace-arm",)
        release.set()
        assert observed.wait(5)
        assert owner.finished.wait(5)
        assert seen == [error]
        with pytest.raises(RuntimeError) as raised:
            tracing.shutdown()
        assert raised.value is error
    finally:
        release.set()
        owner._thread.join(timeout=5)
        fresh_state["_expected_error"] = error


def test_collector_wait_keeps_300_second_read_timing_and_stop_wakes_it(
    monkeypatch: pytest.MonkeyPatch, fresh_state: dict[str, Any]
) -> None:
    interval_read = threading.Event()
    attempts: list[None] = []

    def interval() -> float:
        interval_read.set()
        return 300.0

    owner = lifecycle.CollectorRetry(
        initialize=lambda: attempts.append(None), resolved=lambda: False, interval=interval
    )
    fresh_state["retry_owner"] = owner
    fresh_state["retry_thread"] = owner._thread
    assert interval_read.wait(5)
    assert attempts == []
    assert tracing.shutdown(timeout=1) == ()
    assert owner.finished.is_set()
    assert attempts == []


def test_retry_unknown_error_retains_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    error = RuntimeError("unexpected retry preflight")
    observed = threading.Event()
    seen: list[BaseException] = []

    def initialize() -> None:
        raise error

    def report(_message: str, **details: Any) -> None:
        seen.append(details["exc"])
        observed.set()

    monkeypatch.setattr(lifecycle, "report_no_pipeline", report)
    owner = lifecycle.CollectorRetry(
        initialize=initialize, resolved=lambda: False, interval=lambda: 0
    )
    assert observed.wait(5)
    assert owner.finished.wait(5)
    with pytest.raises(RuntimeError) as raised:
        owner.stop()
    assert raised.value is error
    assert seen == [error]
    assert not owner._thread.is_alive()
