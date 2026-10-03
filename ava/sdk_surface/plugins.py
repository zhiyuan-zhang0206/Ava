"""Plugin SDK extension primitives — namespace / member installation and its exception hierarchy.

A plugin declares `sdk_namespaces` / `sdk_members` (`base.packages.plugins.extensions`); nothing here is a
plugin-facing entry point. `ava.sdk_surface.install` is the one writer of the `ava` module and applies each
declaration through `install_namespace` / `install_member`, which return the undo that reverses it. The
registries that used to live here (`_REGISTERED_NAMESPACES`, `_REGISTERED_MEMBERS`,
`REGISTERED_SDK_EXPANSIONS`) are gone: the installation records what it applied, and the prompt's expanded
SDK reference reads `ava.sdk_surface.install.expansions()`.
"""

import sys as _sys
from collections.abc import Callable, Mapping
from types import ModuleType, SimpleNamespace
from typing import Any

from . import ava_module
from .sdk_disable import _DisabledSDKModule

# ── Exception hierarchy (AGENTS.md SDK docstring rule: parent + subclass) ─
# Plugin authors use RegisterNamespaceError for coarse catch, specific
# subclass for fine catch.
# `ava.__all_for_ava__` is only modified by the install primitives here and their
# undo; external mutation would diverge it from what the install recorded.
# Convention: do not directly modify `ava.__all_for_ava__`.


class RegisterNamespaceError(Exception):
    """Root of namespace / member install failures. Plugin authors use this for coarse catch."""


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
    """name already installed by another plugin — last-write-wins would
    cause plugins to silently trample on each other; fail-fast so plugin
    authors negotiate to rename. Message contains the name-holding plugin."""


class UnknownNamespaceError(RegisterNamespaceError):
    """member install target namespace does not exist — the parent
    (e.g. ava.self) must be an existing namespace before a plugin can hang a
    member on it. Typo in the namespace name, or a namespace disabled by
    AVA_SDK_DISABLE."""


class InvalidNamespaceMemberError(RegisterNamespaceError):
    """a declared member is not callable. Members are
    functions the agent invokes (ava.<namespace>.<name>(...)); a non-callable
    has no signature/docstring to render and cannot be called — raise now."""


class MemberConflictError(NamespaceConflictError):
    """a declared member name already exists on the target namespace
    (any existing attribute — a framework member/constant or another plugin's) —
    last-write-wins would silently trample; fail-fast so the plugin author renames."""


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


def install_namespace(
    plugin: str, name: str, module: Any, taken: Mapping[str, str]
) -> Callable[[], None]:
    """Add `ava.<name>` for the agent to call (`ava.<name>.foo()`); returns the undo.

    The namespace is also importable (`import ava.<name>`) for the rest of the process. Boundary: in a
    cold process where the declaring plugin has not been installed yet (bare `import ava`, no agent
    bootstrap), `import ava.<name>` still raises ModuleNotFoundError — import resolution goes through
    sys.modules, not the package's dynamic attribute surface; only attribute access works then. That is
    expected, not a regression.

    Args:
        plugin: the declaring plugin, named in a conflict message.
        name: submodule name — valid Python identifier, no underscore prefix, cannot collide with ava
            builtin modules / top-level attrs (case-sensitive check).
        module: ModuleType or SimpleNamespace — other types (int/None/str/dict) cannot be correctly
            expanded by `help(ava)` and attribute access is inconsistent; raise immediately.
        taken: namespace name -> the plugin that installed it earlier in this install.

    Raises:
        InvalidNamespaceNameError: `name` is not a valid identifier or starts with underscore.
        InvalidNamespaceModuleError: `module` is not ModuleType / SimpleNamespace.
        PluginNamespaceConflictError: `name` already installed by another plugin.
        FrameworkNamespaceConflictError: `name` already taken by a framework-builtin submodule or
            top-level attr, or disabled by AVA_SDK_DISABLE.
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
            f"plugin {plugin!r} namespace {name!r}: module must be ModuleType or "
            f"SimpleNamespace, got {type(module).__name__} — agent uses "
            f"ava.{name}.X requiring attr access, and help(ava) must be able to introspect."
        )

    pkg = ava_module()
    if name in taken:
        raise PluginNamespaceConflictError(
            f"ava.{name} already installed by plugin {taken[name]!r} — same-name override not allowed; "
            f"plugin {plugin!r} must rename."
        )
    if hasattr(pkg, name):
        raise FrameworkNamespaceConflictError(
            f"ava.{name} already exists (framework-builtin submodule or top-level attribute); "
            f"plugin {plugin!r} cannot override."
        )
    # AVA_SDK_DISABLE may have replaced the sys.modules entry with a disabled-module sentinel before the
    # install (the env is applied at `import ava`). The install must NOT clobber the sentinel — it is the
    # legible "disabled" error surface the disable machinery put there, and a disabled name is
    # framework-owned (not overridable), so fail loud with the same conflict class.
    if isinstance(_sys.modules.get(f"ava.{name}"), _DisabledSDKModule):
        raise FrameworkNamespaceConflictError(
            f"ava.{name} is disabled by AVA_SDK_DISABLE — a disabled namespace is framework-owned and not overridable; plugin {plugin!r} must rename."
        )

    if isinstance(module, SimpleNamespace):
        module = _materialize_namespace(name, module)
    # Register under `ava.<name>` in sys.modules so the namespace is a real importable submodule:
    # `import ava.<name>` / `from ava import <name>` resolve through sys.modules (LLM agents write
    # `import ava.memory` out of Python habit). The same object is set on the package, so `ava.<name>`
    # and `import ava.<name>` always agree.
    _sys.modules[f"ava.{name}"] = module
    setattr(pkg, name, module)
    # For `help()` rendering — module's real `__name__` is the plugin's internal path
    # (`plugins.X.Y`); `_qualname` is the agent-facing real name (`ava.<name>`), which also lets
    # `_target_depth` compute heading depth correctly. Underscore prefix avoids being treated as an
    # exposable attr by child discovery.
    module._qualname = f"ava.{name}"  # type: ignore[attr-defined]
    added_to_surface = name not in pkg.__all_for_ava__
    if added_to_surface:
        pkg.__all_for_ava__.append(name)

    def undo() -> None:
        _remove_attr(pkg, name)
        if added_to_surface:
            _remove_from_surface(pkg.__all_for_ava__, name)
        # Drop the importable alias — otherwise `import ava.<name>` keeps returning the stale module
        # after the namespace is torn down.
        _sys.modules.pop(f"ava.{name}", None)
        # The shared module object outlives the namespace (a plugin keeps it); drop the agent-facing
        # name stamp so a later install of the same object starts clean.
        module.__dict__.pop("_qualname", None)

    return undo


def check_expansion(path: str) -> None:
    """Refuse a dotted SDK path that cannot be promoted into the expanded SDK reference.

    Raises:
        ValueError: the path is empty or has an invalid / underscore segment.
    """
    segments = path.split(".")
    if not path or not all(s.isidentifier() and not s.startswith("_") for s in segments):
        raise ValueError(
            f"sdk expansion path {path!r} is invalid — dotted identifiers "
            "without underscore prefixes (e.g. 'cwd', 'shell.sessions')."
        )


def install_member(plugin: str, namespace: str, name: str, fn: Any) -> Callable[[], None]:
    """Attach a callable as a member of an existing ava namespace; the agent invokes it via
    `ava.{namespace}.{name}(...)`. Returns the undo.

    Prefer this over a new namespace when a capability belongs under an existing group — the ava top
    level is deliberately small, so a new self-action goes on `ava.self` next to terminate / restart /
    compact, not as `ava.<thing>`.

    Args:
        plugin: the declaring plugin, named in a conflict message.
        namespace: an existing ava namespace to extend (e.g. "self").
        name: member name — valid identifier, no underscore prefix, must not collide with an existing
            member of that namespace.
        fn: the callable the agent calls. Its signature + docstring are what the agent reads under
            `help(ava.{namespace})`, so the docstring obeys the same agent-facing rules as any SDK
            function.

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
            f"plugin {plugin!r} member {namespace}.{name} must be callable, "
            f"got {type(fn).__name__} — the agent calls ava.{namespace}.{name}(...)."
        )

    pkg = ava_module()
    parent = getattr(pkg, namespace, None)
    # Member discovery (help) + this attach both rely on the parent being a real module with an
    # `__all_for_ava__` whitelist; SimpleNamespace plugin namespaces and disabled-module sentinels are
    # not valid member hosts.
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
            f"same-name override not allowed; plugin {plugin!r} renames."
        )

    setattr(parent, name, fn)
    parent.__all_for_ava__.append(name)

    def undo() -> None:
        # parent gone is reachable, not a bug: a member can sit on a *plugin* namespace whose own undo
        # already ran; the whole module is being discarded, so the member goes with it.
        if getattr(pkg, namespace, None) is parent:
            _remove_attr(parent, name)
            _remove_from_surface(getattr(parent, "__all_for_ava__", None), name)

    return undo


def _remove_attr(obj: Any, name: str) -> None:
    """Delete `name` from `obj` when present."""
    if hasattr(obj, name):
        delattr(obj, name)


def _remove_from_surface(surface: list[str] | None, name: str) -> None:
    """Remove `name` from an `__all_for_ava__`-style surface list when it is
    one (the member host may be gone, in which case there is nothing to do)."""
    if isinstance(surface, list) and name in surface:
        surface.remove(name)
