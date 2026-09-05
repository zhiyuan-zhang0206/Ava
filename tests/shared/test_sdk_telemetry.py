"""Unit tests for shared/sdk_telemetry.py — the SDK-usage runtime primitives.

Covers the frame/emit core (`run_metered`), the agent-code scope gate (`recording`),
the semantic-enrichment primitive (`annotate`) and its frame attribution, and the
event write (`emit`) — including an end-to-end check that a call's annotations land in
its emitted `sdk_call` event `detail`.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from loguru import logger

from shared import sdk_telemetry


@pytest.fixture(autouse=True)
def _reset_sdk_call_sampler() -> None:
    """Make each test's first real SDK event the sampled-in record."""
    sdk_telemetry._sdk_call_counter = itertools.count()


def _spy_emit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        sdk_telemetry,
        "emit",
        lambda fn, detail=None, **_: calls.append((fn, dict(detail or {}))),  # pyright: ignore[reportUnknownArgumentType]
    )
    return calls


# ── scope gate ────────────────────────────────────────────────────────────────


def test_no_emit_outside_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_emit(monkeypatch)
    assert sdk_telemetry.run_metered("ns.fn", lambda: "ok", (), {}) == "ok"
    assert calls == []  # not inside recording() → framework-internal, not counted


def test_emit_inside_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_emit(monkeypatch)
    with sdk_telemetry.recording():
        assert sdk_telemetry.run_metered("ns.fn", lambda: "ok", (), {}) == "ok"
    assert calls == [("ns.fn", {})]


# ── frame stack: top-level only, nesting isolation ─────────────────────────────


def test_only_outermost_call_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_emit(monkeypatch)

    def inner() -> str:
        return sdk_telemetry.run_metered("ns.inner", lambda: "inner", (), {})

    with sdk_telemetry.recording():
        out = sdk_telemetry.run_metered("ns.outer", inner, (), {})
    assert out == "inner"
    assert calls == [("ns.outer", {})]  # nested ns.inner is not emitted


def test_return_and_exception_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _spy_emit(monkeypatch)
    with sdk_telemetry.recording():
        assert sdk_telemetry.run_metered("ns.fn", lambda a, b: a + b, (2, 3), {}) == 5  # pyright: ignore[reportUnknownArgumentType]

        def boom() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            sdk_telemetry.run_metered("ns.boom", boom, (), {})
    # the frame stack must have fully unwound after both calls.
    assert getattr(sdk_telemetry._local, "frames", []) == []


def test_failed_call_still_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_emit(monkeypatch)

    def boom() -> None:
        raise ValueError("x")

    with sdk_telemetry.recording(), pytest.raises(ValueError, match="x"):
        sdk_telemetry.run_metered("ns.boom", boom, (), {})
    assert calls == [("ns.boom", {})]  # invocation counts even when the call raises


# ── annotate ───────────────────────────────────────────────────────────────────


def test_annotate_merges_into_current_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_emit(monkeypatch)

    def grep_like() -> str:
        sdk_telemetry.annotate(subcommand="grep")
        sdk_telemetry.annotate(matches=3)
        return "done"

    with sdk_telemetry.recording():
        sdk_telemetry.run_metered("shell.run", grep_like, (), {})
    assert calls == [("shell.run", {"subcommand": "grep", "matches": 3})]


def test_annotate_attributes_to_own_frame_not_outer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nested call's annotations land on its own (discarded) frame, never on the
    outer call's emitted event."""
    calls = _spy_emit(monkeypatch)

    def inner() -> None:
        sdk_telemetry.annotate(inner_key="leaked?")

    def outer() -> None:
        sdk_telemetry.annotate(outer_key="mine")
        sdk_telemetry.run_metered("ns.inner", inner, (), {})

    with sdk_telemetry.recording():
        sdk_telemetry.run_metered("ns.outer", outer, (), {})
    assert calls == [("ns.outer", {"outer_key": "mine"})]  # no inner_key


def test_annotate_noop_outside_call() -> None:
    sdk_telemetry.annotate(anything="x")  # no active frame → silently ignored, no raise
    assert getattr(sdk_telemetry._local, "frames", []) == []


# ── emit payload + resilience ──────────────────────────────────────────────────


def test_annotate_end_to_end_detail_in_event() -> None:
    """End-to-end through the real emit path: a call's annotations show up in the
    logged `sdk_call` event's `detail` (what lands in agent_events.payload)."""
    captured: list[dict[str, Any]] = []
    sink_id = logger.add(
        lambda m: captured.append(dict(m.record["extra"])),
        level="INFO",
        filter=lambda r: r["extra"].get("event") == sdk_telemetry.SDK_CALL_EVENT,
    )
    try:

        def cd_like() -> None:
            sdk_telemetry.annotate(subcommand="cd", target="workspace")

        with sdk_telemetry.recording():
            sdk_telemetry.run_metered("shell.run", cd_like, (), {})
    finally:
        logger.remove(sink_id)

    assert len(captured) == 1
    assert captured[0]["fn"] == "shell.run"
    assert captured[0]["detail"] == {"subcommand": "cd", "target": "workspace"}
    assert isinstance(captured[0]["duration"], float)  # run_metered measures the call
    assert captured[0]["sample_rate"] == 10


def test_emit_carries_top_level_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry declares SdkCall.duration — the emit path must write it at
    the TOP level (attributes->>'duration'), not nested in detail (audit-round2
    events-obs P2: the TypedDict key had no producer before this)."""
    captured: list[dict[str, Any]] = []
    sink_id = logger.add(
        lambda m: captured.append(dict(m.record["extra"])),
        level="INFO",
        filter=lambda r: r["extra"].get("event") == sdk_telemetry.SDK_CALL_EVENT,
    )
    try:
        sdk_telemetry.emit("shell.run", {"k": 1}, duration=0.42)
    finally:
        logger.remove(sink_id)
    assert captured[0]["fn"] == "shell.run"
    assert captured[0]["duration"] == 0.42
    assert captured[0]["detail"] == {"k": 1}
    assert captured[0]["sample_rate"] == 10


def test_emit_samples_one_in_ten_calls() -> None:
    """The real event path keeps exactly the first call in each ten-call block."""
    captured: list[dict[str, Any]] = []
    sink_id = logger.add(
        lambda m: captured.append(dict(m.record["extra"])),
        level="INFO",
        filter=lambda r: r["extra"].get("event") == sdk_telemetry.SDK_CALL_EVENT,
    )
    try:
        for _ in range(10):
            sdk_telemetry.emit("files.read")
    finally:
        logger.remove(sink_id)

    assert len(captured) == 1
    assert captured[0]["fn"] == "files.read"
    assert captured[0]["sample_rate"] == 10


def test_emit_omits_detail_when_empty() -> None:
    captured: list[dict[str, Any]] = []
    sink_id = logger.add(
        lambda m: captured.append(dict(m.record["extra"])),
        level="INFO",
        filter=lambda r: r["extra"].get("event") == sdk_telemetry.SDK_CALL_EVENT,
    )
    try:
        sdk_telemetry.emit("files.read")
    finally:
        logger.remove(sink_id)
    assert captured[0]["fn"] == "files.read"
    assert "detail" not in captured[0]


def test_emit_swallows_sink_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def bind(self, **_kw: object) -> object:
            raise RuntimeError("sink down")

    monkeypatch.setattr(sdk_telemetry, "logger", _Boom())
    sdk_telemetry.emit("ns.fn", {"k": 1})  # must not raise
