"""Unit tests for agent/sdk_metering.py — the per-call SDK usage recorder.

The recorder wraps every public `ava.*` callable to emit one `sdk_call` event per
top-level invocation (counted by shared.metrics.sdk_usage). These tests pin the two
things that make it safe to bolt onto the whole SDK surface: it is byte-for-byte
transparent to `ava.help` / signatures, and it is a pure side channel over the call
(records once, at the top level, and never perturbs args / return / exceptions).
"""

from __future__ import annotations

import contextlib
import inspect
import io
from collections.abc import Iterator

import pytest

import ava
from agent import sdk_metering
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
    # ava.skills' __all_for_ava__ is the live skill index, whose entries are served
    # by module __getattr__ — the static walk resolves none of them, so nothing under
    # skills. is wrapped (skill use is attributed by skill_invoked events instead).
    # Dynamic MCP server proxies are never recursed into either.
    assert not any(fq.startswith("skills.") for fq in fqs)
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
    call time, and (like the rest) only records inside agent code."""
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
    assert calls == []


def test_install_wraps_and_restores_mcp_call_funnel() -> None:
    """install()/uninstall() wrap the ava.mcps._call_raw funnel so dynamic MCP tool
    calls are metered, and restore it on teardown."""
    import ava.mcps

    before = ava.mcps._call_raw
    sdk_metering.install()
    try:
        assert ava.mcps._call_raw is not before
        assert ava.mcps._call_raw in sdk_metering._RECORDERS
    finally:
        sdk_metering.uninstall()
    assert ava.mcps._call_raw is before
