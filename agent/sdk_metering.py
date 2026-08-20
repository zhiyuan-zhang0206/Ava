"""Per-call SDK usage metering — the wrapping half of the SDK Usage instrumentation
(the runtime state + emit path live in ``shared/sdk_telemetry.py``, kept there so an
SDK function body in the ``ava`` layer can ``annotate()`` its own call).

Every public ``ava.*`` callable is wrapped, once, by a transparent recorder installed
at agent-graph build time (``agent/graph/_build.py:_load_extensions``, after plugins
load). On each top-level call the recorder (via ``run_metered``) writes one ``sdk_call``
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

Scope + top-level gating live in ``run_metered``: metering is armed only inside
``recording()`` (which the exec worker wraps around ``exec(compile(agent_code))``), so a
call counts only when it comes from agent-authored code — framework-internal ``ava.*``
calls (system-prompt rendering via ``ava.help``, hooks) never count. A per-call frame
stack records only the outermost call, so one agent statement that calls
``ava.self.compact()`` (which itself calls other ``ava.*`` helpers) counts once, not once
per internal fan-out. The recorder is installed *outermost* of any plugin
``ava.extend.wrap`` layer, so a plugin-wrapped member (e.g. fleet's ``agents.spawn`` +
``label``) still counts exactly once per agent call, whether the plugin short-circuits
or calls ``inner`` several times.

Coverage is the agent exec worker thread — the dominant SDK-call path. Static namespaces
are wrapped by the walk; ``ava.mcps``'s module helpers (``servers`` / ``description`` /
``help``) are wrapped via the dir() fallback, and its dynamic tools are metered at their
single call funnel (``_call_raw``, keyed by runtime server/tool). ``ava.skills`` is left
to its own ``skill_invoked`` tracking. A bare ``python x.py`` launched child has no
Postgres log sink, so its calls are silently un-metered — the side-channel swallow keeps
that harmless.
"""

from __future__ import annotations

import functools
import inspect
import weakref
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import Any

import ava
from shared.sdk_telemetry import run_metered

# Identity set of live recorder objects. install() skips a target only when the
# current top callable *is* one of these — robust to `ava.extend`'s wrap machinery,
# which copies a wrapped callable's __dict__ (so a plain attribute marker would leak
# onto a plugin wrapper built over a recorder and make install() wrongly skip it).
_RECORDERS: weakref.WeakSet[Callable[..., Any]] = weakref.WeakSet()


def _make_recorder(original: Callable[..., Any], fq: str) -> Callable[..., Any]:
    """Transparent proxy around ``original`` that meters the call as ``fq`` (the frame /
    active-gate / emit logic lives in ``shared.sdk_telemetry.run_metered``)."""

    @functools.wraps(original)
    def recorder(*args: Any, **kwargs: Any) -> Any:
        return run_metered(fq, original, args, kwargs)

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

    mcps_mod = getattr(ava, "mcps", None)
    if mcps_mod is not None:
        funnel = getattr(mcps_mod, _MCP_CALL_FUNNEL, None)
        if callable(funnel) and funnel not in _RECORDERS:
            setattr(mcps_mod, _MCP_CALL_FUNNEL, _make_mcp_recorder(funnel))


def uninstall() -> None:
    """Restore every metered target to the callable the recorder wraps — test
    teardown, so a test that installs the recorders does not leak them into the
    shared ``ava`` singleton the rest of the suite imports.

    The suite calls this after every test (autouse ``_restore_sdk_metering`` in
    ``tests/conftest.py``), because ``install()`` is a side effect of
    ``_load_extensions()`` and is reached lazily on any ``ava.*`` miss — so merely
    touching the namespace metered it for every later test in the worker (issue #83).
    """
    # O(1) early-out: _RECORDERS is a WeakSet, and an installed recorder is held
    # strongly by the namespace it sits on, so an empty set proves nothing is
    # installed. Without it every per-test teardown would pay the full namespace
    # walk below (~7 ms) to restore nothing.
    if not _RECORDERS:
        return

    for parent, attr, _fq in _instrument_targets():
        current = getattr(parent, attr, None)
        if current is not None and current in _RECORDERS:
            setattr(parent, attr, current.__wrapped__)

    mcps_mod = getattr(ava, "mcps", None)
    if mcps_mod is not None:
        funnel = getattr(mcps_mod, _MCP_CALL_FUNNEL, None)
        if funnel is not None and funnel in _RECORDERS:
            setattr(mcps_mod, _MCP_CALL_FUNNEL, funnel.__wrapped__)
