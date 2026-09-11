"""Plugin extension mechanism — the wrap registration primitive + plugin loader.

This module is for **plugin authors and the framework**, not the agent SDK —
hence the `_` prefix; it does not appear in the `ava.help()` listing the agent
sees. The agent should not import it. Plugin authors reach the wrap primitive
through the curated `ava.extend` surface assembled in `ava/__init__.py`:

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

Plugin loading: every production load path shares the primitives here —
`load_plugin_module` (the by-path import contract: dotted name, synthetic
parent packages, `sys.modules` registration before execution, reload-in-place)
and `safe_load_plugin_module` (the fail-soft wrapper). `scan_and_load` is the
external-only loader `agent/_process_boot.py` calls at host boot;
`agent/graph/_build.py:_load_extensions` drives the same primitives, so a
plugin sees the same module name, `__package__`, and `sys.modules` identity
whichever production path imports it. Both wrap the import in
`with PluginContext(name):`, so a wrap registered at plugin import time is
attributed to its plugin without the author passing a name. They live here
because loading and wrapping are two halves of the one plugin-extension
surface.
"""

from __future__ import annotations

import functools
import importlib.util
import inspect
import os.path
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
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


# target -> the base callable captured before any plugin wrapped it. Restored by
# clear_wraps so a reload re-wraps from a pristine core (the job the old
# exclusivity assert did by refusing to run twice).
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

    plugin = _current_plugin_name()

    def chained(*args: Any, **kwargs: Any) -> Any:
        if plugin == UNATTRIBUTED:
            return wrapper(current, *args, **kwargs)
        # Activation telemetry (philosophy §6). A wrapper that calls `inner`
        # exactly once left control flow alone; a short-circuit (0 calls) or a
        # retry (>1) is the layer actually changing what happened, and that is
        # the fact issue #40 wants attributed. `counted` carries `current`'s
        # metadata so a wrapper that introspects `inner` (signature, attached
        # members) sees no difference — the same transparency contract
        # `agent/sdk_metering.py` keeps.
        # A one-element list rather than `nonlocal`: pyright cannot see the
        # nested increment and narrows a rebound local to the literal 0, which
        # makes the read below an "unnecessary comparison" error.
        calls = [0]

        @functools.wraps(current)
        def counted(*inner_args: Any, **inner_kwargs: Any) -> Any:
            calls[0] += 1
            return current(*inner_args, **inner_kwargs)

        try:
            return wrapper(counted, *args, **kwargs)
        finally:
            if calls[0] != 1:
                _record_activation(target, plugin, calls[0])

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


def _plugin_module_dotted(pkg: str, name: str) -> str:
    """The one spelling of a plugin entry module's dotted name — the identity
    both production loaders and `sys.modules` cleanup key on."""
    return f"{pkg}.{name}.plugin"


def register_plugin_parent_packages(pkg: str, name: str, plugin_dir: Path) -> None:
    """Make `pkg.<name>` importable as a namespace-package chain, so relative
    imports inside plugin.py resolve regardless of sys.path / cwd.

    External plugins load under the dotted prefix ``plugins.<name>``, and
    resolving that prefix through the normal import machinery requires
    ``$AVA_HOME`` to be on sys.path. It is not in the exec child (``python -I
    -m agent.exec_child`` boots with cwd=$AVA_HOME/source): ``import plugins``
    there resolves to the checkout's own legacy ``plugins/`` directory when one
    exists, or to nothing — either way ``from . import _sibling`` inside
    plugin.py raises ModuleNotFoundError and took ``import ava`` down with it
    (2026-08-28 ava_ledger incident). The framework owns the dotted prefix, so
    it registers the parent packages itself: ``pkg`` as a namespace package
    over the external plugins root and ``pkg.<name>`` over this plugin's
    directory. Existing sys.modules entries are left untouched — the
    ``pkg.<name>`` registration is what resolves relative imports, the
    top-level one covers a fresh ``import plugins.X`` elsewhere.
    """
    import types

    if sys.modules.get(pkg) is None:
        parent = types.ModuleType(pkg)
        parent.__path__ = [str(plugin_dir.parent)]  # pyright: ignore[reportAttributeAccessIssue]
        sys.modules[pkg] = parent
    child_name = f"{pkg}.{name}"
    if sys.modules.get(child_name) is None:
        child = types.ModuleType(child_name)
        child.__path__ = [str(plugin_dir)]  # pyright: ignore[reportAttributeAccessIssue]
        sys.modules[child_name] = child


def load_plugin_module(plugin_py: Path, *, name: str, pkg: str) -> ModuleType:
    """Import one plugin's ``plugin.py`` by path under its production dotted name.

    THE by-path loader both production load paths share (`scan_and_load` at
    host boot, `agent/graph/_build.py:_load_extensions` per graph build), so
    one plugin sees one module name, one ``__package__``, and one
    ``sys.modules`` identity whichever path imported it. The dotted name is
    ``plugins.<name>.plugin`` for an external plugin and
    ``ava_builtins.plugins.<name>.plugin`` for a built-in — importlib sets
    ``__package__`` from it, which is what makes ``from . import sibling``
    inside plugin.py resolve; the external ``plugins`` parent packages are
    registered here (see `register_plugin_parent_packages`).

    The module object is registered into ``sys.modules`` **before** execution —
    a Pydantic model defined inside a plugin triggers
    ``get_type_hints``-through-``sys.modules`` annotation resolution — and a
    repeat load of the same file re-executes the registered object
    (``importlib.reload`` semantics) instead of binding a second one, so module
    identity is stable for the life of the process (issue #147).

    Raises whatever the module raises: containment is
    `safe_load_plugin_module`'s job, not this function's.
    """
    spec = importlib.util.spec_from_file_location(_plugin_module_dotted(pkg, name), plugin_py)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"spec_from_file_location returned None for existing {plugin_py}")
    # Reload, not replace (issue #147). When this dotted name already names
    # *this* file, execute into the module object that is already registered
    # instead of binding a fresh one — replacing it forks the module identity:
    # whoever imported the plugin before the load keeps the old object, while
    # every later `sys.modules` lookup resolves the new one. Re-executing keeps
    # one `__dict__`, so both sides see the same globals. A *different* file
    # claiming the same dotted name (synthetic plugins under a tmp dir in
    # tests) is a different module and must not inherit the previous one's
    # globals — bind fresh.
    existing = sys.modules.get(spec.name)
    recorded = getattr(existing, "__file__", None)
    if (
        existing is not None
        and recorded is not None
        and os.path.realpath(recorded) == os.path.realpath(plugin_py)
    ):
        module = existing
    else:
        module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    if pkg == "plugins":
        # External plugins only: built-in plugins resolve through the real
        # `ava_builtins.plugins` package (source is on sys.path), and shadowing
        # it with a synthetic module would hide whatever its __init__.py defines
        # from later `importlib.import_module` callers.
        register_plugin_parent_packages(pkg, name, plugin_py.parent)
    spec.loader.exec_module(module)
    return module


def safe_load_plugin_module(plugin_py: Path, *, name: str, pkg: str) -> ModuleType | None:
    """`load_plugin_module` with the fail-soft contract: a plugin failure never
    escapes this boundary.

    On a load failure the half-executed module is dropped from ``sys.modules``
    (a later load retries from a clean slate), the failure is reported loudly
    (`shared.plugin_load_report` — a loguru ERROR plus one
    ``plugin_load_failed`` telemetry event), and ``None`` is returned so the
    caller skips this plugin and keeps going. ``KeyboardInterrupt`` /
    ``SystemExit`` still propagate: cancellation is not a plugin failure
    (2026-08-28 ava_ledger / 2026-09-10 agent-host incidents; user ruling
    2026-09-11).

    Returns:
        The executed module, or ``None`` when the plugin failed to load.
    """
    try:
        return load_plugin_module(plugin_py, name=name, pkg=pkg)
    except KeyboardInterrupt:
        sys.modules.pop(_plugin_module_dotted(pkg, name), None)
        raise
    except SystemExit:
        sys.modules.pop(_plugin_module_dotted(pkg, name), None)
        raise
    except BaseException as exc:
        sys.modules.pop(_plugin_module_dotted(pkg, name), None)
        from shared import plugin_load_report

        plugin_load_report.report_plugin_load_failure(name, exc)
        return None


def scan_and_load(
    plugin_dir: str | Path | None = None,
    *,
    enabled: set[str] | None = None,
) -> list[str]:
    """Scan every subdirectory under plugin_dir, import each one's plugin.py.

    Layout convention:

        $AVA_HOME/plugins/
        ├── audit/plugin.py
        ├── token_budget/plugin.py
        └── my_custom/plugin.py

    Each plugin.py runs `ava.extend.wrap` / other register calls at the top
    level — side-effect driven registration; importing here triggers it. Every
    import goes through `safe_load_plugin_module` inside
    `with PluginContext(name):`, the exact contract
    `agent/graph/_build.py:_load_extensions` uses for the same file — same
    dotted module name, same `sys.modules` identity, same fail-soft
    containment — so the two production load paths cannot disagree about what
    a plugin import does.

    Args:
        plugin_dir: scan root. `None` uses the loaded generation in wheel mode
            or the per-home installation root in source mode. String with `~`
            is auto-expanded to user home.
        enabled: explicit set of enabled names. Empty set = load none
            (distinct from `None`); `None` (default) loads all valid plugins
            under plugin_dir — for tests and explicit full loads. The
            production caller (`agent/_process_boot.py:load_process_extensions`)
            passes the per-machine enable set from `plugins_config.json`, so a
            disabled plugin is never imported by the host-boot path either.

    Returns the list of successfully loaded plugin names (sorted). No plugin_dir
    → empty list. A plugin that fails to load — relative-import error, syntax
    error, top-level exception, missing sibling — is skipped with a loud report
    and the remaining plugins still load (the fail-soft contract; 2026-08-28
    ava_ledger / 2026-09-10 agent-host incidents). Disabled names are never
    imported.
    """
    if plugin_dir is None:
        from shared.runtime_interpreter import external_plugin_read_root

        root = external_plugin_read_root()
    else:
        root = Path(plugin_dir).expanduser()
    if not root.exists():
        return []

    from shared.plugin_context import PluginContext

    loaded: list[str] = []
    for plugin_subdir in sorted(root.iterdir()):
        # Dot-prefixed dirs are atomic-install residue (.name.staging /
        # .name.backup-<pid>, 2026-08-28 ava_ledger defense line) — never a
        # real plugin, even when a hard kill left plugin.py inside.
        if plugin_subdir.name.startswith("."):
            continue
        if not plugin_subdir.is_dir():
            continue
        plugin_py = plugin_subdir / "plugin.py"
        if not plugin_py.exists():
            continue

        plugin_name = plugin_subdir.name
        if enabled is not None and plugin_name not in enabled:
            continue

        with PluginContext(plugin_name):
            module = safe_load_plugin_module(plugin_py, name=plugin_name, pkg="plugins")
        if module is None:
            continue
        loaded.append(plugin_name)

    return loaded
