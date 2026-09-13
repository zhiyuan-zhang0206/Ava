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


# ── full tally: per-recording, unsampled, top-level only ──────────────────────


def test_recording_yields_the_full_tally_per_fn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loop N executions count N; a call that never runs counts nothing; emit's
    spy sees the same order of calls."""
    calls = _spy_emit(monkeypatch)
    with sdk_telemetry.recording() as tally:
        for _ in range(3):
            sdk_telemetry.run_metered("files.read", lambda: "ok", (), {})
        sdk_telemetry.run_metered("shell.run", lambda: "ok", (), {})
    assert tally == {"files.read": 3, "shell.run": 1}
    assert [fn for fn, _ in calls] == ["files.read", "files.read", "files.read", "shell.run"]


def test_tally_is_unsampled_while_events_keep_the_one_in_ten_gate() -> None:
    """Acceptance: the 1-in-10 sampling stays in the emit layer only — ten real
    executions write one event but a full tally of ten."""
    captured: list[dict[str, Any]] = []
    sink_id = logger.add(
        lambda m: captured.append(dict(m.record["extra"])),
        level="INFO",
        filter=lambda r: r["extra"].get("event") == sdk_telemetry.SDK_CALL_EVENT,
    )
    try:
        with sdk_telemetry.recording() as tally:
            for _ in range(10):
                sdk_telemetry.run_metered("files.read", lambda: "ok", (), {})
    finally:
        logger.remove(sink_id)

    assert len(captured) == 1  # the sampler dropped the other nine events
    assert tally == {"files.read": 10}  # the tally is full
    assert getattr(sdk_telemetry._local, "tally", None) is None  # restored on exit


def test_tally_counts_a_failed_top_level_call_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Like its event, a call that raises still counts — it really executed."""
    _spy_emit(monkeypatch)

    def boom() -> None:
        raise ValueError("x")

    with sdk_telemetry.recording() as tally, pytest.raises(ValueError, match="x"):
        sdk_telemetry.run_metered("ns.boom", boom, (), {})
    assert tally == {"ns.boom": 1}


def test_tally_counts_only_outermost_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nested ava.* call inside a metered call is framework fan-out, not an
    agent statement — same top-level-only rule as the events."""
    _spy_emit(monkeypatch)

    def inner() -> str:
        return sdk_telemetry.run_metered("ns.inner", lambda: "inner", (), {})

    with sdk_telemetry.recording() as tally:
        sdk_telemetry.run_metered("ns.outer", inner, (), {})
    assert tally == {"ns.outer": 1}


def test_tally_absent_outside_recording_and_each_block_gets_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _spy_emit(monkeypatch)
    sdk_telemetry.run_metered("ns.fn", lambda: "ok", (), {})  # framework-internal: no tally
    with sdk_telemetry.recording() as first:
        sdk_telemetry.run_metered("a.fn", lambda: "ok", (), {})
    with sdk_telemetry.recording() as second:
        sdk_telemetry.run_metered("b.fn", lambda: "ok", (), {})
    assert first == {"a.fn": 1}
    assert second == {"b.fn": 1}
    assert getattr(sdk_telemetry._local, "tally", None) is None


def test_tally_entries_sorts_by_descending_count_then_method() -> None:
    assert sdk_telemetry.tally_entries({"b.x": 2, "a.x": 2, "c.x": 5}) == [
        {"method": "c.x", "count": 5},
        {"method": "a.x", "count": 2},
        {"method": "b.x", "count": 2},
    ]
    assert sdk_telemetry.tally_entries({}) == []


# ── reading the metadata back off exec_output messages ────────────────────────


def _exec_output(tool_call_id: str, sdk_calls: list[dict[str, Any]] | None) -> Any:
    from langchain_core.messages import ToolMessage

    kwargs: dict[str, Any] = {"ava_msg_type": "exec_output"}
    if sdk_calls is not None:
        kwargs["sdk_calls"] = sdk_calls
    return ToolMessage(content="out", tool_call_id=tool_call_id, additional_kwargs=kwargs)


def test_sdk_calls_by_tool_call_id_reads_exec_output_metadata() -> None:
    """Present-and-empty maps to `[]` (a real zero); a message without the field
    (pre-tally history) stays absent from the map; other messages are ignored."""
    from langchain_core.messages import HumanMessage, ToolMessage

    messages = [
        HumanMessage(content="hi"),
        _exec_output("tc-1", [{"method": "files.read", "count": 3}]),
        _exec_output("tc-2", []),
        _exec_output("tc-3", None),
        ToolMessage(content="x", tool_call_id="tc-4"),
    ]
    index = sdk_telemetry.sdk_calls_by_tool_call_id(messages)
    assert index == {
        "tc-1": [sdk_telemetry.SdkCall(method="files.read", count=3)],
        "tc-2": [],
    }


def test_sdk_calls_by_tool_call_id_respects_the_start_window() -> None:
    """Only messages[start:] is scanned — the rendered span."""
    messages = [
        _exec_output("tc-1", [{"method": "files.read", "count": 1}]),
        _exec_output("tc-2", [{"method": "shell.run", "count": 1}]),
    ]
    assert set(sdk_telemetry.sdk_calls_by_tool_call_id(messages, start=1)) == {"tc-2"}
