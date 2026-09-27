"""The wrap registration primitive — `ava.extend.wrap` and its introspection.

Agent visibility is governed by the `__all_for_ava__` whitelist in
`ava/__init__.py`, not by a name's underscore prefix: this module carries a
public name — reached across the `ava` package boundary by the agent kernel
(`agent/state.py:clear_plugin_registrations`) — but stays out of the agent's
`ava.help()` view because it is absent from `__all_for_ava__`. Plugin authors
reach the wrap primitive through the curated `ava.extend` surface assembled in
`ava/__init__.py`:

    import ava

    def audit_writes(inner, path, content):
        log_audit("write", path, len(content))
        return inner(path, content)

    ava.extend.wrap("files.write", audit_writes)

`ava.extend.wrap(target, wrapper)` installs `wrapper` around the callable at the
dotted `ava` path `target` (`"files.read"`, `"shell.run"`, `"agents.spawn"`,
`"understand"`). The wrapper's first parameter receives the current callable
(`inner`) — the original, or the previous plugin's wrap when several layers
stack — and the wrapper decides whether, when, and how many times to call it.
`inner(*args, **kwargs) -> result`; the wrapper is Turing-complete Python and
can express before / after / replace / retry with no schema ceremony. That is
the point: a schema'd hook registry would cap extensions at anticipated shapes.

**Why this exists instead of bare `setattr`.** A plugin monkey-patching
`ava.files.read = my_read` leaves no record of who changed it, in what order, or
how to undo it — the old code compensated with a hand-rolled "I am the sole
wrapper" assert on every target. The registry here makes the wrap stack
enumerable (`ava.extend.stack(target)` / `ava.extend.wrappers()`), deterministic
(registration order = plugin load order, and plugins load in sorted name order),
and reversible (`clear_wraps` restores originals on reload), so the assert is
gone and layering is allowed.

**Lawfulness contract (enforced at review, not by types).** A wrapper is
Turing-complete, so nothing mechanically stops it from misbehaving; three rules
keep a stack composable and are checked in review:

1. Preserve the inner signature. Extend it by *adding* keyword arguments only
   (see `ava_fleet`'s `label`); never drop or reorder what callers pass to
   `inner`.
2. Never swallow an exception silently. Let it propagate, or re-raise with
   context — a wrapper that eats errors turns a fail-fast core into a guessing
   game.
3. Document any short-circuit (not calling `inner`) or multiple calls of
   `inner`. The next author reading the stack must be able to reason about
   control flow from the docstring.

Rule 3's cases are also the ones worth measuring, so a plugin layer that calls
`inner` anything other than exactly once emits one `plugin_activation` event
(`shared/plugin_activation.py`) — the runtime half of the attribution ledger,
and philosophy §6's obsolescence gauge for wrap-shaped shims. A layer that
passes straight through records nothing: it always runs once installed, so
counting it would measure the installation rather than the shim.

The plugin *loader* (by-path import, fail-soft containment, the external
`plugins/` directory scan) is a separate concern — framework API for the
agent kernel — and lives in `ava/sdk_surface/plugin_loader.py`.
"""

from __future__ import annotations

import functools
import inspect
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


class WrapError(Exception):
    """Root of `ava.extend.wrap` failures. Plugin authors catch this for a coarse net."""


class WrapTargetError(WrapError):
    """`target` is not a dotted path of plain identifiers under `ava`, or it does
    not resolve to a callable. Wrapping a missing / non-callable member is a
    plugin bug — fail fast at registration rather than at first agent call."""


@dataclass(frozen=True)
class WrapLayer:
    """One installed wrap layer, in registration order.

    - target: dotted `ava` path, e.g. `"files.read"`.
    - plugin: the plugin that registered it (`UNATTRIBUTED` when wrapped outside
      a `PluginContext`, e.g. a direct test call).
    - wrapper: the author's `wrapper(inner, *args, **kwargs)` function — the
      introspectable artifact (`wrapper.__module__` / source say who and what).
    - chained: the closure actually installed on the namespace at this layer.
    """

    target: str
    plugin: str
    wrapper: Callable[..., Any]
    chained: Callable[..., Any]


# target -> the base callable captured before any plugin wrapped it (below any
# SDK-metric recorder layers — see `_base_callable`). Restored by clear_wraps so
# a reload re-wraps from a pristine core (the job the old exclusivity assert did
# by refusing to run twice).
_ORIGINALS: dict[str, Callable[..., Any]] = {}

# target -> layers in registration (= plugin load) order, innermost first.
_LAYERS: dict[str, list[WrapLayer]] = {}


# `WrapLayer.plugin` for a wrap installed outside a plugin import (the framework
# itself, or a test calling `wrap` directly). Such a layer is real machinery and
# still shows up in `stack(target)`, but it is nobody's contribution, so it is
# neither in the attribution ledger nor in activation telemetry.
UNATTRIBUTED = "<unknown>"


def _current_plugin_name() -> str:
    """The plugin importing right now, or `UNATTRIBUTED`. Lazy import keeps this
    leaf module free of an `ava -> shared` load-order dependency."""
    try:
        from shared.plugin_context import current_plugin_name
    except ImportError:
        return UNATTRIBUTED
    return current_plugin_name() or UNATTRIBUTED


def _record_activation(target: str, plugin: str, inner_calls: int) -> None:
    """Report one non-transparent firing of this layer — see `chained`. Lazy
    import for the same reason `_current_plugin_name` is lazy; the emit path
    itself swallows its own failures."""
    try:
        from shared import plugin_activation
    except ImportError:
        return
    plugin_activation.record(plugin, "sdkWraps", target, detail=f"inner_calls={inner_calls}")


def _record_contribution(target: str, wrapper: Callable[..., Any]) -> None:
    """Mirror the layer into the plugin attribution ledger `ava plugins inspect`
    reads. Lazy import for the same reason `_current_plugin_name` is lazy; a
    no-op outside a plugin import (a test's direct wrap keeps showing up in
    `stack(target)`, which is the whole-machine view)."""
    try:
        from shared import plugin_contributions
    except ImportError:
        return
    plugin_contributions.record(
        "sdkWraps",
        target,
        detail=f"{getattr(wrapper, '__module__', '?')}.{getattr(wrapper, '__qualname__', wrapper)}",
    )


def _locate(target: str) -> tuple[Any, str]:
    """Resolve a dotted `ava` path to `(parent_object, attr_name)`.

    `"files.read"` -> `(ava.files, "read")`; `"understand"` -> `(ava, "understand")`.
    Raises WrapTargetError for a malformed path; lets a genuine AttributeError
    from a missing / AVA_SDK_DISABLE-disabled segment propagate (its message is
    already legible).
    """
    segments = target.split(".")
    if not target or not all(s.isidentifier() and not s.startswith("_") for s in segments):
        raise WrapTargetError(
            f"wrap target {target!r} must be a dotted path of identifiers under `ava` "
            f"(no leading underscore), e.g. 'files.read' or 'agents.spawn'."
        )
    obj: Any = sys.modules["ava"]
    for seg in segments[:-1]:
        obj = getattr(obj, seg)
    return obj, segments[-1]


def _install_metadata(chained: Callable, wrapper: Callable, current: Callable) -> None:
    """Make `chained` present as the function it replaces.

    - name / qualname / module come from `current`, so the SDK stub renders
      `def read(...)` and repr stays `ava.files`.
    - doc: the wrapper's own docstring becomes the new agent-facing contract
      when it wrote one (it is enhancing the surface, like the cwd-aware
      `files.read`); otherwise inherit `current`'s — a transparent wrap keeps the
      original contract.
    - signature: the wrapper's declared parameters minus the leading `inner`, so
      a wrapper that adds a keyword (fleet's `label`) advertises it and a
      transparent wrap keeps the original arity. `inspect.signature` honors
      `__signature__`, which is what `ava.help()` renders.
    - `__dict__`: carry function-attached members (e.g.
      `ava.understand.UnderstandError`) so the agent's documented attribute
      access survives the wrap. `setdefault` lets the wrapper keep its own.
    """
    chained.__name__ = getattr(current, "__name__", getattr(wrapper, "__name__", "wrapped"))
    chained.__qualname__ = getattr(current, "__qualname__", chained.__name__)
    chained.__module__ = getattr(current, "__module__", getattr(wrapper, "__module__", ""))
    chained.__doc__ = wrapper.__doc__ or getattr(current, "__doc__", None)
    for key, value in getattr(current, "__dict__", {}).items():
        chained.__dict__.setdefault(key, value)
    try:
        sig = inspect.signature(wrapper)
    except (ValueError, TypeError):
        return
    params = list(sig.parameters.values())
    if params:  # drop `inner`
        chained.__signature__ = sig.replace(parameters=params[1:])  # type: ignore[attr-defined]


def _base_callable(current: Callable[..., Any]) -> Callable[..., Any]:
    """`current` with any SDK-metric recorder layers stripped (task #3427).

    The metering recorder is installed at SDK import, *before* plugins load, so a
    real target usually carries it when a plugin wraps. The recorder is not a
    plugin layer: chaining over it would keep a stale recorder alive inside the
    wrap forever (so the metering `uninstall()` WeakSet never empties and its
    O(1) early-out dies) and would make `clear_wraps` restore a proxy where the
    registry promises the base callable. Recorders always carry `__wrapped__`
    (functools.wraps); the lazy import avoids the ava <-> submodule cycle."""
    from ava import sdk_metering

    while current in sdk_metering._RECORDERS:
        current = current.__wrapped__  # pyright: ignore[reportFunctionMemberAccess]
    return current


def wrap(target: str, wrapper: Callable[..., Any]) -> Callable[..., Any]:
    """Install `wrapper` around the `ava` callable at dotted `target`.

    `target` is a path under `ava` — `"files.read"`, `"shell.run"`,
    `"agents.spawn"`, `"understand"`. `wrapper(inner, *args, **kwargs)` is called
    in place of the target; `inner` is the current callable (the original, or the
    previous layer when plugins stack) and the wrapper decides whether / when /
    how often to call it. Layers compose in registration order — with plugins
    loaded in sorted name order, later plugins wrap outermost — and the order is
    inspectable via `stack` / `wrappers`.

    See this module's docstring for the three-rule lawfulness contract (preserve
    the inner signature, never swallow exceptions, document short-circuits /
    multi-calls). Returns `wrapper` so the caller keeps a reference.

    Raises:
        WrapTargetError: `target` is not a dotted identifier path under `ava` or
            does not resolve to a callable.
    """
    parent, attr = _locate(target)
    current = getattr(parent, attr)
    if not callable(current):
        raise WrapTargetError(
            f"ava.{target} is {type(current).__name__}, not callable — wrap targets are functions."
        )
    current = _base_callable(current)

    plugin = _current_plugin_name()

    @contextmanager
    def invocation() -> Generator[Callable[..., Any], None, None]:
        if plugin == UNATTRIBUTED:
            yield current
            return
        calls = [0]

        @functools.wraps(current)
        def counted(*inner_args: Any, **inner_kwargs: Any) -> Any:
            calls[0] += 1
            return current(*inner_args, **inner_kwargs)

        try:
            yield counted
        finally:
            if calls[0] != 1:
                _record_activation(target, plugin, calls[0])

    def chained(*args: Any, **kwargs: Any) -> Any:
        with invocation() as inner:
            return wrapper(inner, *args, **kwargs)

    if inspect.iscoroutinefunction(current) or inspect.iscoroutinefunction(wrapper):

        async def async_chained(*args: Any, **kwargs: Any) -> Any:
            with invocation() as inner:
                return await wrapper(inner, *args, **kwargs)

        chained = async_chained

    _install_metadata(chained, wrapper, current)
    setattr(parent, attr, chained)

    _ORIGINALS.setdefault(target, current)
    _LAYERS.setdefault(target, []).append(
        WrapLayer(target=target, plugin=plugin, wrapper=wrapper, chained=chained)
    )
    _record_contribution(target, wrapper)
    return wrapper


def stack(target: str) -> list[tuple[str, Callable[..., Any]]]:
    """The wrap layers on `target`, innermost first (= registration / load order).

    Each entry is `(plugin, wrapper)`. Empty list when nothing wrapped `target`.
    Answers "who changed `ava.<target>` on this machine" as one call.
    """
    return [(layer.plugin, layer.wrapper) for layer in _LAYERS.get(target, [])]


def wrappers() -> dict[str, list[tuple[str, Callable[..., Any]]]]:
    """Every wrapped target -> its `stack(target)`. The whole-machine wrap map,
    the runtime answer to "what did plugins inject" that plugin-injection docs
    are generated from instead of hand-maintained."""
    return {target: stack(target) for target in _LAYERS}


def clear_wraps() -> None:
    """Restore every wrapped target to its captured original and empty the
    registry. Called from `agent.state.clear_plugin_registrations` at the top of
    each `_load_extensions`, so a reload (test fixture / dev hot-reload) re-wraps
    from a pristine core instead of stacking onto the previous load's chain."""
    for target, original in _ORIGINALS.items():
        try:
            parent, attr = _locate(target)
        except AttributeError:
            # The wrap's parent namespace is already gone — a plugin namespace
            # cleared by clear_registered_namespaces before clear_wraps runs in
            # clear_plugin_registrations takes its wraps with it; nothing to restore.
            continue
        setattr(parent, attr, original)
    _ORIGINALS.clear()
    _LAYERS.clear()
