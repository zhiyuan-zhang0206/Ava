"""Unit tests for ava/sdk_surface/metering.py — the per-call SDK usage recorder.

The recorder wraps every public `ava.*` callable to emit one `sdk_call` event per
public entry (summed by the Grafana call-frequency ranking). These tests pin the two
things that make it safe to bolt onto the whole SDK surface: it is byte-for-byte
transparent to `ava.help` / signatures, and records each entry independently.
Emitter failures preserve an already-raised body exception as the primary outcome.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import sys
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

import ava
from ava.sdk_surface import install, metering
from base.agents.context import AvaContext
from base.agents.context.identity import ExternalLease
from base.agents.sdk import call_policy
from base.agents.sdk import telemetry as sdk_usage_telemetry
from base.agents.sdk.tally import SdkCallTally
from base.packages.plugins.extensions import (
    ExtensionRegistry,
    PluginContributions,
    SdkMember,
    SdkNamespace,
    SdkWrap,
)
from tests.fixtures.pin_agent import pin_agent


@contextlib.contextmanager
def _execution_tally() -> Generator[SdkCallTally, None, None]:
    """A test execution explicitly owns its SDK tally through the local context."""
    previous = getattr(ava, "context", None)
    tally = SdkCallTally()
    ava.bind_context(replace(previous or AvaContext(), sdk_calls=tally))
    try:
        yield tally
    finally:
        if previous is None:
            ava.unbind_context()
        else:
            ava.bind_context(previous)


def _spy_emit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object], float | None]]:
    """Capture (fn, detail, duration) for each emitted sdk_call event."""
    calls: list[tuple[str, dict[str, object], float | None]] = []

    def emit(
        fn: str,
        detail: Mapping[str, object] | None = None,
        duration: float | None = None,
        *,
        identity: Mapping[str, Any],
        sampling_policy: call_policy.SamplingPolicy | None = None,
    ) -> None:
        assert sampling_policy is not None
        calls.append((fn, dict(detail or {}), duration))

    monkeypatch.setattr(sdk_usage_telemetry, "emit", emit)
    return calls


@pytest.fixture(autouse=True)
def _valid_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(call_policy, "policy", call_policy.SamplingPolicy)


def _help(*targets: object) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ava.help(*targets)
    return buf.getvalue()


@pytest.fixture
def _installed() -> Iterator[None]:
    """Install the recorders over the real `ava` singleton, then restore — so a
    wrapped function never leaks into the rest of the suite."""
    ledger = metering.install()
    try:
        yield
    finally:
        metering.uninstall(ledger)


# ── transparency ──────────────────────────────────────────────────────────────


def test_help_is_byte_identical_across_install(
    monkeypatch: pytest.MonkeyPatch, model_installation: install.Installation
) -> None:
    """Acceptance for the transparency contract: metering must not change a single
    byte of what the agent sees via `ava.help`."""
    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)
    before_root = _help(ava)
    before_ns = _help(ava.files)
    before_fn = _help(ava.files.read)

    ledger = metering.install()
    try:
        assert _help(ava) == before_root
        assert _help(ava.files) == before_ns
        assert _help(ava.files.read) == before_fn
    finally:
        metering.uninstall(ledger)


def test_signature_and_identity_metadata_preserved() -> None:
    # Capture the pristine metadata, then install: name / module / doc / signature
    # must be unchanged (functools.wraps + __wrapped__ resolution).
    before_sig = inspect.signature(ava.files.read)
    before_doc = ava.files.read.__doc__
    ledger = metering.install()
    try:
        read = ava.files.read
        assert read.__name__ == "read"
        assert read.__module__ == "ava.files"
        assert read.__doc__ == before_doc
        assert inspect.signature(read) == before_sig
    finally:
        metering.uninstall(ledger)


def test_function_attached_members_survive(_installed: None) -> None:
    # ava.understand carries UnderstandError as a function attribute; the __dict__
    # copy in functools.wraps must keep it reachable after wrapping.
    assert isinstance(getattr(ava.understand, "UnderstandError", None), type)


def test_install_is_idempotent(_installed: None) -> None:
    once = ava.files.read
    metering.install()  # second install must not double-wrap
    assert ava.files.read is once


# ── enumeration ───────────────────────────────────────────────────────────────


def test_instrument_targets_selects_routines_not_classes_or_constants() -> None:
    fqs = {fq for _parent, _attr, fq in metering._instrument_targets()}
    # plain functions, nested-namespace functions, and top-level functions
    assert {"files.read", "shell.run", "shell.sessions.new", "self.compact", "understand"} <= fqs
    # ava.mcps has no list __all_for_ava__, but its own module helpers are still metered
    # via the dir() fallback (dynamic tool calls are metered separately at _call_raw).
    assert {"mcps.servers", "mcps.description", "mcps.help"} <= fqs
    # classes and constants exposed in __all_for_ava__ are never wrapped
    assert "agents.AgentRow" not in fqs  # a class
    assert "self.AGENT_ID" not in fqs  # a constant
    assert "memory.PATH" not in fqs  # a constant
    # ava.skills' __all_for_ava__ is the live skill index plus one real utility
    # (`read`). The index entries are served by module __getattr__ and the static
    # walk resolves none of them, so they are never wrapped (skill use is
    # attributed by skill_invoked events instead); `read` is a genuine routine
    # and belongs in the metered surface. Dynamic MCP server proxies are never
    # recursed into either.
    assert "skills.read" in fqs
    assert not any(fq.startswith("skills.") and fq != "skills.read" for fq in fqs)
    assert not any(fq.startswith("mcps.") and fq.count(".") > 1 for fq in fqs)


def test_instrument_targets_does_not_evaluate_raising_dynamic_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: ava.self.MACHINE_SPEC / SELF_MACHINE_NAME are served via module
    __getattr__ that computes machine identity and raises MachineNameMissing when unset
    (CI / isolated $AVA_HOME / schedule runner). The walk resolves members statically, so
    it never force-evaluates them — otherwise install() crashes load_extensions in the
    child (rc=1)."""
    import base.cluster.machine

    def _raise() -> str:
        raise base.cluster.machine.MachineNameMissing("machine name not set")

    monkeypatch.setattr(base.cluster.machine, "machine_name", _raise)
    # sanity: normal attribute access really does raise under this condition
    with pytest.raises(base.cluster.machine.MachineNameMissing):
        _ = ava.self.SELF_MACHINE_NAME

    fqs = {fq for _parent, _attr, fq in metering._instrument_targets()}
    assert "self.compact" in fqs  # real functions still enumerated
    assert "self.SELF_MACHINE_NAME" not in fqs  # dynamic constant skipped, not evaluated
    assert "self.MACHINE_SPEC" not in fqs


# ── recorder wrapping (agent side; call / emit logic is in test_sdk_telemetry) ───


def test_plugin_wrapped_signature_survives_and_counts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member already wrapped by a plugin (extra kwarg, custom __signature__) stays
    transparent under the recorder and still emits exactly one event."""
    calls = _spy_emit(monkeypatch)

    def core(a: int, b: int) -> tuple[int, int]:
        return (a, b)

    def plugin_wrapped(a: int, b: int, *, label: str | None = None) -> tuple[int, int]:
        return core(a, b)

    # mimic ava.sdk_surface.wraps._install_metadata: identity of the wrapped member + a
    # signature that advertises the plugin's added `label` kwarg.
    plugin_wrapped.__name__ = "spawn"
    plugin_wrapped.__module__ = "ava.agents"
    plugin_wrapped.__signature__ = inspect.signature(plugin_wrapped)  # type: ignore[attr-defined]

    rec = metering._make_recorder(plugin_wrapped, "agents.spawn")
    assert rec.__name__ == "spawn"
    assert rec.__module__ == "ava.agents"
    assert "label" in inspect.signature(rec).parameters
    with _execution_tally():
        assert rec(1, 2, label="x") == (1, 2)
    assert len(calls) == 1
    assert calls[0][:2] == ("agents.spawn", {})
    assert calls[0][2] is not None and calls[0][2] >= 0


def test_recorder_feeds_the_execution_tally(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapped surface bumps the execution's full tally (two calls count two),
    independent of the emit sampler."""
    _spy_emit(monkeypatch)
    rec = metering._make_recorder(lambda: "ok", "ns.fn")
    with _execution_tally() as tally:
        assert rec() == "ok"
        assert rec() == "ok"
    assert tally.snapshot() == {"ns.fn": 2}


def test_recorder_recognized_by_identity_not_copied_dict() -> None:
    """P3: ava.extend._install_metadata copies a wrapped callable's __dict__ onto its
    wrapper, so a plugin wrapper built over a recorder inherits the recorder's dict.
    is_recorder() must key off object identity (the marker points at the recorder itself), not the attribute's presence, or
    it would skip re-wrapping such a wrapper and leave the recorder buried inside."""
    rec = metering._make_recorder(lambda: None, "ns.fn")
    assert metering.is_recorder(rec)

    def plugin_wrapper() -> None:
        return rec()

    # replicate _install_metadata's `chained.__dict__.setdefault(k, v)` copy.
    for k, v in rec.__dict__.items():
        plugin_wrapper.__dict__.setdefault(k, v)
    assert not metering.is_recorder(plugin_wrapper)


def test_mcp_recorder_derives_fq_from_runtime_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP tools are dynamic, so the funnel recorder builds the fq from server/tool at
    call time, both inside and outside an execution tally."""
    calls = _spy_emit(monkeypatch)

    def _fake_call(server: str, tool: str, **_kw: object) -> dict[str, str]:
        return {"server": server, "tool": tool}

    rec = metering._make_mcp_recorder(_fake_call)
    with _execution_tally():
        assert rec("chrome", "navigate", url="x") == {"server": "chrome", "tool": "navigate"}
    assert len(calls) == 1
    assert calls[0][:2] == ("mcps.chrome.navigate", {})
    assert calls[0][2] is not None and calls[0][2] >= 0

    calls.clear()
    rec("chrome", "navigate")  # without an execution tally
    assert calls[0][0] == "mcps.chrome.navigate"


def test_a_surface_installed_without_the_agent_layer_is_seen() -> None:
    """A plugin load that never imports `agent.state` (the schedule runner's in-process script)
    still installs namespaces and members on the SDK surface. The gate must see them, or the
    fixture never drops them and they stay for the rest of the worker."""
    from tests.fixtures.plugin_registrations import plugin_registrations_present

    assert not plugin_registrations_present()
    registry = ExtensionRegistry(
        (
            (
                "probe",
                PluginContributions(
                    sdk_namespaces=(SdkNamespace("gate_probe", SimpleNamespace()),)
                ),
            ),
        )
    )
    install.install(registry)  # the autouse teardown drops it
    with patch.dict(sys.modules):
        sys.modules.pop("agent.state", None)
        assert plugin_registrations_present()
        assert "agent.state" not in sys.modules, "asking must not load the agent layer"


def test_uninstall_restores_from_the_install_record_without_a_namespace_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task #3426: teardown restores from the install() ledger, not a fresh
    namespace walk — the walk re-resolves dynamic member surfaces (the `ava.skills`
    index scans the skills tree and reads the install registry), which state a
    passing test arranged can poison after the test itself went green."""
    target = SimpleNamespace()

    def demo() -> str:
        return "ok"

    target.demo = demo

    def _stub_targets() -> list[tuple[object, str, str]]:
        return [(target, "demo", "demo")]

    monkeypatch.setattr(metering, "_instrument_targets", _stub_targets)
    ledger = metering.install()
    wrapped = target.demo
    assert wrapped is not demo
    assert metering.is_recorder(wrapped)

    def _no_walk() -> list[tuple[object, str, str]]:
        pytest.fail("uninstall() must not re-walk the namespace (task #3426)")

    monkeypatch.setattr(metering, "_instrument_targets", _no_walk)
    metering.uninstall(ledger)
    assert target.demo is demo


def test_teardown_survives_a_poisoned_dynamic_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task #3426 acceptance shape: arm metering first, then poison the skills
    surface (simulating the broken-registry state a test deliberately leaves
    behind); uninstall() must complete without touching the surface and restore
    every recorded pair."""
    ledger = metering.install()
    assert ledger
    recorded = list(ledger)

    def _poisoned(_self: object) -> list[str]:
        raise RuntimeError("simulated corrupt install registry")

    monkeypatch.setattr(type(ava.skills), "__all_for_ava__", property(_poisoned))  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(RuntimeError):
        metering._instrument_targets()  # the old teardown path explodes here

    metering.uninstall(ledger)
    # Completeness on the precise unit of the guarantee: no recorded pair still
    # holds a recorder.
    for parent, attr in recorded:
        assert not metering.is_recorder(getattr(parent, attr, None))
    assert not metering.is_recorder(ava.files.read)
    assert not metering.is_recorder(ava.mcps._call_raw)


@pytest.mark.asyncio
async def test_async_calls_measure_each_concurrent_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    calls = _spy_emit(monkeypatch)

    async def body(label: str) -> str:
        await asyncio.sleep(0)
        return label

    wrapped = metering._make_recorder(body, "plugin.async_call")
    assert inspect.iscoroutinefunction(wrapped)
    a, b = wrapped("a"), wrapped("b")
    assert calls == []
    assert await asyncio.gather(a, b) == ["a", "b"]
    assert [row[0] for row in calls] == ["plugin.async_call", "plugin.async_call"]
    assert all(row[2] is not None for row in calls)


@pytest.mark.parametrize("async_call", [False, True])
async def test_recorder_rejects_invalid_sampling_before_the_original_call(
    monkeypatch: pytest.MonkeyPatch, async_call: bool
) -> None:
    from base.agents.sdk import call_policy

    calls: list[str] = []

    def invalid() -> call_policy.SamplingPolicy:
        raise TypeError("invalid sampling configuration")

    def body() -> str:
        calls.append("side effect")
        return "ok"

    async def async_body() -> str:
        return body()

    monkeypatch.setattr(call_policy, "policy", invalid)
    original = async_body if async_call else body
    wrapped = metering._make_recorder(original, "plugin.write")
    assert inspect.signature(wrapped) == inspect.signature(original)
    with pytest.raises(TypeError, match="invalid sampling configuration"):
        if async_call:
            await wrapped()
        else:
            wrapped()
    assert calls == []


def test_borrowed_identity_is_stamped_on_external_sdk_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from base import telemetry
    from base.agents.sdk import call_policy

    rows: list[dict[str, Any]] = []

    def capture(*_args: Any, **kwargs: Any) -> None:
        rows.append(kwargs)

    def validate() -> int:
        pytest.fail("observational telemetry must not validate the lease")

    pin_agent(42, lease=ExternalLease(agent_id=99, validate=validate, config=lambda: None))
    monkeypatch.setattr(telemetry, "emit", capture)
    monkeypatch.setattr(call_policy, "policy", call_policy.SamplingPolicy)
    wrapped = metering._make_recorder(lambda: "ok", "files.read")
    assert wrapped() == "ok"
    assert rows[0]["agent_id"] == 99
    assert rows[0]["source"] == "agent:99"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_wrapper", [False, True])
async def test_plugin_wrap_preserves_awaited_single_event(
    monkeypatch: pytest.MonkeyPatch, async_wrapper: bool
) -> None:
    import asyncio
    from collections.abc import Awaitable

    calls = _spy_emit(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(sdk_usage_telemetry.time, "monotonic", lambda: clock[0])

    async def body() -> str:
        await asyncio.sleep(0)
        clock[0] += 2.0
        return "ok"

    def passthrough(inner: Callable[[], Awaitable[str]]) -> Awaitable[str]:
        return inner()

    async def awaited(inner: Callable[[], Awaitable[str]]) -> str:
        return await inner()

    registry = ExtensionRegistry(
        (
            (
                "async-test",
                PluginContributions(
                    sdk_members=(SdkMember("self", "review_async_test", body),),
                    sdk_wraps=(
                        SdkWrap(
                            "self.review_async_test", awaited if async_wrapper else passthrough
                        ),
                    ),
                ),
            ),
        )
    )
    try:
        install.install(registry)  # the recorder goes on last, over the plugin's wrap
        call = ava.self.review_async_test
        assert inspect.iscoroutinefunction(call)
        calls.clear()
        pending = call()
        assert calls == []
        assert await pending == "ok"
        assert calls == [("self.review_async_test", {}, 2.0)]
    finally:
        install.uninstall()


def test_install_does_not_evaluate_dynamic_namespace_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def dynamic_names() -> list[str]:
        pytest.fail("instrumentation must not load skills or discover remote MCP servers")

    monkeypatch.setattr(ava.skills, "__dir__", dynamic_names)
    monkeypatch.setattr(ava.mcps, "__dir__", dynamic_names)
    ledger = metering.install()
    metering.uninstall(ledger)


@pytest.mark.parametrize("wrapper", ["sync", "async", "mcp"])
async def test_invalid_identity_snapshot_prevents_sdk_body(
    monkeypatch: pytest.MonkeyPatch,
    wrapper: str,
) -> None:
    """Unknown provenance errors fail at admission instead of performing an unattributed action."""
    from base.agents.messages import external_caller

    failure = RuntimeError("identity unreadable")
    executed: list[str] = []

    def _boom() -> None:
        raise failure

    monkeypatch.setattr(external_caller, "external_caller", _boom)

    async def body() -> None:
        executed.append("async")

    with pytest.raises(RuntimeError) as caught:
        if wrapper == "async":
            await metering._make_recorder(body, "probe.async")()
        elif wrapper == "mcp":
            metering._make_mcp_recorder(lambda *_: executed.append("mcp"))("probe", "tool")
        else:
            metering._make_recorder(lambda: executed.append("sync"), "probe.sync")()
    assert caught.value is failure
    assert executed == []


async def test_awaited_calls_retain_their_entry_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later attachment cannot relabel an already-admitted async SDK call."""
    import asyncio

    from base import telemetry
    from base.agents.sdk import call_policy

    rows: list[dict[str, Any]] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(call_policy, "policy", call_policy.SamplingPolicy)

    def capture(*_args: Any, **kwargs: Any) -> None:
        rows.append(kwargs)

    monkeypatch.setattr(telemetry, "emit", capture)

    async def held() -> None:
        entered.set()
        await release.wait()

    pin_agent(41)
    first_tally = SdkCallTally()
    ava.bind_context(replace(ava.context, sdk_calls=first_tally))
    first = asyncio.create_task(metering._make_recorder(held, "probe.held")())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        pin_agent(42)
        second_tally = SdkCallTally()
        ava.bind_context(replace(ava.context, sdk_calls=second_tally))
        metering._make_recorder(lambda: None, "probe.next")()
        release.set()
        await first
    finally:
        release.set()
        await first
    assert first_tally.snapshot() == {"probe.held": 1}
    assert second_tally.snapshot() == {"probe.next": 1}
    assert [(row["attributes"]["fn"], row["agent_id"], row["source"]) for row in rows] == [
        ("probe.next", 42, "agent:42"),
        ("probe.held", 41, "agent:41"),
    ]


def test_public_fanout_counts_each_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_emit(monkeypatch)
    inner = metering._make_recorder(lambda: "done", "probe.inner")
    outer = metering._make_recorder(inner, "probe.outer")
    with _execution_tally() as tally:
        assert outer() == "done"
    assert tally.snapshot() == {"probe.inner": 1, "probe.outer": 1}
    assert [row[0] for row in calls] == ["probe.inner", "probe.outer"]


def test_recursive_public_entry_counts_each_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_emit(monkeypatch)

    def body(depth: int) -> int:
        return recursive(depth - 1) + 1 if depth else 0

    recursive = metering._make_recorder(body, "probe.recursive")
    with _execution_tally() as tally:
        assert recursive(2) == 2
    assert tally.snapshot() == {"probe.recursive": 3}
    assert len(calls) == 3


def test_plugin_reload_and_multi_call_wrap_have_one_final_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy_emit(monkeypatch)
    bodies: list[str] = []

    def body() -> int:
        bodies.append("body")
        return 1

    def twice(inner: Callable[[], int]) -> int:
        return inner() + inner()

    registry = ExtensionRegistry(
        (
            (
                "probe",
                PluginContributions(
                    sdk_members=(SdkMember("self", "multi_probe_test", body),),
                    sdk_wraps=(
                        SdkWrap("self.multi_probe_test", twice),
                        SdkWrap("self.multi_probe_test", twice),
                    ),
                ),
            ),
        )
    )
    for _ in range(2):
        install.install(registry)
        with _execution_tally() as tally:
            assert cast(Callable[[], int], ava.self.multi_probe_test)() == 4
        assert tally.snapshot() == {"self.multi_probe_test": 1}
        install.uninstall()
    assert bodies == ["body"] * 8
    assert [row[0] for row in calls] == ["self.multi_probe_test"] * 2
