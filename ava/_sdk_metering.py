"""Per-call SDK usage metering — the wrapping half of the SDK Usage instrumentation
(the runtime state + emit path live in ``shared/sdk_telemetry.py``, kept there so an
SDK function body in the ``ava`` layer can ``annotate()`` its own call).

Every public ``ava.*`` callable is wrapped, once, by a transparent recorder installed
at SDK import and again at agent-graph build time after plugins load. On each top-level call the recorder (via ``run_metered``) writes one ``sdk_call``
event into the unified ``events`` stream, carrying the dotted function name in ``attributes.fn``
(``files.read``, ``shell.run``, ``self.compact``) plus any ``detail`` the call
annotated. ``shared.metrics_aggregate``'s sdk_usage counts those events — replacing the old regex
scrape of code-event *source text*, which counted any ``ava.X(`` occurrence in comments,
string literals, docstrings, and agent-written example code (so ``ava._private(`` from a
private call, ``ava.bootDefaultActor(`` from a comment, and ``ava.x.y(`` from a
placeholder string all showed up as "SDK usage").

Transparency contract — the recorder MUST NOT perturb the SDK surface:
  - ``functools.wraps(original)`` copies name / qualname / module / doc / ``__dict__``
    and sets ``__wrapped__``, so ``inspect.signature`` (and therefore ``ava.help``)
    resolves the original signature byte-for-byte, and function-attached members
    (``ava.understand.UnderstandError``) survive via the ``__dict__`` copy.
  - Pure side channel: metering failures are swallowed and never change the call's
    arguments, return value, or exceptions. Lifecycle exceptions
    (``AgentTermination`` / ``AgentRestart``) propagate untouched.

Every public call is metered, including bare Python, CLI and external attachments.
Only outermost calls count, so SDK-internal fan-out does not inflate usage.
``recording()`` collects an optional per-execution tally; it is not an event gate.
Static functions are wrapped at SDK import and again after plugin loading; dynamic
MCP calls are wrapped at their common call funnel.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import weakref
from collections.abc import Callable, Generator
from types import ModuleType, SimpleNamespace
from typing import Any

import ava
from ava import _boot

# Identity set of live recorder objects. install() skips a target only when the
# current top callable *is* one of these — robust to `ava.extend`'s wrap machinery,
# which copies a wrapped callable's __dict__ (so a plain attribute marker would leak
# onto a plugin wrapper built over a recorder and make install() wrongly skip it).
_RECORDERS: weakref.WeakSet[Callable[..., Any]] = weakref.WeakSet()

# Restore ledger (task #3426): the ``(parent, attr)`` pairs install() actually
# wrapped, in wrap order. uninstall() restores from this record rather than
# re-walking the namespace — the walk resolves dynamic member surfaces
# (``ava.skills``'s ``__all_for_ava__`` scans the skills tree and reads the
# install registry), so teardown would otherwise be exposed to state a passing
# test deliberately arranged (macOS local: 8 teardowns exploded after green).
_WRAPPED: list[tuple[Any, str]] = []


@contextlib.contextmanager
def _caller() -> Generator[None, None, None]:
    """Snapshot provenance before the call; metering never changes SDK behavior."""
    from shared import sdk_telemetry
    from shared.external_caller import external_caller

    identity = {}
    with contextlib.suppress(Exception):
        _boot._try_establish_from_env()
        borrowed = _boot._external_agent_id
        turn = _boot.current_turn_agent_id()
        agent_id = borrowed if borrowed is not None else turn
        if agent_id is None:
            agent_id = _boot._agent_id
        external = external_caller()
        source = f"agent:{agent_id}" if agent_id else (_boot._actor or "system")
        if external and borrowed is None and turn is None:
            source = external.source()
        identity = {
            "agent_id": agent_id,
            "source": source,
        }
    token = sdk_telemetry._identity.set(identity)
    try:
        yield
    finally:
        sdk_telemetry._identity.reset(token)


def _make_recorder(original: Callable[..., Any], fq: str) -> Callable[..., Any]:
    """Transparent proxy around ``original`` that meters the call as ``fq`` (the frame /
    tally / emit logic lives in ``shared.sdk_telemetry.run_metered``)."""

    @functools.wraps(original)
    def recorder(*args: Any, **kwargs: Any) -> Any:
        from shared.sdk_telemetry import run_metered

        with _caller():
            return run_metered(fq, original, args, kwargs)

    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def async_recorder(*args: Any, **kwargs: Any) -> Any:
            from shared import sdk_telemetry

            with _caller():
                return await sdk_telemetry.run_metered_async(fq, original, args, kwargs)

        _RECORDERS.add(async_recorder)
        return async_recorder

    _RECORDERS.add(recorder)
    return recorder


# ava.mcps exposes tools dynamically (no list `__all_for_ava__`), so the namespace walk cannot
# reach them. Every MCP tool call — text form `ava.mcps.<server>.<tool>(...)` and the
# `raw` form — funnels through `ava.mcps._call_raw(server, tool, ...)`, so one wrapper
# there meters them all, keyed by the runtime server + tool.
_MCP_CALL_FUNNEL = "_call_raw"


def _make_mcp_recorder(original: Callable[..., Any]) -> Callable[..., Any]:
    """Recorder for the MCP call funnel — derives the fq from the runtime args."""

    @functools.wraps(original)
    def recorder(server: str, tool: str, *args: Any, **kwargs: Any) -> Any:
        from shared.sdk_telemetry import run_metered

        with _caller():
            return run_metered(f"mcps.{server}.{tool}", original, (server, tool, *args), kwargs)

    _RECORDERS.add(recorder)
    return recorder


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
            # install() -> _load_extensions. Statically-resolvable functions and
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


def install() -> None:
    """Wrap every public ``ava.*`` callable with the recording proxy. Idempotent.

    Called from ``_load_extensions`` after plugins load, so plugin namespaces /
    members / ``ava.extend.wrap`` layers are all present and get metered too (the
    recorder sits outermost of any plugin wrap). Re-running only wraps targets whose
    current top callable is not already a recorder, so it is safe to call on every
    plugin reload — a newly plugin-wrapped target gets a fresh outermost recorder.
    """
    for parent, attr, fq in _instrument_targets():
        current = getattr(parent, attr, None)
        if current is None or current in _RECORDERS:
            continue
        setattr(parent, attr, _make_recorder(current, fq))
        _WRAPPED.append((parent, attr))

    mcps_mod = getattr(ava, "mcps", None)
    if mcps_mod is not None:
        funnel = getattr(mcps_mod, _MCP_CALL_FUNNEL, None)
        if callable(funnel) and funnel not in _RECORDERS:
            setattr(mcps_mod, _MCP_CALL_FUNNEL, _make_mcp_recorder(funnel))
            _WRAPPED.append((mcps_mod, _MCP_CALL_FUNNEL))


def uninstall() -> None:
    """Restore every metered target to the callable the recorder wraps — test
    teardown, so a test that installs the recorders does not leak them into the
    shared ``ava`` singleton the rest of the suite imports.

    The suite calls this after every test (autouse ``_restore_sdk_metering`` in
    ``tests/conftest.py``), because ``install()`` is a side effect of
    ``_load_extensions()`` and is reached lazily on any ``ava.*`` miss — so merely
    touching the namespace metered it for every later test in the worker (issue #83).

    Restores from the ``install()`` record (``_WRAPPED``), never by re-walking the
    namespace: the walk resolves dynamic member surfaces (``ava.skills``'s index
    scans the skills tree and reads the install registry) and must not run at
    teardown, where a test's deliberately-broken state can make it raise — the
    failure shape of task #3426 (macOS local: 8 teardowns exploded after the test
    bodies had gone green).
    """
    # O(1) early-out: _RECORDERS is a WeakSet, and an installed recorder is held
    # strongly by the namespace it sits on, so an empty set proves nothing is
    # installed. Without it every per-test teardown would pay for a full restore
    # pass that could only find nothing.
    if not _RECORDERS:
        _WRAPPED.clear()
        return

    # Restore from the install() ledger, never by walking the namespace (task
    # #3426): the walk re-resolves dynamic member surfaces (`ava.skills` scans the
    # skills tree and reads the install registry), which test-arranged state can
    # poison. Every wrapped pair was recorded at wrap time, so the restore stays
    # complete.
    for parent, attr in _WRAPPED:
        current = getattr(parent, attr, None)
        if current is not None and current in _RECORDERS:
            setattr(parent, attr, current.__wrapped__)
    _WRAPPED.clear()
