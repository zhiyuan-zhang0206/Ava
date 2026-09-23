"""Unit tests for ava/_sdk_metering.py — the per-call SDK usage recorder.

The recorder wraps every public `ava.*` callable to emit one `sdk_call` event per
top-level invocation (counted by the `sdk_usage` metric). These tests pin the two
things that make it safe to bolt onto the whole SDK surface: it is byte-for-byte
transparent to `ava.help` / signatures, and it is a pure side channel over the call
(records once, at the top level, and never perturbs args / return / exceptions).
"""

from __future__ import annotations

import contextlib
import inspect
import io
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import ava
from ava import _sdk_metering as sdk_metering
from shared import sdk_telemetry


def _spy_emit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object], float | None]]:
    """Capture (fn, detail, duration) for each emitted sdk_call event."""
    calls: list[tuple[str, dict[str, object], float | None]] = []
    monkeypatch.setattr(
        sdk_telemetry,
        "emit",
        lambda fn, detail=None, duration=None: calls.append((fn, dict(detail or {}), duration)),  # pyright: ignore[reportUnknownArgumentType]
    )
    return calls


def _help(*targets: object) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ava.help(*targets)
    return buf.getvalue()


@pytest.fixture
def _installed() -> Iterator[None]:
    """Install the recorders over the real `ava` singleton, then restore — so a
    wrapped function never leaks into the rest of the suite."""
    sdk_metering.install()
    try:
        yield
    finally:
        sdk_metering.uninstall()


# ── transparency ──────────────────────────────────────────────────────────────


def test_help_is_byte_identical_across_install() -> None:
    """Acceptance for the transparency contract: metering must not change a single
    byte of what the agent sees via `ava.help`."""
    before_root = _help(ava)
    before_ns = _help(ava.files)
    before_fn = _help(ava.files.read)

    sdk_metering.install()
    try:
        assert _help(ava) == before_root
        assert _help(ava.files) == before_ns
        assert _help(ava.files.read) == before_fn
    finally:
        sdk_metering.uninstall()


def test_signature_and_identity_metadata_preserved() -> None:
    # Capture the pristine metadata, then install: name / module / doc / signature
    # must be unchanged (functools.wraps + __wrapped__ resolution).
    before_sig = inspect.signature(ava.files.read)
    before_doc = ava.files.read.__doc__
    sdk_metering.install()
    try:
        read = ava.files.read
        assert read.__name__ == "read"
        assert read.__module__ == "ava.files"
        assert read.__doc__ == before_doc
        assert inspect.signature(read) == before_sig
    finally:
        sdk_metering.uninstall()


def test_function_attached_members_survive(_installed: None) -> None:
    # ava.understand carries UnderstandError as a function attribute; the __dict__
    # copy in functools.wraps must keep it reachable after wrapping.
    assert isinstance(getattr(ava.understand, "UnderstandError", None), type)


def test_install_is_idempotent(_installed: None) -> None:
    once = ava.files.read
    sdk_metering.install()  # second install must not double-wrap
    assert ava.files.read is once


# ── enumeration ───────────────────────────────────────────────────────────────


def test_instrument_targets_selects_routines_not_classes_or_constants() -> None:
    fqs = {fq for _parent, _attr, fq in sdk_metering._instrument_targets()}
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
    it never force-evaluates them — otherwise install() crashes _load_extensions in the
    child (rc=1)."""
    import shared.machine

    def _raise() -> str:
        raise shared.machine.MachineNameMissing("machine name not set")

    monkeypatch.setattr(shared.machine, "machine_name", _raise)
    # sanity: normal attribute access really does raise under this condition
    with pytest.raises(shared.machine.MachineNameMissing):
        _ = ava.self.SELF_MACHINE_NAME

    fqs = {fq for _parent, _attr, fq in sdk_metering._instrument_targets()}
    assert "self.compact" in fqs  # real functions still enumerated
    assert "self.SELF_MACHINE_NAME" not in fqs  # dynamic constant skipped, not evaluated
    assert "self.MACHINE_SPEC" not in fqs


# ── recorder wrapping (agent side; frame / emit logic is in test_sdk_telemetry) ───


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

    # mimic ava._extend._install_metadata: identity of the wrapped member + a
    # signature that advertises the plugin's added `label` kwarg.
    plugin_wrapped.__name__ = "spawn"
    plugin_wrapped.__module__ = "ava.agents"
    plugin_wrapped.__signature__ = inspect.signature(plugin_wrapped)  # type: ignore[attr-defined]

    rec = sdk_metering._make_recorder(plugin_wrapped, "agents.spawn")
    assert rec.__name__ == "spawn"
    assert rec.__module__ == "ava.agents"
    assert "label" in inspect.signature(rec).parameters
    with sdk_telemetry.recording():
        assert rec(1, 2, label="x") == (1, 2)
    assert len(calls) == 1
    assert calls[0][:2] == ("agents.spawn", {})
    assert calls[0][2] is not None and calls[0][2] >= 0


def test_recorder_feeds_the_recording_tally(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapped surface bumps the recording's full tally (two calls count two),
    independent of the emit sampler."""
    _spy_emit(monkeypatch)
    rec = sdk_metering._make_recorder(lambda: "ok", "ns.fn")
    with sdk_telemetry.recording() as tally:
        assert rec() == "ok"
        assert rec() == "ok"
    assert tally == {"ns.fn": 2}


def test_recorder_recognized_by_identity_not_copied_dict() -> None:
    """P3: ava.extend._install_metadata copies a wrapped callable's __dict__ onto its
    wrapper, so a plugin wrapper built over a recorder inherits the recorder's dict.
    install() must key off object identity (the _RECORDERS set), not an attribute, or
    it would skip re-wrapping such a wrapper and leave the recorder buried inside."""
    rec = sdk_metering._make_recorder(lambda: None, "ns.fn")
    assert rec in sdk_metering._RECORDERS

    def plugin_wrapper() -> None:
        return rec()

    # replicate _install_metadata's `chained.__dict__.setdefault(k, v)` copy.
    for k, v in rec.__dict__.items():
        plugin_wrapper.__dict__.setdefault(k, v)
    assert plugin_wrapper not in sdk_metering._RECORDERS


def test_mcp_recorder_derives_fq_from_runtime_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP tools are dynamic, so the funnel recorder builds the fq from server/tool at
    call time, both inside and outside an execution tally."""
    calls = _spy_emit(monkeypatch)

    def _fake_call(server: str, tool: str, **_kw: object) -> dict[str, str]:
        return {"server": server, "tool": tool}

    rec = sdk_metering._make_mcp_recorder(_fake_call)
    with sdk_telemetry.recording():
        assert rec("chrome", "navigate", url="x") == {"server": "chrome", "tool": "navigate"}
    assert len(calls) == 1
    assert calls[0][:2] == ("mcps.chrome.navigate", {})
    assert calls[0][2] is not None and calls[0][2] >= 0

    calls.clear()
    rec("chrome", "navigate")  # outside recording()
    assert calls[0][0] == "mcps.chrome.navigate"


def test_install_wraps_and_restores_mcp_call_funnel() -> None:
    """install()/uninstall() wrap the ava.mcps._call_raw funnel so dynamic MCP tool
    calls are metered, and restore it on teardown."""
    import ava.mcps

    # `ava` is a process-global singleton and `_load_extensions` installs the
    # recorders as a side effect, so any earlier test in this xdist worker that
    # loaded plugins leaves the funnel already wrapped — install() then correctly
    # no-ops and the wrap assertion below reads as a failure. Which tests share a
    # worker is not deterministic under `-n`, so take a clean baseline first.
    sdk_metering.uninstall()
    before = ava.mcps._call_raw
    sdk_metering.install()
    try:
        assert ava.mcps._call_raw is not before
        assert ava.mcps._call_raw in sdk_metering._RECORDERS
    finally:
        sdk_metering.uninstall()
    assert ava.mcps._call_raw is before


def test_a_plugin_load_is_undone_by_the_autouse_teardown(request: pytest.FixtureRequest) -> None:
    """Issue #83: `_load_extensions()` meters the process-global `ava` singleton as a
    side effect and nothing used to put it back, so one plugin-loading test silently
    rewrote the callables every later test in that xdist worker saw.

    The autouse `_restore_sdk_metering` in `tests/conftest.py` is what closes that.
    It runs after this test body, where a self-test cannot observe it, so the two
    halves are pinned separately: the fixture is wired onto every test, and its one
    action reverses a *real* `_load_extensions()` — not just the hand-built
    `install()` the test above covers.
    """
    import ava.mcps
    from agent.graph import _build
    from ava._sdk_metering import _RECORDERS

    assert "_restore_sdk_metering" in request.fixturenames

    sdk_metering.uninstall()
    bare_funnel = ava.mcps._call_raw

    _build._load_extensions()
    metered = {fq for p, a, fq in sdk_metering._instrument_targets() if getattr(p, a) in _RECORDERS}
    assert metered, "the leak this guards is gone"
    assert ava.mcps._call_raw in _RECORDERS

    sdk_metering.uninstall()  # the fixture's action, made observable
    assert ava.mcps._call_raw is bare_funnel
    # The whole surface, not just the funnel: a later test asserting on identity or
    # on call counts through a wrapped path must see no recorder anywhere.
    assert not [
        fq for p, a, fq in sdk_metering._instrument_targets() if getattr(p, a) in _RECORDERS
    ]


def test_uninstall_restores_from_the_install_record_without_a_namespace_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task #3426: teardown restores from the install() ledger, not a fresh
    namespace walk — the walk re-resolves dynamic member surfaces (the `ava.skills`
    index scans the skills tree and reads the install registry), which state a
    passing test arranged can poison after the test itself went green."""
    sdk_metering.uninstall()  # clean baseline, as in the sibling tests above
    target = SimpleNamespace()

    def demo() -> str:
        return "ok"

    target.demo = demo

    def _stub_targets() -> list[tuple[object, str, str]]:
        return [(target, "demo", "demo")]

    monkeypatch.setattr(sdk_metering, "_instrument_targets", _stub_targets)
    sdk_metering.install()
    wrapped = target.demo
    assert wrapped is not demo
    assert wrapped in sdk_metering._RECORDERS

    def _no_walk() -> list[tuple[object, str, str]]:
        pytest.fail("uninstall() must not re-walk the namespace (task #3426)")

    monkeypatch.setattr(sdk_metering, "_instrument_targets", _no_walk)
    sdk_metering.uninstall()
    assert target.demo is demo


def test_teardown_survives_a_poisoned_dynamic_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task #3426 acceptance shape: arm metering first, then poison the skills
    surface (simulating the broken-registry state a test deliberately leaves
    behind); uninstall() must complete without touching the surface and restore
    every recorded pair."""
    sdk_metering.uninstall()
    before = set(sdk_metering._RECORDERS)
    sdk_metering.install()
    assert sdk_metering._RECORDERS
    recorded = list(sdk_metering._WRAPPED)

    def _poisoned(_self: object) -> list[str]:
        raise RuntimeError("simulated corrupt install registry")

    monkeypatch.setattr(type(ava.skills), "__all_for_ava__", property(_poisoned))  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(RuntimeError):
        sdk_metering._instrument_targets()  # the old teardown path explodes here

    sdk_metering.uninstall()
    # Completeness on the precise unit of the guarantee: no recorded pair still
    # holds a recorder. (The set itself may retain recorders that
    # `ava._extend._ORIGINALS` captured before this test armed metering — that
    # retention predates task #3426 and is not this fix's business.)
    for parent, attr in recorded:
        assert getattr(parent, attr, None) not in sdk_metering._RECORDERS
    assert ava.files.read not in sdk_metering._RECORDERS
    assert ava.mcps._call_raw not in sdk_metering._RECORDERS
    assert set(sdk_metering._RECORDERS) <= before


@pytest.mark.asyncio
async def test_async_calls_measure_execution_and_isolate_concurrent_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    calls = _spy_emit(monkeypatch)

    async def body(label: str) -> str:
        sdk_telemetry.annotate(label=label)
        await asyncio.sleep(0)
        return label

    wrapped = sdk_metering._make_recorder(body, "plugin.async_call")
    assert inspect.iscoroutinefunction(wrapped)
    a, b = wrapped("a"), wrapped("b")
    assert calls == []
    assert await asyncio.gather(a, b) == ["a", "b"]
    assert [row[1] for row in calls] == [{"label": "a"}, {"label": "b"}]


def test_plain_python_import_installs_sdk_events(tmp_path: Path) -> None:
    import json
    import os
    import subprocess
    import sys

    target = tmp_path / "input.txt"
    target.write_text("hello")
    code = """
import json, sys
import ava
from shared import telemetry, sdk_call_policy
sdk_call_policy.policy = sdk_call_policy.SamplingPolicy
rows = []
telemetry.emit = lambda *args, **kwargs: rows.append(kwargs)
assert ava.files.read(sys.argv[1]) == "hello"
print(json.dumps(rows))
"""
    result = subprocess.run(  # noqa: S603 — fixed Python code and an isolated fixture path
        [sys.executable, "-c", code, str(target)],
        env={**os.environ, "AVA_AGENT_ID": "42"},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["agent_id"] == 42
    assert rows[0]["attributes"]["fn"] == "files.read"
    assert rows[0]["attributes"]["sample_rate"] == 1


def test_borrowed_identity_is_stamped_on_external_sdk_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any

    from ava import _boot
    from shared import sdk_call_policy, telemetry

    rows: list[dict[str, Any]] = []

    def capture(*_args: Any, **kwargs: Any) -> None:
        rows.append(kwargs)

    def validate() -> int:
        pytest.fail("observational telemetry must not validate the lease")

    monkeypatch.setattr(_boot, "_external_identity", validate)
    monkeypatch.setattr(_boot, "_external_agent_id", 99)
    monkeypatch.setattr(_boot, "_agent_id", 42)
    monkeypatch.setattr(telemetry, "emit", capture)
    monkeypatch.setattr(sdk_call_policy, "policy", sdk_call_policy.SamplingPolicy)
    wrapped = sdk_metering._make_recorder(lambda: "ok", "files.read")
    assert wrapped() == "ok"
    assert rows[0]["agent_id"] == 99
    assert rows[0]["source"] == "agent:99"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_wrapper", [False, True])
async def test_plugin_wrap_preserves_awaited_single_event(
    monkeypatch: pytest.MonkeyPatch, async_wrapper: bool
) -> None:
    import asyncio
    from collections.abc import Awaitable, Callable

    from ava import _extend
    from shared.plugin_context import PluginContext

    calls = _spy_emit(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(sdk_telemetry.time, "monotonic", lambda: clock[0])

    async def body() -> str:
        await asyncio.sleep(0)
        clock[0] += 2.0
        sdk_telemetry.annotate(body=True)
        return "ok"

    def passthrough(inner: Callable[[], Awaitable[str]]) -> Awaitable[str]:
        return inner()

    async def awaited(inner: Callable[[], Awaitable[str]]) -> str:
        return await inner()

    ava.register_namespace_member("self", "review_async_test", body)
    try:
        sdk_metering.install()
        with PluginContext("async-test"):
            ava.extend.wrap("self.review_async_test", awaited if async_wrapper else passthrough)
        sdk_metering.install()
        call = ava.self.review_async_test
        assert inspect.iscoroutinefunction(call)
        calls.clear()
        pending = call()
        assert calls == []
        assert await pending == "ok"
        assert calls == [("self.review_async_test", {"body": True}, 2.0)]
    finally:
        sdk_metering.uninstall()
        _extend.clear_wraps()
        ava.clear_registered_namespaces()


def test_install_does_not_evaluate_dynamic_namespace_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def dynamic_names() -> list[str]:
        pytest.fail("instrumentation must not load skills or discover remote MCP servers")

    monkeypatch.setattr(ava.skills, "__dir__", dynamic_names)
    monkeypatch.setattr(ava.mcps, "__dir__", dynamic_names)
    sdk_metering.install()
    sdk_metering.uninstall()
