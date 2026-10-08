"""Unit tests for base/agents/sdk/telemetry.py — the SDK-usage runtime primitives.

Covers the frame/emit core (`run_metered`), the agent-code scope gate (`recording`),
the semantic-enrichment primitive (`annotate`) and its frame attribution, and the
event write (`emit`) — including an end-to-end check that a call's annotations land in
its emitted `sdk_call` event `detail`.
"""

from __future__ import annotations

import asyncio
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from base import telemetry
from base.agents.sdk import call_policy
from base.agents.sdk import telemetry as sdk_usage_telemetry
from base.agents.sdk.call_policy import SamplingPolicy


@pytest.fixture(autouse=True)
def _full_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(call_policy, "policy", SamplingPolicy)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def capture(*_args: Any, **kw: Any) -> None:
        rows.append(kw["attributes"])

    monkeypatch.setattr(telemetry, "emit", capture)
    return rows


def _spy_emit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        sdk_usage_telemetry,
        "emit",
        lambda fn, detail=None, **_: calls.append((fn, dict(detail or {}))),  # pyright: ignore[reportUnknownArgumentType]
    )
    return calls


# ── scope gate ────────────────────────────────────────────────────────────────


def test_emit_without_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_emit(monkeypatch)
    assert sdk_usage_telemetry.run_metered("ns.fn", lambda: "ok", (), {}) == "ok"
    assert calls == [("ns.fn", {})]


def test_emit_inside_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_emit(monkeypatch)
    with sdk_usage_telemetry.recording():
        assert sdk_usage_telemetry.run_metered("ns.fn", lambda: "ok", (), {}) == "ok"
    assert calls == [("ns.fn", {})]


# ── frame stack: top-level only, nesting isolation ─────────────────────────────


def test_only_outermost_call_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_emit(monkeypatch)

    def inner() -> str:
        return sdk_usage_telemetry.run_metered("ns.inner", lambda: "inner", (), {})

    with sdk_usage_telemetry.recording():
        out = sdk_usage_telemetry.run_metered("ns.outer", inner, (), {})
    assert out == "inner"
    assert calls == [("ns.outer", {})]  # nested ns.inner is not emitted


def test_return_and_exception_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _spy_emit(monkeypatch)
    with sdk_usage_telemetry.recording():
        assert sdk_usage_telemetry.run_metered("ns.fn", lambda a, b: a + b, (2, 3), {}) == 5  # pyright: ignore[reportUnknownArgumentType]

        def boom() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            sdk_usage_telemetry.run_metered("ns.boom", boom, (), {})
    # the frame stack must have fully unwound after both calls.
    assert sdk_usage_telemetry._frames.get() == ()


def test_failed_call_still_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_emit(monkeypatch)

    def boom() -> None:
        raise ValueError("x")

    with sdk_usage_telemetry.recording(), pytest.raises(ValueError, match="x"):
        sdk_usage_telemetry.run_metered("ns.boom", boom, (), {})
    assert calls == [("ns.boom", {})]  # invocation counts even when the call raises


# ── annotate ───────────────────────────────────────────────────────────────────


def test_annotate_merges_into_current_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_emit(monkeypatch)

    def grep_like() -> str:
        sdk_usage_telemetry.annotate(subcommand="grep")
        sdk_usage_telemetry.annotate(matches=3)
        return "done"

    with sdk_usage_telemetry.recording():
        sdk_usage_telemetry.run_metered("shell.run", grep_like, (), {})
    assert calls == [("shell.run", {"subcommand": "grep", "matches": 3})]


def test_annotate_attributes_to_own_frame_not_outer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nested call's annotations land on its own (discarded) frame, never on the
    outer call's emitted event."""
    calls = _spy_emit(monkeypatch)

    def inner() -> None:
        sdk_usage_telemetry.annotate(inner_key="leaked?")

    def outer() -> None:
        sdk_usage_telemetry.annotate(outer_key="mine")
        sdk_usage_telemetry.run_metered("ns.inner", inner, (), {})

    with sdk_usage_telemetry.recording():
        sdk_usage_telemetry.run_metered("ns.outer", outer, (), {})
    assert calls == [("ns.outer", {"outer_key": "mine"})]  # no inner_key


def test_annotate_noop_outside_call() -> None:
    sdk_usage_telemetry.annotate(anything="x")  # no active frame → silently ignored, no raise
    assert sdk_usage_telemetry._frames.get() == ()


# ── emit payload + resilience ──────────────────────────────────────────────────


def test_detail_reaches_event(captured: list[dict[str, Any]]) -> None:
    def body() -> None:
        sdk_usage_telemetry.annotate(subcommand="cd", target="workspace")

    sdk_usage_telemetry.run_metered("shell.run", body, (), {})
    assert len(captured) == 1
    assert captured[0]["detail"] == {"subcommand": "cd", "target": "workspace"}
    assert isinstance(captured[0]["duration"], float)
    assert captured[0]["sample_rate"] == 1


def test_emit_carries_top_level_duration(captured: list[dict[str, Any]]) -> None:
    sdk_usage_telemetry.emit("shell.run", {"k": 1}, duration=0.42)
    assert captured == [{"fn": "shell.run", "duration": 0.42, "detail": {"k": 1}, "sample_rate": 1}]


def test_default_records_every_call(captured: list[dict[str, Any]]) -> None:
    for _ in range(10):
        sdk_usage_telemetry.emit("files.read")
    assert len(captured) == 10
    assert all(row["sample_rate"] == 1 for row in captured)


def test_emit_omits_detail_when_empty(captured: list[dict[str, Any]]) -> None:
    sdk_usage_telemetry.emit("files.read")
    assert "detail" not in captured[0]


def test_emit_swallows_sink_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("sink down")

    monkeypatch.setattr(telemetry, "emit", fail)
    sdk_usage_telemetry.emit("ns.fn", {"k": 1})


def test_emit_sink_failure_is_reported_with_its_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken sink never raises into the SDK call, and is never silent either."""
    reports: list[tuple[str, BaseException]] = []

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("sink down")

    monkeypatch.setattr(telemetry, "emit", fail)

    def record(sink: str, exc: BaseException) -> None:
        reports.append((sink, exc))

    monkeypatch.setattr(telemetry, "report_sink_failure", record)
    sdk_usage_telemetry.emit("ns.fn")
    assert len(reports) == 1
    assert "sdk_call" in reports[0][0]
    assert isinstance(reports[0][1], RuntimeError)


# ── full tally: per-recording, unsampled, top-level only ──────────────────────


def test_recording_yields_the_full_tally_per_fn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loop N executions count N; a call that never runs counts nothing; emit's
    spy sees the same order of calls."""
    calls = _spy_emit(monkeypatch)
    with sdk_usage_telemetry.recording() as tally:
        for _ in range(3):
            sdk_usage_telemetry.run_metered("files.read", lambda: "ok", (), {})
        sdk_usage_telemetry.run_metered("shell.run", lambda: "ok", (), {})
    assert tally == {"files.read": 3, "shell.run": 1}
    assert [fn for fn, _ in calls] == ["files.read", "files.read", "files.read", "shell.run"]


def test_live_sampling_keeps_tally_complete(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]
) -> None:
    selected = iter(range(10))

    def sample(_every: int) -> int:
        return next(selected)

    monkeypatch.setattr(random, "randrange", sample)
    current = SamplingPolicy(sampling_enabled=True, sample_every=10)
    monkeypatch.setattr(call_policy, "policy", lambda: current)
    with sdk_usage_telemetry.recording() as tally:
        for _ in range(10):
            sdk_usage_telemetry.run_metered("files.read", lambda: None, (), {})
        current = SamplingPolicy(sampling_enabled=False, sample_every=10)
        sdk_usage_telemetry.run_metered("files.read", lambda: None, (), {})
    assert [row["sample_rate"] for row in captured] == [10, 1]
    assert tally == {"files.read": 11}


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("failure", ["auth", "schema", "code"])
async def test_invalid_policy_blocks_sdk_side_effects(
    monkeypatch: pytest.MonkeyPatch, async_call: bool, failure: str
) -> None:
    cache = call_policy._PolicyCache()
    cache.value = SamplingPolicy()

    def invalid() -> SamplingPolicy:
        if failure == "auth":
            httpx.Response(
                401, request=httpx.Request("GET", "https://gateway/bootstrap")
            ).raise_for_status()
        if failure == "schema":
            return SamplingPolicy(sample_every=0)
        raise TypeError("policy reader bug")

    monkeypatch.setattr(call_policy, "_read_policy", invalid)
    cache.refresh()
    monkeypatch.setattr(call_policy, "policy", cache.read)
    calls: list[str] = []

    def body() -> None:
        calls.append("side effect")

    async def async_body() -> None:
        body()

    error = {"auth": httpx.HTTPStatusError, "schema": ValidationError, "code": TypeError}[failure]
    with sdk_usage_telemetry.recording() as tally:
        for _ in range(2):
            with pytest.raises(error):
                if async_call:
                    await sdk_usage_telemetry.run_metered_async("files.write", async_body, (), {})
                else:
                    sdk_usage_telemetry.run_metered("files.write", body, (), {})
        assert tally == {}
    assert calls == []
    assert sdk_usage_telemetry._frames.get() == ()


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("original", [ValueError("SDK failed"), asyncio.CancelledError()])
async def test_started_call_uses_its_snapshot_and_preserves_its_exception(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict[str, Any]],
    async_call: bool,
    original: BaseException,
) -> None:
    cache = call_policy._PolicyCache()
    cache.value = SamplingPolicy()
    cache.next_refresh = float("inf")
    reads: list[str] = []

    def current() -> SamplingPolicy:
        reads.append("read")
        return cache.read()

    def invalid() -> SamplingPolicy:
        raise TypeError("new invalid policy")

    monkeypatch.setattr(call_policy, "policy", current)
    monkeypatch.setattr(call_policy, "_read_policy", invalid)
    nested: list[str] = []

    def body() -> None:
        cache.refresh()
        sdk_usage_telemetry.run_metered("inner.call", lambda: nested.append("ran"), (), {})
        raise original

    async def async_body() -> None:
        body()

    with pytest.raises(type(original)) as caught:
        if async_call:
            await sdk_usage_telemetry.run_metered_async("outer.call", async_body, (), {})
        else:
            sdk_usage_telemetry.run_metered("outer.call", body, (), {})
    assert caught.value is original
    assert reads == ["read"]
    assert nested == ["ran"]
    assert captured[0]["fn"] == "outer.call"
    with pytest.raises(TypeError, match="new invalid policy"):
        sdk_usage_telemetry.run_metered("next.call", lambda: nested.append("must not run"), (), {})
    assert nested == ["ran"]


def test_direct_emit_propagates_policy_errors_without_creating_events(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]
) -> None:
    def invalid() -> SamplingPolicy:
        raise TypeError("invalid policy")

    monkeypatch.setattr(call_policy, "policy", invalid)
    with pytest.raises(TypeError, match="invalid policy"):
        sdk_usage_telemetry.emit("files.write")
    assert captured == []


def test_tally_counts_a_failed_top_level_call_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Like its event, a call that raises still counts — it really executed."""
    _spy_emit(monkeypatch)

    def boom() -> None:
        raise ValueError("x")

    with sdk_usage_telemetry.recording() as tally, pytest.raises(ValueError, match="x"):
        sdk_usage_telemetry.run_metered("ns.boom", boom, (), {})
    assert tally == {"ns.boom": 1}


def test_tally_counts_only_outermost_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nested ava.* call inside a metered call is framework fan-out, not an
    agent statement — same top-level-only rule as the events."""
    _spy_emit(monkeypatch)

    def inner() -> str:
        return sdk_usage_telemetry.run_metered("ns.inner", lambda: "inner", (), {})

    with sdk_usage_telemetry.recording() as tally:
        sdk_usage_telemetry.run_metered("ns.outer", inner, (), {})
    assert tally == {"ns.outer": 1}


def test_tally_absent_outside_recording_and_each_block_gets_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _spy_emit(monkeypatch)
    sdk_usage_telemetry.run_metered("ns.fn", lambda: "ok", (), {})  # framework-internal: no tally
    with sdk_usage_telemetry.recording() as first:
        sdk_usage_telemetry.run_metered("a.fn", lambda: "ok", (), {})
    with sdk_usage_telemetry.recording() as second:
        sdk_usage_telemetry.run_metered("b.fn", lambda: "ok", (), {})
    assert first == {"a.fn": 1}
    assert second == {"b.fn": 1}
    assert sdk_usage_telemetry._tally.get() is None


def test_tally_entries_sorts_by_descending_count_then_method() -> None:
    assert sdk_usage_telemetry.tally_entries({"b.x": 2, "a.x": 2, "c.x": 5}) == [
        {"method": "c.x", "count": 5},
        {"method": "a.x", "count": 2},
        {"method": "b.x", "count": 2},
    ]
    assert sdk_usage_telemetry.tally_entries({}) == []


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
    index = sdk_usage_telemetry.sdk_calls_by_tool_call_id(messages)
    assert index == {
        "tc-1": [sdk_usage_telemetry.SdkCall(method="files.read", count=3)],
        "tc-2": [],
    }


def test_sdk_calls_by_tool_call_id_respects_the_start_window() -> None:
    """Only messages[start:] is scanned — the rendered span."""
    messages = [
        _exec_output("tc-1", [{"method": "files.read", "count": 1}]),
        _exec_output("tc-2", [{"method": "shell.run", "count": 1}]),
    ]
    assert set(sdk_usage_telemetry.sdk_calls_by_tool_call_id(messages, start=1)) == {"tc-2"}


@pytest.mark.parametrize("error_type", [ImportError, RuntimeError])
@pytest.mark.parametrize("async_call", [False, True])
def test_local_capture_import_failure_rejects_body_with_original_exception(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception], async_call: bool
) -> None:
    import builtins

    failure = error_type("local capture module is invalid")
    original_import = builtins.__import__
    executed: list[str] = []
    emitted = _spy_emit(monkeypatch)

    def import_module(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "base.agents.impersonation.manifest":
            raise failure
        return original_import(name, *args, **kwargs)

    def body() -> None:
        executed.append("body")

    async def async_body() -> None:
        body()

    monkeypatch.setattr(builtins, "__import__", import_module)
    with sdk_usage_telemetry.recording() as tally, pytest.raises(error_type) as raised:
        if async_call:
            asyncio.run(sdk_usage_telemetry.run_metered_async("capture.call", async_body, (), {}))
        else:
            sdk_usage_telemetry.run_metered("capture.call", body, (), {})
    assert raised.value is failure
    assert executed == [] and emitted == [] and tally == {}


def test_no_local_participant_uses_the_real_optional_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.agents.impersonation import manifest

    assert manifest.admit_local_sdk_call() is None
    emitted = _spy_emit(monkeypatch)

    def body() -> str:
        assert not manifest.local_sdk_call_was_admitted()
        return "without-participant"

    assert sdk_usage_telemetry.run_metered("capture.call", body, (), {}) == "without-participant"
    assert emitted == [("capture.call", {})]


@pytest.mark.parametrize("error_type", ["ImportError", "RuntimeError"])
def test_cold_capture_import_failure_rejects_body(tmp_path: Path, error_type: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
import importlib.abc
import os
import sys
from base.agents.sdk import call_policy, telemetry
from base.agents.sdk.call_policy import SamplingPolicy

assert 'base.agents.impersonation.manifest' not in sys.modules
error_type = {'ImportError': ImportError, 'RuntimeError': RuntimeError}[os.environ['TEST_CAPTURE_ERROR']]
failure = error_type('invalid cold capture module')
executed = []
call_policy.policy = SamplingPolicy
telemetry.emit = lambda *args, **kwargs: executed.append('emit')

class BrokenCapture(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'base.agents.impersonation.manifest':
            raise failure

sys.meta_path.insert(0, BrokenCapture())
try:
    telemetry.run_metered('capture.call', lambda: executed.append('body'), (), {})
except (ImportError, RuntimeError) as actual:
    assert actual is failure
else:
    raise AssertionError('capture import failure was hidden')
assert executed == []
""",
        ],
        cwd=tmp_path,
        env={
            **{key: value for key, value in os.environ.items() if key != "AVA_CONFIG_BOOT"},
            "AVA_HOME": str(tmp_path / "absent-home"),
            "AVA_CONFIG_FETCH": "skip",
            "TEST_CAPTURE_ERROR": error_type,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
