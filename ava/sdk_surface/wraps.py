"""The wrap registration primitive — `ava.extend.wrap` and its introspection.

Agent visibility is governed by the `__all_for_ava__` whitelist in
`ava/__init__.py`, not by a name's underscore prefix: this module stays out of
the agent's `ava.help()` view because it is absent from `__all_for_ava__`. A
plugin declares a wrap in its `contribute()`:

    def audit_writes(inner, path, content):
        log_audit("write", path, len(content))
        return inner(path, content)

    def contribute() -> PluginContributions:
        return PluginContributions(sdk_wraps=(SdkWrap("files.write", audit_writes),))

`ava.sdk_surface.install` applies it through `apply_wrap(target, wrapper, plugin, layers)`, which
installs `wrapper` around the callable at the dotted `ava` path `target` (`"files.read"`,
`"shell.run"`, `"agents.spawn"`, `"understand"`) and returns the undo. `layers` is the
install's build ledger (`{target: [WrapLayer, ...]}`, registration order); it is frozen
into the `Installation` at the end of the build, and `stack` / `wrappers` read it back. The curated
`ava.extend` surface keeps the introspection (`stack`, `wrappers`). The wrapper's first parameter receives the current callable
(`inner`) — the original, or the previous plugin's wrap when several layers
stack — and the wrapper decides whether, when, and how many times to call it.
`inner(*args, **kwargs) -> result`; the wrapper is Turing-complete Python and
can express before / after / replace / retry with no schema ceremony. That is
the point: a schema'd hook registry would cap extensions at anticipated shapes.

**Why this exists instead of bare `setattr`.** A plugin monkey-patching
`ava.files.read = my_read` leaves no record of who changed it, in what order, or
how to undo it — the old code compensated with a hand-rolled "I am the sole
wrapper" assert on every target. The layer table here makes the wrap stack
enumerable (`ava.extend.stack(target)` / `ava.extend.wrappers()`), deterministic
(declaration order = plugin load order, and plugins load in sorted name order),
and reversible (each `apply_wrap` returns its undo, run in reverse by
`ava.sdk_surface.install.uninstall`), so the assert is gone and layering is allowed.

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
(`base/packages/plugins/activation.py`) — the runtime half of the plugin attribution,
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
from collections.abc import Callable, Generator, Mapping
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
    - plugin: the plugin that declared it.
    - wrapper: the author's `wrapper(inner, *args, **kwargs)` function — the
      introspectable artifact (`wrapper.__module__` / source say who and what).
    - chained: the closure actually installed on the namespace at this layer.
    """

    target: str
    plugin: str
    wrapper: Callable[..., Any]
    chained: Callable[..., Any]


def _record_activation(target: str, plugin: str, inner_calls: int) -> None:
    """Report one non-transparent firing of this layer — see `chained`. Lazy
    import keeps this leaf free of an `ava -> shared` load-order dependency; the emit path
    itself swallows its own failures."""
    try:
        from ava.sdk_surface import settings as _settings
        from base.packages.plugins import activation

        model = _settings.agent_setting("llm_model")
    except Exception:
        from base.log import logger

        logger.opt(exception=True).warning(
            "wrap activation for {} by plugin {} not recorded: reading the model failed",
            target,
            plugin,
        )
        return
    activation.record(plugin, "sdkWraps", target, detail=f"inner_calls={inner_calls}", model=model)


def _locate(target: str) -> tuple[Any, str]:
    """Resolve a dotted `ava` path to `(parent_object, attr_name)`.

    `"files.read"` -> `(ava.files, "read")`; `"understand"` -> `(ava, "understand")`.
    Missing declared attributes are WrapTargetError. Errors raised by a real
    attribute descriptor propagate unchanged.
    """
    segments = target.split(".")
    if not target or not all(s.isidentifier() and not s.startswith("_") for s in segments):
        raise WrapTargetError(
            f"wrap target {target!r} must be a dotted path of identifiers under `ava` "
            f"(no leading underscore), e.g. 'files.read' or 'agents.spawn'."
        )
    obj: Any = sys.modules["ava"]
    for seg in segments[:-1]:
        obj = _target_member(obj, seg, target)
    return obj, segments[-1]


def _target_member(parent: Any, attr: str, target: str) -> Any:
    """Read a declared target without mistaking a descriptor's error for absence."""
    missing = object()
    if inspect.getattr_static(parent, attr, missing) is missing:
        raise WrapTargetError(f"ava.{target} does not resolve: {attr!r} is not declared.")
    return getattr(parent, attr)


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
    from ava.sdk_surface import metering

    while metering.is_recorder(current):
        current = current.__wrapped__  # pyright: ignore[reportFunctionMemberAccess]
    return current


def apply_wrap(
    target: str,
    wrapper: Callable[..., Any],
    plugin: str,
    layers: dict[str, list[WrapLayer]],
) -> Callable[[], None]:
    """Install `wrapper` around the `ava` callable at dotted `target`; returns the undo.

    `layers` is the caller's ledger (`{target: [WrapLayer, ...]}`, registration order):
    `ava.sdk_surface.install` passes its build ledger, which the installation then
    carries; the undo removes the layer from the same ledger and restores the
    callable. `target` is a path under `ava` — `"files.read"`, `"shell.run"`,
    `"agents.spawn"`, `"understand"`. `wrapper(inner, *args, **kwargs)` is called
    in place of the target; `inner` is the current callable (the original, or the
    previous layer when plugins stack) and the wrapper decides whether / when /
    how often to call it. Layers compose in declaration order — with plugins
    installed in sorted name order, later plugins wrap outermost — and the order is
    inspectable via `stack` / `wrappers`.

    See this module's docstring for the three-rule lawfulness contract (preserve
    the inner signature, never swallow exceptions, document short-circuits /
    multi-calls).

    Raises:
        WrapTargetError: `target` is not a dotted identifier path under `ava` or
            does not resolve to a callable.
    """
    parent, attr = _locate(target)
    current = _target_member(parent, attr, target)
    if not callable(current):
        raise WrapTargetError(
            f"ava.{target} is {type(current).__name__}, not callable — wrap targets are functions."
        )
    current = _base_callable(current)

    @contextmanager
    def invocation() -> Generator[Callable[..., Any], None, None]:
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

    layer = WrapLayer(target=target, plugin=plugin, wrapper=wrapper, chained=chained)
    target_layers = layers.setdefault(target, [])
    target_layers.append(layer)

    def undo() -> None:
        target_layers.remove(layer)
        if not target_layers:
            del layers[target]
        try:
            parent_now, attr_now = _locate(target)
        except AttributeError:
            # The wrap's parent namespace is already gone — a plugin namespace undone earlier takes
            # its wraps with it; nothing to restore.
            return
        if getattr(parent_now, attr_now, None) is chained:
            setattr(parent_now, attr_now, current)

    return undo


def _installed_layers() -> Mapping[str, tuple[WrapLayer, ...]]:
    """The current installation's wrap layers (empty when nothing is installed)."""
    from . import install as _install

    current = _install.installed()
    return {} if current is None else current.wrap_layers


def stack(target: str) -> list[tuple[str, Callable[..., Any]]]:
    """The wrap layers on `target`, innermost first (= registration / load order).

    Each entry is `(plugin, wrapper)`. Empty list when nothing wrapped `target` (or
    nothing is installed). Answers "who changed `ava.<target>` on this machine" as one call.
    """
    return [(layer.plugin, layer.wrapper) for layer in _installed_layers().get(target, ())]


def wrappers() -> dict[str, list[tuple[str, Callable[..., Any]]]]:
    """Every wrapped target -> its `stack(target)`. The whole-machine wrap map,
    the runtime answer to "what did plugins inject" that plugin-injection docs
    are generated from instead of hand-maintained."""
    return {
        target: [(layer.plugin, layer.wrapper) for layer in layers]
        for target, layers in _installed_layers().items()
    }
