import sys as _sys
from types import SimpleNamespace
from typing import Any

# ── SDK entry machinery — implementations in `ava/_exports/` ────────────────
#
# The entry surface moved into `ava/_exports/` modules to keep this file a
# readable coordinator: `const` (the `ava.const()` factory — imported here,
# before the submodule imports, so top-level const assignments like
# `ava.self.AGENT_ID = ava.const(...)` work during submodule load),
# `sdk_disable` (AVA_SDK_DISABLE), `discovery` (children discovery +
# `agent_visible_names`), `help` (the `ava.help()` renderer), and `plugins`
# (the plugin registration API). Each name is re-exported with a redundant
# alias so the external API — `import ava.X`, `ava.help`,
# `ava.register_namespace`, `ava._apply_sdk_disable`, the `ava._format_*`
# helpers tests reach — is unchanged.
from ._exports.const import const as const
from ._exports.discovery import _classify_dir_entry as _classify_dir_entry
from ._exports.discovery import _Constant as _Constant
from ._exports.discovery import _hidden_surface_members as _hidden_surface_members
from ._exports.discovery import _module_attribute_annotations as _module_attribute_annotations
from ._exports.discovery import _module_attribute_docs as _module_attribute_docs
from ._exports.discovery import _module_children as _module_children
from ._exports.discovery import agent_visible_names as agent_visible_names
from ._exports.help import _COMPACT_CLASSES as _COMPACT_CLASSES
from ._exports.help import _format_docstring as _format_docstring
from ._exports.help import _format_documented_const_stub as _format_documented_const_stub
from ._exports.help import _format_signature as _format_signature
from ._exports.help import help as help
from ._exports.plugins import _REGISTERED_NAMESPACES as _REGISTERED_NAMESPACES
from ._exports.plugins import _REGISTERED_SDK_EXPANSIONS as _REGISTERED_SDK_EXPANSIONS
from ._exports.plugins import FrameworkNamespaceConflictError as FrameworkNamespaceConflictError
from ._exports.plugins import InvalidNamespaceMemberError as InvalidNamespaceMemberError
from ._exports.plugins import InvalidNamespaceModuleError as InvalidNamespaceModuleError
from ._exports.plugins import InvalidNamespaceNameError as InvalidNamespaceNameError
from ._exports.plugins import MemberConflictError as MemberConflictError
from ._exports.plugins import NamespaceConflictError as NamespaceConflictError
from ._exports.plugins import PluginNamespaceConflictError as PluginNamespaceConflictError
from ._exports.plugins import RegisterNamespaceError as RegisterNamespaceError
from ._exports.plugins import UnknownNamespaceError as UnknownNamespaceError
from ._exports.plugins import clear_registered_namespaces as clear_registered_namespaces
from ._exports.plugins import register_namespace as register_namespace
from ._exports.plugins import register_namespace_member as register_namespace_member
from ._exports.plugins import register_sdk_expand as register_sdk_expand
from ._exports.sdk_disable import _applied_disable_entries as _applied_disable_entries
from ._exports.sdk_disable import _apply_sdk_disable as _apply_sdk_disable
from ._exports.sdk_disable import _DisabledSDKModule as _DisabledSDKModule
from ._exports.sdk_disable import _sdk_disable_entries as _sdk_disable_entries

# Runtime connections — DB, Redis. The agent's own identity (AGENT_ID) lives
# under ava.self alongside ava.self.MACHINE_SPEC, not here.
#
# DB_URL / REDIS_URL / GATEWAY_URL are *not* re-exported here as module
# attributes — they live on ava._settings as lazy __getattr__ entries that
# read the current settings.X on each access. This module's own __getattr__
# (defined below) forwards `ava.DB_URL` etc. to _settings, preserving the
# external API while removing the "must mutate settings before import"
# invariant.
from ._settings import DB as DB
from ._settings import REDIS as REDIS

# ── Framework-internal state slot ──────────────────────────────────────────
#
# **Framework-internal. Plugin authors do not directly touch these two
# module attributes — all state read/write goes through
# `agent.state.PluginStateHandle` (the typed handle returned by
# `register_plugin_state(Cls)`)**. These two attributes exist because the
# framework itself (primarily `agent/graph/_exec.py:_exec_node_impl`) and
# the handle internals rely on them to pass the working copy + delta dict;
# a module-level slot is simpler than ContextVar — the slot lives in the
# exec child, which rebuilds it from the request envelope before agent code
# runs; the parent process never touches it.
#
# Lifecycle (framework side):
#   Before agent code runs, the exec child sets
#   `ava.state = <snapshot validated from the request envelope>` +
#   `ava.state_update = {}` (`agent/exec_child.py:_build_state_slot`).
#   handle.read reads ava.state; handle.update synchronously mutates the
#   ava.state working copy + accumulates raw delta into ava.state_update.
#   At turn end, exec_node takes state_update, merges into
#   `Command(update=...)` going through the LangGraph reducer; both slots
#   reset back to None.
#
# Under the LangGraph cycling topology, nodes run sequentially, no
# cross-turn race; if parallel branch (fan-out) is introduced in the
# future, this module-level slot model must be re-evaluated.


class PluginStateOutsideTurnError(Exception):
    """`PluginStateHandle.read()` / `.update()` called outside an exec turn —
    framework hasn't injected the state slot.

    Common misuse: plugin calls handle.read at module load time (during
    `import`); at that moment exec_node hasn't set the slot. Plugins
    should only access state inside hook callbacks or wrapped SDK
    functions (running inside execute_code).
    """


# Framework-internal slots. Plugin code does not read/write directly — go through PluginStateHandle.
state: Any = None
state_update: dict[str, Any] | None = None


# Per-process latch: True once this process has loaded plugin namespaces via the
# subprocess self-load path (`_ensure_plugins_loaded`). Framework-internal — only
# `_ensure_plugins_loaded` writes it. The agent process does NOT go through that
# path (it calls `agent.graph._build._load_extensions` directly from build_graph
# and re-registers built-in hooks after), so the latch stays False there and a
# genuinely-unknown `ava.X` keeps failing fast in `__getattr__`.
_plugins_loaded = False

# False until this module finishes importing its own submodules (set True at the
# very bottom). The lazy plugin load in `__getattr__` MUST stay dormant during
# `import ava`: a `from . import agents` here triggers `__getattr__('agents')`
# via importlib's fromlist probe, and in a launched child (AVA_AGENT_ID already
# in the env) that would run `_load_extensions()` against a half-initialized
# `ava` singleton — the reverse `ava -> agent` import on an incomplete module
# the history doc rejected. Gating on this flag keeps `import ava` byte-identical
# to before; the lazy path only arms once the module is whole.
_init_complete = False


def _ensure_plugins_loaded() -> None:
    """Idempotently load plugin namespaces (`ava.tasks` etc.) into *this* process.

    The entry point for a process an agent launched: a watcher / schedule
    bootstrap calls it explicitly before running agent code, and a persistent-shell
    child reaches it lazily from `__getattr__` on the first unknown `ava.X`.
    Latched so it runs at most once per process.

    The loader lives in the agent layer; it is reached via `importlib` (a runtime
    string, not a static `from agent import`) so this ava-layer module keeps NO
    static dependency on agent — the layering contract stays intact while the
    launched subprocess still self-loads its plugins.
    """
    global _plugins_loaded  # noqa: PLW0603 — one-shot per-process latch
    if _plugins_loaded:
        return
    # Latch BEFORE loading: a plugin's top-level access of a not-yet-registered
    # `ava.X` re-enters `__getattr__` during the load, and the latch makes that
    # re-entry fail fast (as it does in the agent process) instead of recursing.
    _plugins_loaded = True
    import importlib

    importlib.import_module("agent.graph._build")._load_extensions()


def _maybe_load_plugins_for_missing(name: str) -> bool:
    """Env-gated lazy plugin load, shared by `ava.__getattr__` and the framework
    namespace modules that plugins extend (`ava.self`, `ava.ui`).

    A persistent-shell child an agent launched runs a bare `python x.py`
    (no bootstrap to hook, and the session env allowlist does NOT carry
    AVA_AGENT_ID — Task #856), so a plugin namespace
    (`ava.tasks`) or a plugin member on an existing namespace (`ava.ui.notify`,
    `ava.self.set_label` from ava_fleet) would AttributeError. On the first such miss,
    load plugins once — then the caller retries the lookup.

    Returns True iff a load just ran (caller should re-attempt `getattr`), False
    to fall through to the module's own fail-fast AttributeError. Fires only in
    an agent-launched child (`_boot.is_launched_child`), only after `import ava`
    is complete (`_init_complete`), only once (`_plugins_loaded`), and never for
    underscore names — so gateway / cli / the agent process keep fail-fast on a
    genuinely-unknown attribute, `import ava` is untouched, and a dunder probe
    never triggers a load.
    """
    if name.startswith("_") or not _init_complete or _plugins_loaded:
        return False
    from . import _boot

    if not _boot.is_launched_child():
        return False
    _ensure_plugins_loaded()
    return True


# PEP 562 module-level `__getattr__`. Plugins set runtime attributes via
# `ava.register_namespace("X", module)` (real setattr), so this fall-through is
# only hit for genuinely unknown names — we want fail-fast there.
# Its presence tells pyright the module supports dynamic attributes, so test
# files that reference plugin-registered names (e.g. `ava.code` from the
# ava_code plugin) don't trip `reportAttributeAccessIssue`.
#
# DB_URL / REDIS_URL / GATEWAY_URL forward to ava._settings (which in turn
# reads the live `settings.X`). Putting the forward here rather than copying
# into the module dict keeps every access fresh — code that mutates
# settings.data_plane.db_url at runtime (test conftest, eval driver) is immediately
# visible to `ava.DB_URL` readers without import-order gymnastics.
def __getattr__(name: str) -> Any:
    if name == "external":
        import importlib

        module = importlib.import_module("ava.external")
        setattr(_sys.modules[__name__], name, module)
        return module
    if name in ("DB_URL", "REDIS_URL", "GATEWAY_URL"):
        from . import _settings

        return getattr(_settings, name)
    if _maybe_load_plugins_for_missing(name):
        return getattr(_sys.modules[__name__], name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# isort: split

# Submodule imports must come after DB / REDIS (they read these
# globals when importing ava). The `# isort: split` above prevents ruff
# from merging / reordering the two import blocks.
# `_extend` intentionally carries the underscore prefix: it's a plugin-author +
# framework module, should not appear in the `help()` view the agent sees. Its
# curated author surface is assembled as `ava.extend` further down.
# ruff: noqa: E402 — submodule imports must come after DB/REDIS slot injection
from . import _attach as _attach
from . import _extend as _extend
from . import agents as agents
from . import files as files
from . import impersonation as impersonation
from . import mcps as mcps
from . import self as self
from . import shell as shell
from . import skills as skills
from . import ui as ui
from . import watcher as watcher
from . import web as web
from .understand import understand as understand

# ── ava.extend — the plugin extension surface ──────────────────────────────
# Curated view of `_extend` for plugin authors: the wrap registration primitive
# plus its introspection. Deliberately NOT added to `__all_for_ava__` (and not a
# `register_namespace` call) — this is a plugin-author API, so it stays out of
# the `help(ava)` view the agent sees, the same posture as the `_extend` module
# itself. `_extend.scan_and_load` / `clear_wraps` are framework-internal and
# reached via `ava._extend`, so they are absent from this surface.
extend = SimpleNamespace(
    wrap=_extend.wrap,
    stack=_extend.stack,
    wrappers=_extend.wrappers,
)
extend._qualname = "ava.extend"  # type: ignore[attr-defined]  # agent-facing name for help() resolution

# Agent-visible top-level surface — the namespaces + `help` the agent sees in
# `help(ava)`. This is NOT Python's `__all__` (this package declares none —
# nothing does `from ava import *`, and the module's re-exports use redundant-
# alias imports which the type checker already honors). `register_namespace` /
# `const` / `extend` / the exception classes are deliberately absent: they are
# plugin-author / framework API, importable but out of the agent's view.
# `register_namespace` appends to this list and AVA_SDK_DISABLE removes from it,
# so it must be defined before `_apply_sdk_disable` runs.
__all_for_ava__ = [
    "agents",
    "files",
    "help",
    "impersonation",
    "mcps",
    "self",
    "shell",
    "skills",
    "ui",
    "understand",
    "watcher",
    "web",
]

# Apply env-based entries at import time (existing behavior)
_apply_sdk_disable(_sdk_disable_entries)

# The agent-facing FQN a help() heading shows comes from `fn.__module__`
# (`ava.help` → `# ava.help`). The implementations moved into `ava/_exports/`
# modules, so restore the package-level `__module__` on the re-exported entry
# points that used to be defined here — keeps `help(ava.X)` headings
# byte-identical to before the split. (`ava.understand` keeps its own
# pre-existing `ava.understand` module path.)
for _entry in (
    help,
    const,
    register_namespace,
    register_sdk_expand,
    register_namespace_member,
    clear_registered_namespaces,
):
    _entry.__module__ = "ava"

# Module is fully imported now — arm the lazy plugin-namespace load in
# `__getattr__` (kept dormant above so `import ava` never triggers it).
_init_complete = True

# Eager load for agent-launched children (a bare `python x.py` in a persistent
# shell session has no bootstrap to hook). The lazy-on-miss path above cannot
# cover plugin WRAPPERS on existing members — `ava.agents.spawn(label=...)`
# resolves the unwrapped core function without ever missing, then TypeErrors —
# so such a child loads plugins at import, and lazy-on-miss stays as the
# backstop. The agent host binds identities per turn and does not
# export a process-wide AVA_AGENT_ID; gateway / cli do not carry it either.
# Only an agent-launched child reaches this load.
from . import _boot as _boot_module

if _boot_module.is_launched_child():
    _ensure_plugins_loaded()
