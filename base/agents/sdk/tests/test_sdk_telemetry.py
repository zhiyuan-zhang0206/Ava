"""Unit tests for base/agents/sdk/telemetry.py — the SDK-usage runtime primitives.

Covers independent public entries, explicit execution tallies, call-local snapshots
and the event write (`emit`).
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
from base.agents.sdk.tally import SdkCallTally


@pytest.fixture
def sampling_owner() -> call_policy.SamplingPolicyOwner:
    return call_policy.SamplingPolicyOwner()


@pytest.fixture(autouse=True)
def _full_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)


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


def test_emit_without_execution_tally(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    calls = _spy_emit(monkeypatch)
    assert (
        sdk_usage_telemetry.run_metered(
            "ns.fn",
            lambda: "ok",
            (),
            {},
            identity={},
            sampling_owner=sampling_owner,
        )
        == "ok"
    )
    assert calls == [("ns.fn", {})]


def test_call_owns_its_entry_identity_snapshot(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    """Mutation of the caller's mapping during the body cannot relabel the emitted event."""
    rows: list[dict[str, Any]] = []

    def capture(*_args: Any, **kwargs: Any) -> None:
        rows.append(kwargs)

    monkeypatch.setattr(telemetry, "emit", capture)
    identity: dict[str, Any] = {"agent_id": 41, "source": "agent:41"}

    def body() -> str:
        identity.update(agent_id=42, source="agent:42")
        return "done"

    assert (
        sdk_usage_telemetry.run_metered(
            "ns.fn",
            body,
            (),
            {},
            identity=identity,
            sampling_owner=sampling_owner,
        )
        == "done"
    )
    assert rows[0]["agent_id"] == 41
    assert rows[0]["source"] == "agent:41"


def test_emit_with_explicit_execution_tally(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    calls = _spy_emit(monkeypatch)
    tally = SdkCallTally()
    assert (
        sdk_usage_telemetry.run_metered(
            "ns.fn",
            lambda: "ok",
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )
        == "ok"
    )
    assert calls == [("ns.fn", {})]


# ── independent public entries ───────────────────────────────────────────────


def test_each_public_entry_emits(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    calls = _spy_emit(monkeypatch)

    def inner() -> str:
        return sdk_usage_telemetry.run_metered(
            "ns.inner",
            lambda: "inner",
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )

    tally = SdkCallTally()
    out = sdk_usage_telemetry.run_metered(
        "ns.outer",
        inner,
        (),
        {},
        identity={},
        tally=tally,
        sampling_owner=sampling_owner,
    )
    assert out == "inner"
    assert calls == [("ns.inner", {}), ("ns.outer", {})]


def test_return_and_exception_pass_through(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    _spy_emit(monkeypatch)

    def add(a: int, b: int) -> int:
        return a + b

    tally = SdkCallTally()
    assert (
        sdk_usage_telemetry.run_metered(
            "ns.fn",
            add,
            (2, 3),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )
        == 5
    )

    def boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        sdk_usage_telemetry.run_metered(
            "ns.boom",
            boom,
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )


def test_failed_call_still_emits(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    calls = _spy_emit(monkeypatch)

    def boom() -> None:
        raise ValueError("x")

    tally = SdkCallTally()
    with pytest.raises(ValueError, match="x"):
        sdk_usage_telemetry.run_metered(
            "ns.boom",
            boom,
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )
    assert calls == [("ns.boom", {})]  # invocation counts even when the call raises


def test_emit_carries_top_level_duration(
    captured: list[dict[str, Any]], sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    sdk_usage_telemetry.emit(
        "shell.run",
        {"k": 1},
        duration=0.42,
        identity={},
        sampling_owner=sampling_owner,
    )
    assert captured == [{"fn": "shell.run", "duration": 0.42, "detail": {"k": 1}, "sample_rate": 1}]


def test_default_records_every_call(
    captured: list[dict[str, Any]], sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    for _ in range(10):
        sdk_usage_telemetry.emit("files.read", identity={}, sampling_owner=sampling_owner)
    assert len(captured) == 10
    assert all(row["sample_rate"] == 1 for row in captured)


def test_emit_omits_detail_when_empty(
    captured: list[dict[str, Any]], sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    sdk_usage_telemetry.emit("files.read", identity={}, sampling_owner=sampling_owner)
    assert "detail" not in captured[0]


def test_emit_does_not_classify_transport_errors_or_retry(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    failure = httpx.TimeoutException("not a transport owned by the SDK emitter")
    attempts: list[str] = []

    def fail(*_args: Any, **_kwargs: Any) -> None:
        attempts.append("emit")
        raise failure

    monkeypatch.setattr(telemetry, "emit", fail)
    with pytest.raises(httpx.TimeoutException) as raised:
        sdk_usage_telemetry.emit("ns.fn", identity={}, sampling_owner=sampling_owner)
    assert raised.value is failure
    assert attempts == ["emit"]


# ── explicit execution tally, independent of event sampling ───────────────────


def test_explicit_owner_collects_full_tally_per_fn(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    """Loop N executions count N; a call that never runs counts nothing; emit's
    spy sees the same order of calls."""
    calls = _spy_emit(monkeypatch)
    tally = SdkCallTally()
    for _ in range(3):
        sdk_usage_telemetry.run_metered(
            "files.read",
            lambda: "ok",
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )
    sdk_usage_telemetry.run_metered(
        "shell.run",
        lambda: "ok",
        (),
        {},
        identity={},
        tally=tally,
        sampling_owner=sampling_owner,
    )
    assert tally.snapshot() == {"files.read": 3, "shell.run": 1}
    assert [fn for fn, _ in calls] == ["files.read", "files.read", "files.read", "shell.run"]


def test_live_sampling_keeps_tally_complete(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict[str, Any]],
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    selected = iter(range(10))

    def sample(_every: int) -> int:
        return next(selected)

    monkeypatch.setattr(random, "randrange", sample)
    current = SamplingPolicy(sampling_enabled=True, sample_every=10)

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return current

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    tally = SdkCallTally()
    for _ in range(10):
        sdk_usage_telemetry.run_metered(
            "files.read",
            lambda: None,
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )
    current = SamplingPolicy(sampling_enabled=False, sample_every=10)
    sdk_usage_telemetry.run_metered(
        "files.read",
        lambda: None,
        (),
        {},
        identity={},
        tally=tally,
        sampling_owner=sampling_owner,
    )
    assert [row["sample_rate"] for row in captured] == [10, 1]
    assert tally.snapshot() == {"files.read": 11}


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("failure", ["auth", "schema", "code"])
async def test_invalid_policy_blocks_sdk_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    async_call: bool,
    failure: str,
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    cache = call_policy.SamplingPolicyOwner()
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

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return cache.read()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    calls: list[str] = []

    def body() -> None:
        calls.append("side effect")

    async def async_body() -> None:
        body()

    error = {"auth": httpx.HTTPStatusError, "schema": ValidationError, "code": TypeError}[failure]
    tally = SdkCallTally()
    for _ in range(2):
        with pytest.raises(error):
            if async_call:
                await sdk_usage_telemetry.run_metered_async(
                    "files.write",
                    async_body,
                    (),
                    {},
                    identity={},
                    tally=tally,
                    sampling_owner=sampling_owner,
                )
            else:
                sdk_usage_telemetry.run_metered(
                    "files.write",
                    body,
                    (),
                    {},
                    identity={},
                    tally=tally,
                    sampling_owner=sampling_owner,
                )
    assert tally.snapshot() == {}
    assert calls == []


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("original", [ValueError("SDK failed"), asyncio.CancelledError()])
async def test_started_call_uses_its_snapshot_and_preserves_its_exception(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict[str, Any]],
    async_call: bool,
    original: BaseException,
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    cache = call_policy.SamplingPolicyOwner()
    cache.value = SamplingPolicy()
    cache.next_refresh = float("inf")
    reads: list[str] = []

    def current() -> SamplingPolicy:
        reads.append("read")
        return cache.read()

    def invalid() -> SamplingPolicy:
        raise TypeError("new invalid policy")

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return current()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    monkeypatch.setattr(call_policy, "_read_policy", invalid)
    nested: list[str] = []

    def body() -> None:
        cache.refresh()
        with pytest.raises(TypeError, match="new invalid policy"):
            sdk_usage_telemetry.run_metered(
                "inner.call",
                lambda: nested.append("must not run"),
                (),
                {},
                identity={},
                sampling_owner=sampling_owner,
            )
        raise original

    async def async_body() -> None:
        body()

    with pytest.raises(type(original)) as caught:
        if async_call:
            await sdk_usage_telemetry.run_metered_async(
                "outer.call",
                async_body,
                (),
                {},
                identity={},
                sampling_owner=sampling_owner,
            )
        else:
            sdk_usage_telemetry.run_metered(
                "outer.call",
                body,
                (),
                {},
                identity={},
                sampling_owner=sampling_owner,
            )
    assert caught.value is original
    assert reads == ["read", "read"]
    assert nested == []
    assert captured[0]["fn"] == "outer.call"
    with pytest.raises(TypeError, match="new invalid policy"):
        sdk_usage_telemetry.run_metered(
            "next.call",
            lambda: nested.append("must not run"),
            (),
            {},
            identity={},
            sampling_owner=sampling_owner,
        )
    assert nested == []


def test_direct_emit_propagates_policy_errors_without_creating_events(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict[str, Any]],
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    def invalid() -> SamplingPolicy:
        raise TypeError("invalid policy")

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return invalid()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    with pytest.raises(TypeError, match="invalid policy"):
        sdk_usage_telemetry.emit("files.write", identity={}, sampling_owner=sampling_owner)
    assert captured == []


def test_tally_counts_a_failed_public_entry_too(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    """Like its event, a call that raises still counts — it really executed."""
    _spy_emit(monkeypatch)

    def boom() -> None:
        raise ValueError("x")

    tally = SdkCallTally()
    with pytest.raises(ValueError, match="x"):
        sdk_usage_telemetry.run_metered(
            "ns.boom",
            boom,
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )
    assert tally.snapshot() == {"ns.boom": 1}


def test_tally_counts_each_public_entry(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    """Internal public SDK fan-out is a separate entry in the execution tally."""
    _spy_emit(monkeypatch)

    def inner() -> str:
        return sdk_usage_telemetry.run_metered(
            "ns.inner",
            lambda: "inner",
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )

    tally = SdkCallTally()
    sdk_usage_telemetry.run_metered(
        "ns.outer",
        inner,
        (),
        {},
        identity={},
        tally=tally,
        sampling_owner=sampling_owner,
    )
    assert tally.snapshot() == {"ns.inner": 1, "ns.outer": 1}


def test_calls_keep_their_explicit_tally_owner(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    _spy_emit(monkeypatch)
    sdk_usage_telemetry.run_metered(
        "ns.fn", lambda: "ok", (), {}, identity={}, sampling_owner=sampling_owner
    )  # framework-internal: no tally
    first = SdkCallTally()
    sdk_usage_telemetry.run_metered(
        "a.fn",
        lambda: "ok",
        (),
        {},
        identity={},
        tally=first,
        sampling_owner=sampling_owner,
    )
    second = SdkCallTally()
    sdk_usage_telemetry.run_metered(
        "b.fn",
        lambda: "ok",
        (),
        {},
        identity={},
        tally=second,
        sampling_owner=sampling_owner,
    )
    assert first.snapshot() == {"a.fn": 1}
    assert second.snapshot() == {"b.fn": 1}


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
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    async_call: bool,
    sampling_owner: call_policy.SamplingPolicyOwner,
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
    tally = SdkCallTally()
    with pytest.raises(error_type) as raised:
        if async_call:
            asyncio.run(
                sdk_usage_telemetry.run_metered_async(
                    "capture.call",
                    async_body,
                    (),
                    {},
                    identity={},
                    tally=tally,
                    sampling_owner=sampling_owner,
                )
            )
        else:
            sdk_usage_telemetry.run_metered(
                "capture.call",
                body,
                (),
                {},
                identity={},
                tally=tally,
                sampling_owner=sampling_owner,
            )
    assert raised.value is failure
    assert executed == [] and emitted == [] and tally.snapshot() == {}


def test_no_local_participant_uses_the_real_optional_gate(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    from base.agents.impersonation import manifest

    with manifest.admitted_local_sdk_call() as admission:
        assert admission is None
    emitted = _spy_emit(monkeypatch)

    def body() -> str:
        return "without-participant"

    assert (
        sdk_usage_telemetry.run_metered(
            "capture.call",
            body,
            (),
            {},
            identity={},
            sampling_owner=sampling_owner,
        )
        == "without-participant"
    )
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
call_policy.policy = lambda owner: SamplingPolicy()
telemetry.emit = lambda *args, **kwargs: executed.append('emit')

class BrokenCapture(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'base.agents.impersonation.manifest':
            raise failure

sys.meta_path.insert(0, BrokenCapture())
try:
    telemetry.run_metered('capture.call', lambda: executed.append('body'), (), {}, identity={},
                          sampling_owner=call_policy.SamplingPolicyOwner())
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
