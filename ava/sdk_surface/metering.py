"""Per-call SDK usage metering — the wrapping half of the SDK Usage instrumentation
(the call-local admission and emit path live in ``base/agents/sdk/telemetry.py``).

Every public ``ava.*`` callable is wrapped, once, by a transparent recorder installed by
the SDK installation (``ava.sdk_surface.install``) over the final surface — after the plugin
load, outermost of any plugin wrap layer. On each public entry the recorder (via ``run_metered``) writes one ``sdk_call``
event into the unified ``events`` stream, carrying the dotted function name in ``attributes.fn``
(``files.read``, ``shell.run``, ``self.compact``). the Grafana call-frequency ranking sums those events — replacing the old regex
scrape of code-event *source text*, which counted any ``ava.X(`` occurrence in comments,
string literals, docstrings, and agent-written example code (so ``ava._private(`` from a
private call, ``ava.bootDefaultActor(`` from a comment, and ``ava.x.y(`` from a
placeholder string all showed up as "SDK usage").

Transparency contract — the recorder MUST NOT perturb the SDK surface:
  - ``functools.wraps(original)`` copies name / qualname / module / doc / ``__dict__``
    and sets ``__wrapped__``, so ``inspect.signature`` (and therefore ``ava.help``)
    resolves the original signature byte-for-byte, and function-attached members
    (``ava.understand.UnderstandError``) survive via the ``__dict__`` copy.
  - A valid sampling policy is captured before each call executes. Invalid
    configuration or caller identity prevents execution; transient fetch failures
    may use its last valid snapshot. An emitter error after a successful body
    propagates without retrying the body.
    If the body already failed, its original exception and cause remain primary;
    the emitter failure is attached as an exception note, including cancellation
    and lifecycle exceptions.

Every public call is metered, including bare Python, CLI and external attachments.
Nested public entries count independently. The process-local execution context
holds an optional tally; recorders pass that owner explicitly to each call.
Static functions are wrapped by the installation; dynamic
MCP calls are wrapped at their common call funnel.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import Any

import ava
from base.agents.sdk.tally import SdkCallTally

# A recorder marks itself with a reference to itself. `is_recorder` tests that identity, so
# `install()` skips a target only when the current top callable *is* a recorder — robust to
# `ava.extend`'s wrap machinery, which copies a wrapped callable's __dict__: a plugin wrapper built
# over a recorder inherits the marker, but it points at the inner recorder, not at the wrapper.
_RECORDER_MARK = "__ava_recorder__"


def _recorder(fn: Callable[..., Any]) -> Callable[..., Any]:
    setattr(fn, _RECORDER_MARK, fn)
    return fn


def is_recorder(fn: object) -> bool:
    """Whether `fn` is itself a metering recorder (not a wrapper that merely copied one's attributes)."""
    return getattr(fn, _RECORDER_MARK, None) is fn


def _caller() -> tuple[dict[str, Any], SdkCallTally | None]:
    """Snapshot this call's provenance; invalid identity rejects admission."""
    from base.agents.messages.external_caller import external_caller

    bound = getattr(ava, "context", None)
    own = None if bound is None else bound.identity
    borrowed = own.lease.agent_id if own is not None and own.lease is not None else None
    agent_id = borrowed
    if agent_id is None and own is not None:
        agent_id = own.agent_id
    external = external_caller()
    actor = own.actor if own is not None else None
    source = f"agent:{agent_id}" if agent_id else (actor or "system")
    if external and borrowed is None:
        source = external.source()
    return {"agent_id": agent_id, "source": source}, None if bound is None else bound.sdk_calls


def _make_recorder(original: Callable[..., Any], fq: str) -> Callable[..., Any]:
    """Transparent proxy around ``original`` that meters the call as ``fq`` (the call /
    tally / emit logic lives in ``base.agents.sdk.telemetry.run_metered``)."""

    @functools.wraps(original)
    def recorder(*args: Any, **kwargs: Any) -> Any:
        from base.agents.sdk.telemetry import run_metered

        identity, tally = _caller()
        return run_metered(fq, original, args, kwargs, identity=identity, tally=tally)

    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def async_recorder(*args: Any, **kwargs: Any) -> Any:
            from base.agents.sdk import telemetry as sdk_usage_telemetry

            identity, tally = _caller()
            return await sdk_usage_telemetry.run_metered_async(
                fq, original, args, kwargs, identity=identity, tally=tally
            )

        return _recorder(async_recorder)

    return _recorder(recorder)


# ava.mcps exposes tools dynamically (no list `__all_for_ava__`), so the namespace walk cannot
# reach them. Every MCP tool call — text form `ava.mcps.<server>.<tool>(...)` and the
# `raw` form — funnels through `ava.mcps._call_raw(server, tool, ...)`, so one wrapper
# there meters them all, keyed by the runtime server + tool.
_MCP_CALL_FUNNEL = "_call_raw"


def _make_mcp_recorder(original: Callable[..., Any]) -> Callable[..., Any]:
    """Recorder for the MCP call funnel — derives the fq from the runtime args."""

    @functools.wraps(original)
    def recorder(server: str, tool: str, *args: Any, **kwargs: Any) -> Any:
        from base.agents.sdk.telemetry import run_metered

        identity, tally = _caller()
        return run_metered(
            f"mcps.{server}.{tool}",
            original,
            (server, tool, *args),
            kwargs,
            identity=identity,
            tally=tally,
        )

    return _recorder(recorder)


def _instrument_targets() -> list[tuple[Any, str, str]]:
    """Walk the ``ava`` namespace tree → ``(parent, attr, fq)`` for every public callable.

    ``fq`` is the dotted path under ``ava`` (``files.read``, ``shell.sessions.new``,
    ``understand``) — the key space the metric reports. Recurses into sub-namespaces
    (modules / SimpleNamespace); targets routines; skips classes, constants, and
    dynamic namespaces without an ``__all_for_ava__``.
    """
    targets: list[tuple[Any, str, str]] = []
    seen: set[int] = set()

    def walk(container: Any, prefix: str) -> None:
        if id(container) in seen:
            return
        seen.add(id(container))
        # agent_visible_names is the single source of truth for the agent
        # surface (help / SDK-expand / doc-lint share it). It reads
        # __all_for_ava__ statically so a dynamic proxy surface is never
        # force-evaluated; falls back to a module's own public routines
        # (ava.mcps) or a SimpleNamespace's public vars.
        for name in ava.agent_visible_names(container):
            # getattr_static, never getattr: a dynamically-served member (e.g.
            # ava.self.MACHINE_SPEC, computed via module __getattr__) must not be
            # force-evaluated — it can raise (MachineNameMissing when unset) and crash
            # install() -> load_extensions. Statically-resolvable functions and
            # submodules (the only things we wrap / recurse) are all real attributes.
            attr = inspect.getattr_static(container, name, None)
            if attr is None:
                continue
            fq = f"{prefix}{name}"
            if inspect.isroutine(attr):
                targets.append((container, name, fq))
            elif isinstance(attr, (ModuleType, SimpleNamespace)):
                walk(attr, f"{fq}.")

    walk(ava, "")
    return targets


def install() -> tuple[tuple[Any, str], ...]:
    """Wrap every public ``ava.*`` callable with the recording proxy. Idempotent.

    Called by ``ava.sdk_surface.install`` after plugins load, so plugin namespaces /
    members / declared wrap layers are all present and get metered too (the
    recorder sits outermost of any plugin wrap). Re-running only wraps targets whose
    current top callable is not already a recorder, so it is safe to call on every
    plugin reload — a newly plugin-wrapped target gets a fresh outermost recorder.

    Returns the restore ledger: the ``(parent, attr)`` pairs this call actually wrapped,
    in wrap order. The installation carries it; ``uninstall(ledger)`` restores from it.
    """
    wrapped: list[tuple[Any, str]] = []
    for parent, attr, fq in _instrument_targets():
        current = getattr(parent, attr, None)
        if current is None or is_recorder(current):
            continue
        setattr(parent, attr, _make_recorder(current, fq))
        wrapped.append((parent, attr))

    mcps_mod = getattr(ava, "mcps", None)
    if mcps_mod is not None:
        funnel = getattr(mcps_mod, _MCP_CALL_FUNNEL, None)
        if callable(funnel) and not is_recorder(funnel):
            setattr(mcps_mod, _MCP_CALL_FUNNEL, _make_mcp_recorder(funnel))
            wrapped.append((mcps_mod, _MCP_CALL_FUNNEL))
    return tuple(wrapped)


def uninstall(ledger: tuple[tuple[Any, str], ...]) -> None:
    """Restore every metered target in `ledger` to the callable the recorder wraps — test
    teardown, so a test that installs the recorders does not leak them into the
    shared ``ava`` singleton the rest of the suite imports.

    ``ledger`` is the record ``install()`` returned — the ``(parent, attr)`` pairs it
    actually wrapped, in wrap order; the Installation carries it
    (``install.installed().metered``), and the suite's autouse restore
    (``tests/fixtures/guards.py``) passes that plus any direct ``install()`` return.

    Restore from the record, never by re-walking the namespace: the walk resolves
    dynamic member surfaces (``ava.skills``'s index scans the skills tree and reads
    the install registry) and must not run at teardown, where a test's
    deliberately-broken state can make it raise — the failure shape of task #3426
    (macOS local: 8 teardowns exploded after the test bodies had gone green).
    """
    # O(1) early-out: the ledger is empty when nothing was wrapped. Without it every
    # per-test teardown would pay for a full restore pass that could only find nothing.
    if not ledger:
        return
    for parent, attr in ledger:
        current = getattr(parent, attr, None)
        if current is not None and is_recorder(current):
            setattr(parent, attr, current.__wrapped__)
