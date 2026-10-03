"""The plugin loader — by-path import and fail-soft containment.

This is framework API for the agent kernel, not the agent SDK: it carries a
public name — reached across the `ava` package boundary by `agent/extensions/__init__.py` —
but stays out of the agent's `ava.help()` view
because it is absent from `ava.__all_for_ava__`.

The one production load path (`agent/extensions/__init__.py:load_extensions`, which the
agent host, a child and the plugin catalog all run) uses the primitives here — `load_plugin_module`
(the by-path import contract: dotted name, synthetic parent packages,
`sys.modules` registration before execution, reload-in-place) and
`safe_load_plugin_module` (the fail-soft wrapper), so a plugin sees one module name,
`__package__`, and `sys.modules` identity however many times it loads. Importing registers nothing: the SDK
surface a plugin declares is installed by `ava/sdk_surface/install.py`.
"""

from __future__ import annotations

import importlib.util
import os.path
import sys
from pathlib import Path
from types import ModuleType


def _plugin_module_dotted(pkg: str, name: str, module: str = "plugin") -> str:
    """The one spelling of a plugin module's dotted name — the identity both
    production loaders and `sys.modules` cleanup key on. `module` is the entry
    `plugin`, or a sibling like the `agent_runtime` face (task #3633)."""
    return f"{pkg}.{name}.{module}"


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


def load_plugin_module(
    plugin_py: Path, *, name: str, pkg: str, module: str = "plugin"
) -> ModuleType:
    """Import one plugin's ``plugin.py`` by path under its production dotted name.

    THE by-path loader (`agent.extensions.load_extensions` drives it), so
    one plugin sees one module name, one ``__package__``, and one
    ``sys.modules`` identity however many times it loads. The dotted name is
    ``plugins.<name>.<module>`` for an external plugin and
    ``ava_builtins.plugins.<name>.<module>`` for a built-in (`module` defaults
    to the entry ``plugin``; `agent.extensions` passes ``agent_runtime`` for
    the runtime face) — importlib sets
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
    spec = importlib.util.spec_from_file_location(
        _plugin_module_dotted(pkg, name, module), plugin_py
    )
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
        loaded = existing
    else:
        loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    if pkg == "plugins":
        # External plugins only: built-in plugins resolve through the real
        # `ava_builtins.plugins` package (source is on sys.path), and shadowing
        # it with a synthetic module would hide whatever its __init__.py defines
        # from later `importlib.import_module` callers.
        register_plugin_parent_packages(pkg, name, plugin_py.parent)
    spec.loader.exec_module(loaded)
    # What the import system does after loading a submodule: bind it as an attribute of its parent
    # package, so `pkg.module` resolves by attribute (a dotted `monkeypatch` / `mock.patch` target
    # walks attributes, and a by-path load left the parent without one).
    parent_name, _, child_attr = spec.name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None:
        setattr(parent, child_attr, loaded)
    return loaded


def safe_load_plugin_module(
    plugin_py: Path, *, name: str, pkg: str, module: str = "plugin"
) -> ModuleType | None:
    """`load_plugin_module` with the fail-soft contract: a plugin failure never
    escapes this boundary.

    On a load failure the half-executed module is dropped from ``sys.modules``
    (a later load retries from a clean slate), the failure is reported loudly
    (`base.packages.plugins.load_report` — a loguru ERROR plus one
    ``plugin_load_failed`` telemetry event), and ``None`` is returned so the
    caller skips this plugin and keeps going. ``KeyboardInterrupt`` /
    ``SystemExit`` still propagate: cancellation is not a plugin failure
    (2026-08-28 ava_ledger / 2026-09-10 agent-host incidents; user ruling
    2026-09-11).

    Returns:
        The executed module, or ``None`` when the plugin failed to load.
    """
    try:
        return load_plugin_module(plugin_py, name=name, pkg=pkg, module=module)
    except KeyboardInterrupt:
        sys.modules.pop(_plugin_module_dotted(pkg, name, module), None)
        raise
    except SystemExit:
        sys.modules.pop(_plugin_module_dotted(pkg, name, module), None)
        raise
    except BaseException as exc:
        sys.modules.pop(_plugin_module_dotted(pkg, name, module), None)
        from base.packages.plugins import load_report

        load_report.report_plugin_load_failure(name, exc)
        return None
