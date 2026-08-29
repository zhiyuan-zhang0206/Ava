"""Plugin SDK extension point — namespace / member / SDK-expand registration.

The `ava.register_namespace` family, its exception hierarchy, and the
registration registries, split out of `ava/__init__.py`. The package entry
re-exports every public name; the registries and `__all_for_ava__` mutations
are applied to the `ava` package module itself (via `ava_module()`), so plugin
registrations land exactly where they did before the split.
"""

import sys as _sys
from types import ModuleType, SimpleNamespace
from typing import Any, Literal

from . import ava_module
from .sdk_disable import _DisabledSDKModule

# ── plugin SDK extension point ────────────────────────────────────────────
#
# Plugin author API (not in __all_for_ava__ — this is for plugin authors,
# should not appear in the help(ava) view the agent sees). After `import ava` in
# a plugin's plugin.py, calling `ava.register_namespace("foo", module)`
# adds an importable submodule at the ava top level (registered in
# sys.modules as `ava.<name>`, so `import ava.<name>` works); the agent hits the plugin
# implementation via `ava.foo.bar()`; help(ava) also includes the `foo/`
# submodule line. `_REGISTERED_NAMESPACES` tracks plugin-owned names;
# `agent.state.clear_plugin_registrations()` calls
# `_clear_registered_namespaces()` on graph rebuild / test reload to tear
# them down, avoiding reload-induced NamespaceConflictError.


# ── Exception hierarchy (CLAUDE.md SDK docstring rule: parent + subclass) ─
# Plugin authors use RegisterNamespaceError for coarse catch, specific
# subclass for fine catch.
# `__all_for_ava__` and `_REGISTERED_NAMESPACES` are two-faced representations
# of the same invariant — only modifiable via register_namespace /
# clear_registered_namespaces; external mutation of `ava.__all_for_ava__` would
# diverge the two; convention: do not directly modify `ava.__all_for_ava__`.


class RegisterNamespaceError(Exception):
    """Root of `ava.register_namespace` failures. Plugin authors use this for coarse catch."""


class InvalidNamespaceNameError(RegisterNamespaceError):
    """name is not a valid identifier or starts with underscore (that's the framework / private namespace convention)."""


class InvalidNamespaceModuleError(RegisterNamespaceError):
    """module argument is not ModuleType or SimpleNamespace.

    Plugins mistakenly passing int / None / str / dict etc. cannot be
    correctly expanded by `help(ava)` and don't support `ava.{name}.foo`
    attribute access — raise immediately so the plugin author fixes it,
    rather than getting AttributeError six months later when the agent
    runs and losing the root cause.
    """


class NamespaceConflictError(RegisterNamespaceError):
    """name is already taken. Two subclasses distinguish framework-builtin vs another plugin."""


class FrameworkNamespaceConflictError(NamespaceConflictError):
    """name collides with a framework-builtin submodule (ava.files / ava.shell) or top-level attr —
    plugin must rename (framework-owned namespaces are not overridable)."""


class PluginNamespaceConflictError(NamespaceConflictError):
    """name already registered by another plugin — last-write-wins would
    cause plugins to silently trample on each other; fail-fast so plugin
    authors negotiate to rename. Message contains the name-holding plugin
    (if `agent.state.PluginContext` is active during plugin.py import)."""


class UnknownNamespaceError(RegisterNamespaceError):
    """register_namespace_member target namespace does not exist — the parent
    (e.g. ava.self) must be an existing namespace before a plugin can hang a
    member on it. Typo in the namespace name, or a namespace disabled by
    AVA_SDK_DISABLE."""


class InvalidNamespaceMemberError(RegisterNamespaceError):
    """register_namespace_member was passed a non-callable member. Members are
    functions the agent invokes (ava.<namespace>.<name>(...)); a non-callable
    has no signature/docstring to render and cannot be called — raise now."""


class MemberConflictError(NamespaceConflictError):
    """register_namespace_member name already exists on the target namespace
    (any existing attribute — a framework member/constant or another plugin's) —
    last-write-wins would silently trample; fail-fast so the plugin author renames."""


# name → name-holding plugin (read from
# `agent.state.PluginContext.current_plugin_name()` — `_load_extensions`
# calls plugin.py's `import` triggering register_namespace inside
# `with PluginContext(name):`, where ContextVar is alive). Calls outside
# a plugin (tests, dev) get `<unknown>`.
_REGISTERED_NAMESPACES: dict[str, str] = {}

# (namespace, member name) pairs added via register_namespace_member — torn
# down alongside _REGISTERED_NAMESPACES on reload. Tracked separately because
# a member lives on an existing namespace module (e.g. ava.self), not at the
# ava top level.
_REGISTERED_MEMBERS: list[tuple[str, str]] = []

# Dotted paths plugins promote into the system prompt's expanded SDK
# reference via register_sdk_expand — rendered BEFORE the configured
# framework list (a plugin promotes its own highest-frequency surface, e.g.
# ava_code's cwd leads the coding namespaces). Torn down on reload alongside
# the other plugin registrations.
_REGISTERED_SDK_EXPANSIONS: list[str] = []


def _current_plugin_name() -> str:
    """Lazy fetch of the currently active plugin name — avoids the
    ava → agent.state top-level import reverse dependency. This function
    is only called at register_namespace runtime, when the agent module
    has finished importing. Returns `<unknown>` when not called inside a
    PluginContext (e.g. test directly calling)."""
    try:
        from shared.plugin_context import current_plugin_name as _cp
    except ImportError:
        return "<unknown>"
    return _cp() or "<unknown>"


def _record(
    surface: Literal["sdkNamespaces", "sdkMembers", "sdkExpansions"],
    identifier: str,
    *,
    detail: str = "",
) -> None:
    """Attribute this registration to the importing plugin, for
    `ava plugins inspect`. Lazily imported for the same reason
    `_current_plugin_name` is, and a no-op outside a plugin import."""
    try:
        from shared import plugin_contributions
    except ImportError:
        return
    plugin_contributions.record(surface, identifier, detail=detail)


def _materialize_namespace(name: str, ns: SimpleNamespace) -> ModuleType:
    """Turn a SimpleNamespace plugin namespace into a real module.

    `import ava.<name>` resolves through `sys.modules`, whose entries are
    conventionally real modules: a bare SimpleNamespace there is missed by
    the module predicates (`ismodule()`) behind discovery and help rendering,
    so `dir()` / `getattr_static` probes would see an object that does not
    look like a module. Materializing it as a real module named `ava.<name>`
    keeps every discovery path (`agent_visible_names`, help rendering)
    consistent — the members are copied into the module dict, and the
    `__all_for_ava__` surface is synthesized from the public members when the
    namespace did not declare one.
    """
    module = ModuleType(f"ava.{name}")
    module.__dict__.update(vars(ns))
    surface = getattr(ns, "__all_for_ava__", None)
    if not isinstance(surface, list):
        surface = [member for member in vars(ns) if not member.startswith("_")]
    module.__all_for_ava__ = surface  # type: ignore[attr-defined]
    return module


def register_namespace(name: str, module: Any) -> None:
    """Plugin actively adds a submodule to the ava top level; agent accesses via `ava.{name}.foo()`.

    Registration makes the namespace importable (`import ava.<name>`) for the rest of
    the process. Boundary: in a cold process where the registering plugin has not
    loaded yet (bare `import ava`, no agent bootstrap), `import ava.<name>` still
    raises ModuleNotFoundError — import resolution goes through sys.modules, not the
    package's dynamic attribute surface; only attribute access (`ava.<name>.x`)
    works then. That is expected, not a regression.

    Args:
        name: submodule name — valid Python identifier, no underscore
            prefix, cannot collide with ava builtin modules / top-level
            attrs (case-sensitive check).
        module: ModuleType or SimpleNamespace — other types
            (int/None/str/dict) cannot be correctly expanded by
            `help(ava)` and attribute access is inconsistent; raise immediately.

    Raises:
        InvalidNamespaceNameError: `name` is not a valid identifier or starts with underscore.
        InvalidNamespaceModuleError: `module` is not ModuleType / SimpleNamespace.
        FrameworkNamespaceConflictError: `name` already taken by a
            framework-builtin submodule or top-level attr, or disabled by
            AVA_SDK_DISABLE.
        PluginNamespaceConflictError: `name` already registered by another plugin.
    """
    if not name.isidentifier():
        raise InvalidNamespaceNameError(
            f"namespace name {name!r} is invalid — must be a valid Python identifier."
        )
    if name.startswith("_"):
        raise InvalidNamespaceNameError(
            f"namespace name {name!r} cannot start with underscore — that's the framework / private namespace convention."
        )
    if not isinstance(module, (ModuleType, SimpleNamespace)):
        raise InvalidNamespaceModuleError(
            f"register_namespace({name!r}, ...) module must be ModuleType or "
            f"SimpleNamespace, got {type(module).__name__} — agent uses "
            f"ava.{name}.X requiring attr access, and help(ava) must be able to introspect."
        )

    pkg = ava_module()
    if name in _REGISTERED_NAMESPACES:
        prev = _REGISTERED_NAMESPACES[name]
        raise PluginNamespaceConflictError(
            f"ava.{name} already registered by plugin {prev!r} — same-name override not allowed; plugin authors negotiate to rename."
        )
    if hasattr(pkg, name):
        raise FrameworkNamespaceConflictError(
            f"ava.{name} already exists (framework-builtin submodule / top-level attribute); plugin cannot override."
        )
    # AVA_SDK_DISABLE may have replaced the sys.modules entry with a
    # disabled-module sentinel before plugin load (the env is applied at
    # `import ava`; plugins register later). Registration must NOT clobber
    # the sentinel — the sentinel is the legible "disabled" error surface
    # the disable machinery put there, and a disabled name is framework-owned
    # (not overridable), so fail loud with the same conflict class.
    if isinstance(_sys.modules.get(f"ava.{name}"), _DisabledSDKModule):
        raise FrameworkNamespaceConflictError(
            f"ava.{name} is disabled by AVA_SDK_DISABLE — a disabled namespace is framework-owned and not overridable; plugin must rename."
        )

    if isinstance(module, SimpleNamespace):
        module = _materialize_namespace(name, module)
    # Register under `ava.<name>` in sys.modules so the namespace is a real
    # importable submodule: `import ava.<name>` / `from ava import <name>`
    # resolve through sys.modules (LLM agents write `import ava.memory` out
    # of Python habit; without the entry the import machinery raises
    # ModuleNotFoundError even though attribute access works). The same
    # object is set on the package, so `ava.<name>` and `import ava.<name>`
    # always agree. Kept in lockstep with `clear_registered_namespaces`,
    # which pops the entry on teardown.
    #
    # Boundary (documented, not a regression): the entry is written by THIS
    # registration, so `import ava.<name>` only succeeds once the registering
    # plugin has loaded. In a cold process that never loads the plugin — a
    # bare `import ava` without agent bootstrap — `import ava.<name>` still
    # raises ModuleNotFoundError: the import machinery resolves submodules
    # through sys.modules, not through the package's dynamic attribute
    # surface, so plugin namespaces are importable exactly when the plugin
    # registered them. Attribute access (`ava.<name>.x`) works either way.
    # AVA_SDK_DISABLE's disabled-module sentinel is never clobbered.
    _sys.modules[f"ava.{name}"] = module

    setattr(pkg, name, module)
    # For `help()` rendering — module's real `__name__` is the plugin's
    # internal path (`plugins.X.Y`); `_qualname` is the agent-facing real
    # name (`ava.<name>`), which also lets `_target_depth` compute heading
    # depth correctly. Underscore prefix avoids being treated as exposable
    # attr by child discovery.
    module._qualname = f"ava.{name}"  # type: ignore[attr-defined]
    _REGISTERED_NAMESPACES[name] = _current_plugin_name()
    _record("sdkNamespaces", name, detail=getattr(module, "__name__", type(module).__name__))
    if name not in pkg.__all_for_ava__:
        pkg.__all_for_ava__.append(name)


def register_sdk_expand(*paths: str) -> None:
    """Plugin promotes dotted SDK paths into the system prompt's expanded
    SDK reference, rendered before the configured framework list
    (settings.agent.sdk_expand_in_system_prompt). Use for a plugin namespace whose
    full contract belongs with the always-expanded high-frequency surface —
    a framework default must not name plugin namespaces, so this hook is the
    only way a plugin path gets there.

    Registration is idempotent (re-registering an already-registered path is
    a no-op); AVA_SDK_DISABLE still wins at render time. Torn down by
    `clear_registered_namespaces` on plugin reload.

    Args:
        paths: dotted paths under `ava` (e.g. "cwd", "code.review") — each
            segment a valid identifier, no underscore prefix.

    Raises:
        ValueError: a path is empty or has an invalid / underscore segment.
    """
    for path in paths:
        segments = path.split(".")
        if not path or not all(s.isidentifier() and not s.startswith("_") for s in segments):
            raise ValueError(
                f"register_sdk_expand path {path!r} is invalid — dotted identifiers "
                "without underscore prefixes (e.g. 'cwd', 'shell.sessions')."
            )
        if path not in _REGISTERED_SDK_EXPANSIONS:
            _REGISTERED_SDK_EXPANSIONS.append(path)
            _record("sdkExpansions", path, detail="expanded ahead of the framework list")


def register_namespace_member(namespace: str, name: str, fn: Any) -> None:
    """Plugin attaches a callable as a member of an existing ava namespace; the
    agent invokes it via `ava.{namespace}.{name}(...)`.

    Prefer this over `register_namespace` when a capability belongs under an
    existing group rather than warranting a brand-new top-level name — the ava
    top level is deliberately small, so a new self-action goes on `ava.self`
    next to terminate / restart / compact, not as `ava.<thing>`.

    Args:
        namespace: an existing ava namespace to extend (e.g. "self").
        name: member name — valid identifier, no underscore prefix, must not
            collide with an existing member of that namespace.
        fn: the callable the agent calls. Its signature + docstring are what
            the agent reads under `help(ava.{namespace})`, so the docstring
            obeys the same agent-facing rules as any SDK function.

    Raises:
        InvalidNamespaceNameError: name is not a valid identifier or starts with underscore.
        InvalidNamespaceMemberError: fn is not callable.
        UnknownNamespaceError: namespace is not an existing ava namespace module.
        MemberConflictError: name already exists on that namespace.
    """
    if not name.isidentifier():
        raise InvalidNamespaceNameError(
            f"member name {name!r} is invalid — must be a valid Python identifier."
        )
    if name.startswith("_"):
        raise InvalidNamespaceNameError(
            f"member name {name!r} cannot start with underscore — that's the framework / private namespace convention."
        )
    if not callable(fn):
        raise InvalidNamespaceMemberError(
            f"register_namespace_member({namespace!r}, {name!r}, ...) member must be callable, "
            f"got {type(fn).__name__} — the agent calls ava.{namespace}.{name}(...)."
        )

    pkg = ava_module()
    parent = getattr(pkg, namespace, None)
    # Member discovery (help) + this attach both rely on the parent being a
    # real module with an `__all_for_ava__` whitelist; SimpleNamespace plugin
    # namespaces and disabled-module sentinels are not valid member hosts.
    if not isinstance(parent, ModuleType) or not isinstance(
        getattr(parent, "__all_for_ava__", None), list
    ):
        raise UnknownNamespaceError(
            f"ava.{namespace} is not an existing namespace that can host members — "
            f"check the name (or it may be disabled by AVA_SDK_DISABLE)."
        )
    if hasattr(parent, name):
        raise MemberConflictError(
            f"ava.{namespace}.{name} already exists (a framework attribute or another plugin's member) — "
            f"same-name override not allowed; plugin authors rename."
        )

    setattr(parent, name, fn)
    parent.__all_for_ava__.append(name)
    _REGISTERED_MEMBERS.append((namespace, name))
    _record(
        "sdkMembers",
        f"{namespace}.{name}",
        detail=f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', fn)}",
    )


def clear_registered_namespaces() -> None:
    """Called by `agent.state.clear_plugin_registrations` — tears down all
    SDK namespaces added by plugins on reload, so the next
    `_load_extensions()` re-registers from empty state, avoiding
    reload-induced ghost namespace collisions.

    Cross-module public (same semantics as `agent.state.PluginContext`:
    used internally by framework but plugin authors can also call manually
    in dev REPL) — so no underscore prefix.
    """
    pkg = ava_module()
    # Iterate over a copy of keys — `_REGISTERED_NAMESPACES.clear()` below
    # runs *outside* the for loop, strictly speaking it isn't modified
    # during iteration; but copying the key set is safer when
    # `delattr` + `__all_for_ava__.remove` fail paths do partial cleanup.
    for name in list(_REGISTERED_NAMESPACES):
        _remove_attr(pkg, name)
        _remove_from_surface(pkg.__all_for_ava__, name)
        # Drop the importable alias — otherwise `import ava.<name>` keeps
        # returning the stale module after the namespace is torn down, and a
        # later re-registration would swap the package attribute to a fresh
        # object while sys.modules still serves the old one.
        _sys.modules.pop(f"ava.{name}", None)
    _REGISTERED_NAMESPACES.clear()

    # Members live on an existing namespace module (e.g. ava.self), so undo
    # them on that parent rather than on the ava top level.
    for namespace, name in _REGISTERED_MEMBERS:
        parent = getattr(pkg, namespace, None)
        # parent gone is reachable, not a bug: a member can sit on a *plugin*
        # namespace, and the loop above already deleted plugin top-level names.
        # The whole module is being discarded, so the member goes with it —
        # nothing to undo here.
        if parent is not None:
            _remove_attr(parent, name)
            _remove_from_surface(getattr(parent, "__all_for_ava__", None), name)
    _REGISTERED_MEMBERS.clear()

    _REGISTERED_SDK_EXPANSIONS.clear()


def _remove_attr(obj: Any, name: str) -> None:
    """Delete `name` from `obj` when present."""
    if hasattr(obj, name):
        delattr(obj, name)


def _remove_from_surface(surface: list[str] | None, name: str) -> None:
    """Remove `name` from an `__all_for_ava__`-style surface list when it is
    one (the member host may be gone, in which case there is nothing to do)."""
    if isinstance(surface, list) and name in surface:
        surface.remove(name)
