import sys as _sys
from types import ModuleType as _ModuleType
from types import SimpleNamespace
from typing import Any, cast

# Runtime connections — `ava.DB`, `ava.REDIS` — are the bound context's clients
# (`ava.context.sql` / `.redis`), served by the module class below. The agent's own identity
# (AGENT_ID) lives under ava.self alongside ava.self.MACHINE_SPEC, not here.
#
# DB_URL / REDIS_URL / GATEWAY_URL are *not* re-exported here as module
# attributes — they live on ava.sdk_surface.settings as lazy __getattr__ entries that
# read the current settings.X on each access. This module's own __getattr__
# (defined below) forwards `ava.DB_URL` etc. to _settings, preserving the
# external API while removing the "must mutate settings before import"
# invariant.
from base.agents.context import AvaContext

from .sdk_surface import process_context

# ── SDK entry machinery — implementations in `ava/sdk_surface/` ─────────────
#
# `ava/sdk_surface/` keeps this file a readable coordinator: `const` (the
# `ava.const()` factory — imported here, before the submodule imports, so
# top-level const assignments like `ava.self.AGENT_ID = ava.const(...)` work
# during submodule load), `sdk_disable` (AVA_SDK_DISABLE), `discovery`
# (children discovery + `agent_visible_names`), `help` (the `ava.help()`
# renderer), and `plugins` (the plugin registration API). The plugin-author
# entry points are re-exported here; framework controls (the render
# contextvars, the SDK-disable entries) are imported from their own module.
from .sdk_surface.const import const as const
from .sdk_surface.discovery import agent_visible_names as agent_visible_names
from .sdk_surface.help import help as help
from .sdk_surface.plugins import FrameworkNamespaceConflictError as FrameworkNamespaceConflictError
from .sdk_surface.plugins import InvalidNamespaceMemberError as InvalidNamespaceMemberError
from .sdk_surface.plugins import InvalidNamespaceModuleError as InvalidNamespaceModuleError
from .sdk_surface.plugins import InvalidNamespaceNameError as InvalidNamespaceNameError
from .sdk_surface.plugins import MemberConflictError as MemberConflictError
from .sdk_surface.plugins import NamespaceConflictError as NamespaceConflictError
from .sdk_surface.plugins import PluginNamespaceConflictError as PluginNamespaceConflictError
from .sdk_surface.plugins import RegisterNamespaceError as RegisterNamespaceError
from .sdk_surface.plugins import UnknownNamespaceError as UnknownNamespaceError

# ── Framework-internal state slot ──────────────────────────────────────────
#
# **Framework-internal. Plugin authors do not directly touch these two
# attributes — exec-side state read/write goes through `agent.state.PluginStateHandle`
# (`read` / `update`), and host-side logic (graph hooks) never touches them at all: it reads
# the graph `state` argument and returns an update dict.** The framework itself (the exec
# child, an external attachment) and the handle internals use them to pass the exec turn's
# working copy + delta dict; the slot lives in the exec child, which rebuilds it from the
# request envelope before agent code runs.
#
# Lifecycle (framework side):
#   Before agent code runs, the exec child (after loading plugins) sets
#   `ava.state = <snapshot validated from the request envelope>` +
#   `ava.state_update = {}` (`agent/execution/child.py:_build_state_slot`).
#   handle.read reads ava.state; handle.update synchronously mutates the
#   ava.state working copy + accumulates raw delta into ava.state_update.
#   When the child exits it writes state_update into its result envelope; exec_node
#   reads it from there and merges it into `Command(update=...)`, going through
#   the LangGraph reducer. Nothing resets the slots in the child: they are discarded
#   with the process. An external attachment unbinds them at detach. No other process
#   — the agent host included — ever binds them.
#
# Under the LangGraph cycling topology, nodes run sequentially, no
# cross-turn race; if parallel branch (fan-out) is introduced in the
# future, this one-slot model must be re-evaluated.


class PluginStateOutsideTurnError(AttributeError):
    """`ava.state` / `ava.state_update` (or a `PluginStateHandle` read / update) touched
    outside an exec turn — the framework binds the state slot only in the exec child that
    runs agent code.

    An AttributeError, so the attribute simply does not exist elsewhere. Common misuse: a
    plugin reads state at module load time, or from a graph hook in the agent host — host
    logic reads the graph `state` argument its hook was handed, never the SDK's slot.
    """


# ── The exec turn's state slot ───────────────────────────────────────────────
# `ava.state` / `ava.state_update` exist only while an exec turn is bound: reading either
# anywhere else raises `PluginStateOutsideTurnError`, never a None. The two values live under
# private keys of this module's own dict; the properties below are the only readers and
# writers, and `in_exec_turn()` / `unbind_exec_turn()` are the framework's explicit questions
# and ends of a turn. A bind is plain assignment — `ava.state = <snapshot>`,
# `ava.state_update = {}` — and the exec child dies with its slot.
_STATE_KEY = "_exec_state"
_UPDATE_KEY = "_exec_state_update"
_CONTEXT_KEY = "_exec_context"

state: Any
state_update: dict[str, Any] | None

context: AvaContext
"""Your execution identity and supplied service connections.

`ava.self.AGENT_ID` identifies the agent your SDK calls represent.
`context.identity` records execution ownership and caller provenance. Compact,
restart and terminate require that you own the represented agent's native loop.

Use `context.sql`, `context.redis` and `context.gateway` for database, Redis and
gateway API access. Unavailable when no execution context is established.
"""


def _outside_exec_turn(name: str) -> PluginStateOutsideTurnError:
    return PluginStateOutsideTurnError(
        f"ava.{name} exists only inside execute_code (an exec turn); this process has no bound "
        "turn state. Host code reads the graph state its hook was handed instead."
    )


class _SdkModule(_ModuleType):
    """The `ava` module, whose three framework slots use process-local private keys."""

    @property
    def state(self) -> Any:
        value = self.__dict__.get(_STATE_KEY)
        if value is None:
            raise _outside_exec_turn("state")
        return value

    @state.setter
    def state(self, value: Any) -> None:
        if value is None:
            raise TypeError("ava.state cannot be None; end a turn with ava.unbind_exec_turn()")
        self.__dict__[_STATE_KEY] = value

    @property
    def state_update(self) -> Any:
        if self.__dict__.get(_STATE_KEY) is None:
            raise _outside_exec_turn("state_update")
        # Whatever the exec code left there: the exec child validates it is a dict at exit.
        return self.__dict__.get(_UPDATE_KEY)

    @state_update.setter
    def state_update(self, value: Any) -> None:
        self.__dict__[_UPDATE_KEY] = value

    @property
    def context(self) -> AvaContext:
        bound = self.__dict__.get(_CONTEXT_KEY)
        if bound is process_context.SdkProcessPurpose.SHARED_HOST:
            raise process_context.ContextOutsideProcessError(
                "the shared agent host has no SDK process context; "
                "host code uses the context its caller supplied"
            )
        if bound is not None:
            return bound
        launched = process_context.launched_context()
        if launched is None:
            raise process_context.ContextOutsideProcessError(
                "ava.context requires an execution child, launched script or external attachment; "
                "host code uses the context its caller supplied"
            )
        import atexit

        self.context = launched
        atexit.register(launched.clients.close)
        return launched

    @context.setter
    def context(self, value: AvaContext) -> None:
        if is_host_process():
            raise RuntimeError("the shared agent host cannot bind an SDK process context")
        if not isinstance(value, AvaContext):
            raise TypeError("ava.context must be an AvaContext")
        self.__dict__[_CONTEXT_KEY] = value

    @context.deleter
    def context(self) -> None:
        if is_host_process():
            raise RuntimeError("the shared agent host cannot release its startup posture")
        self.__dict__[_CONTEXT_KEY] = None

    @property
    def DB(self) -> Any:  # noqa: N802 — the SDK's name for the SQL connection
        return self.context.sql

    @property
    def REDIS(self) -> Any:  # noqa: N802 — the SDK's name for the Redis client
        return self.context.redis

    def __dir__(self) -> list[str]:
        return sorted({*super().__dir__(), "DB", "REDIS", "context", "state", "state_update"})


_sys.modules[__name__].__class__ = _SdkModule


def bind_host_process() -> None:
    """Establish shared-host startup posture before plugins or graph work.

    This process never binds a current agent to the SDK, even if its environment
    inherited an agent id. The posture lasts until this interpreter exits.
    """
    sdk = cast(_SdkModule, _sys.modules[__name__])
    bound = sdk.__dict__.get(_CONTEXT_KEY)
    if bound is not None and bound is not process_context.SdkProcessPurpose.SHARED_HOST:
        raise RuntimeError("cannot boot a shared host with an established SDK process context")
    sdk.__dict__[_CONTEXT_KEY] = process_context.SdkProcessPurpose.SHARED_HOST


def is_host_process() -> bool:
    """Whether this interpreter explicitly booted as the shared host."""
    return globals().get(_CONTEXT_KEY) is process_context.SdkProcessPurpose.SHARED_HOST


def bind_context(context: AvaContext) -> None:
    """Initialize this process's SDK entry with an explicit context.

    Framework-internal: used by disposable children and exclusive attachments,
    never by the shared host to select its current agent.
    """
    cast(_SdkModule, _sys.modules[__name__]).context = context


def unbind_context() -> AvaContext | None:
    """Release the SDK binding without creating a launched-script context.

    The caller owns restoration and closing its clients.
    """
    sdk = cast(_SdkModule, _sys.modules[__name__])
    context = sdk.__dict__.get(_CONTEXT_KEY)
    del sdk.context
    return context


def in_exec_turn() -> bool:
    """Whether this process is running an exec turn — the framework has bound its state slot.

    The one explicit answer for the SDK's own call sites (cwd-aware wraps, the security
    scan): true in an exec child while agent code runs and in an attached external
    controller, false in the agent host and in bare scripts. Framework-internal — not in the
    `ava.help()` view.
    """
    return globals().get(_STATE_KEY) is not None


def unbind_exec_turn() -> None:
    """End the bound turn: drop both slot values (an external attachment's detach, a test)."""
    globals().pop(_STATE_KEY, None)
    globals().pop(_UPDATE_KEY, None)


# Plugin-load state lives in the SDK installation slot (`ava.sdk_surface.install`):
# an `Installation` once loaded (`.faces` = the agent-runtime faces — state fields /
# hooks / prompt sections, loaded on the full path via
# `ensure_plugins_loaded(surface=False)`), `load_attempted()` true while a load is in
# flight; after a failure it re-raises the original error. The agent process does NOT go through this path (it calls
# `agent.extensions.load_extensions` directly from build_graph / host boot and
# re-registers built-in hooks after), so a genuinely-unknown `ava.X` keeps failing fast
# in `__getattr__` there; nothing to latch in this module.

# False until this module finishes importing its own submodules (set True at the
# very bottom). The lazy plugin load in `__getattr__` MUST stay dormant during
# `import ava`: a `from . import agents` here triggers `__getattr__('agents')`
# via importlib's fromlist probe, and in a launched child (AVA_AGENT_ID already
# in the env) that would run `load_extensions()` against a half-initialized
# `ava` singleton — the reverse `ava -> agent` import on an incomplete module
# the history doc rejected. Gating on this flag keeps `import ava` byte-identical
# to before; the lazy path only arms once the module is whole.
_init_complete = False


def ensure_plugins_loaded(*, surface: bool = True) -> None:
    """Idempotently load plugin namespaces (`ava.tasks` etc.) into *this* process.

    The entry point for a process an agent launched: a watcher / schedule
    bootstrap calls it explicitly before running agent code, and a persistent-shell
    child reaches it lazily from `__getattr__` on the first unknown `ava.X`.
    `surface=True` (the default) loads the plugin *surfaces* only — the child
    contract (task #3633). `surface=False` loads the full agent-runtime faces
    too (state fields / hooks / prompt sections), called when a stateful
    child's lazy state slot resolves: the state schema needs the plugins'
    field registrations. Each stage runs at most once per process — the load state
    is the installation slot (`ava.sdk_surface.install`): `installed()` answers
    "loaded", `faces` "and the faces", `load_attempted()` "in flight, or attempted
    and failed (no retry)"; the failed state preserves the original exception. A call that arrives while the loader module is still
    initializing (a re-entrant import) defers instead, so a later miss retries
    once the module is complete.

    The loader lives in the agent layer; it is reached via `importlib` (a runtime
    string, not a static `from agent import`) so this ava-layer module keeps NO
    static dependency on agent — the layering contract stays intact while the
    launched subprocess still self-loads its plugins.

    Expected typed declaration refusals are isolated by the installer after a
    successful rollback. Unknown inventory, configuration, programming or I/O
    failures propagate unchanged; a later call re-raises the same failure without
    retrying the load. The loader's earlier per-module import containment is a
    separate boundary in `agent.extensions`.
    """
    from .sdk_surface import install as _sdk_install
    from .sdk_surface import sdk_disable

    _sdk_install.raise_load_failure()
    installation = _sdk_install.installed()
    if installation is not None and (surface or installation.faces):
        return
    if _sdk_install.load_attempted():
        return  # An actual re-entrant miss while a load is in flight.
    import importlib

    if installation is None:
        # Operator errors in AVA_SDK_DISABLE are fatal, before plugin admission.
        sdk_disable.apply_entries(sdk_disable.env_entries())
    _sdk_install.mark_load_attempt()
    try:
        loader = importlib.import_module("agent.extensions")
        if getattr(loader.__spec__, "_initializing", False):
            # Python returned the module during its own import: no load ran yet.
            _sdk_install.clear_load_attempt()
            return
        if installation is not None:
            loader.load_agent_faces()
            _sdk_install.mark_faces_loaded()
        else:
            from base import paths
            from base.config import settings
            from base.config.service_read import ConfigAuthority
            from base.lm.plugin_providers import build_model_catalog

            def complete_read_model() -> Any:
                from base.config import Settings

                return Settings(profile=None)

            authority = ConfigAuthority.deferred(
                runtime=settings,
                build_all_domains=complete_read_model,
                env_path=paths.ava_home() / ".env",
            )
            loader.load_extensions(
                surface=surface, catalog=build_model_catalog(), authority=authority
            )
            if not surface:
                _sdk_install.mark_faces_loaded()
    except BaseException as exc:
        _sdk_install.mark_load_failed(exc)
        raise


def _maybe_load_plugins_for_missing(name: str) -> bool:
    """Env-gated lazy plugin load, shared by `ava.__getattr__` and the framework
    namespace modules that plugins extend (`ava.self`, `ava.ui`).

    A persistent-shell child an agent launched runs a bare `python x.py`
    (no bootstrap to hook), so a plugin namespace
    (`ava.tasks`) or a plugin member on an existing namespace (`ava.ui.notify`,
    `ava.self.set_label` from ava_fleet) would AttributeError. On the first such miss,
    load plugins once — then the caller retries the lookup.

    Returns True iff this call loaded plugins (caller should re-attempt
    `getattr`); a deferral — the loader module is still importing — returns
    False so the caller fails fast now and a later miss retries. Fires only in
    an agent-launched child (`agent_identity.is_launched_child`), only after `import ava`
    is complete (`_init_complete`), only while no load is installed, in flight or
    failed, and never for underscore names — so gateway / cli / the agent process
    keep fail-fast on a genuinely-unknown attribute, `import ava` is untouched,
    and a dunder probe never triggers a load.
    """
    if name.startswith("_") or not _init_complete:
        return False
    from .sdk_surface import install as _sdk_install

    _sdk_install.raise_load_failure()
    if _sdk_install.installed() is not None or _sdk_install.load_attempted():
        return False
    from .sdk_surface import agent_identity

    if not agent_identity.is_launched_child():
        return False
    ensure_plugins_loaded()
    return _sdk_install.installed() is not None


# PEP 562 module-level `__getattr__`. Plugins set runtime attributes via
# the SDK install (a real setattr), so this fall-through is
# only hit for genuinely unknown names — we want fail-fast there.
# Its presence tells pyright the module supports dynamic attributes, so test
# files that reference plugin-registered names (e.g. `ava.cwd` from the
# ava_code plugin) don't trip `reportAttributeAccessIssue`.
#
# DB_URL / REDIS_URL / GATEWAY_URL forward to ava.sdk_surface.settings (which in turn
# reads the live `settings.X`). Putting the forward here rather than copying
# into the module dict keeps every access fresh — code that mutates
# settings.data_plane.db_url at runtime (test conftest, eval driver) is immediately
# visible to `ava.DB_URL` readers without import-order gymnastics.
def __getattr__(name: str) -> Any:
    if name in ("state", "state_update"):
        # Reached only when the slot property raised (an AttributeError falls through to here).
        raise _outside_exec_turn(name)
    if name == "context":
        raise process_context.ContextOutsideProcessError(
            "ava.context requires an execution child, launched script or external attachment; "
            "host code uses the context its caller supplied"
        )
    if name == "DB":
        return _sys.modules[__name__].context.sql
    if name == "REDIS":
        return _sys.modules[__name__].context.redis
    if name == "external":
        import importlib

        module = importlib.import_module("ava.external")
        setattr(_sys.modules[__name__], name, module)
        return module
    if name in ("DB_URL", "REDIS_URL", "GATEWAY_URL"):
        from .sdk_surface import settings as _settings

        return getattr(_settings, name)
    if _maybe_load_plugins_for_missing(name):
        return getattr(_sys.modules[__name__], name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# isort: split

# Submodule imports must come after DB / REDIS (they read these
# globals when importing ava). The `# isort: split` above prevents ruff
# from merging / reordering the two import blocks.
# `ava.sdk_surface.wraps` and `ava.sdk_surface.plugin_loader` are public names
# (agent visibility is the `__all_for_ava__` whitelist below, not the
# underscore) reached across the `ava` package boundary by the agent kernel
# (`agent/state.py`, `agent/process_boot.py`, `agent/extensions/__init__.py`).
# `wraps`' curated plugin-author surface is assembled as `ava.extend` further
# down.
# ruff: noqa: E402 — submodule imports must come after DB/REDIS slot injection
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
from .sdk_surface import attachment_transport as attachment_transport
from .sdk_surface import wraps as _wraps
from .understand import understand as understand

# ── ava.extend — the plugin extension surface ──────────────────────────────
# Curated view of `ava.sdk_surface.wraps` for plugin authors: the wrap-stack
# introspection (a plugin *declares* a wrap in `contribute()`; the SDK install applies
# it). Deliberately NOT added to `__all_for_ava__` — this is a plugin-author API,
# so it stays out of the `help(ava)` view the agent sees.
extend = SimpleNamespace(
    stack=_wraps.stack,
    wrappers=_wraps.wrappers,
)
extend._qualname = "ava.extend"  # type: ignore[attr-defined]  # agent-facing name for help() resolution

# Agent-visible top-level surface — the namespaces + `help` the agent sees in
# `help(ava)`. This is NOT Python's `__all__` (this package declares none —
# nothing does `from ava import *`, and the module's re-exports use redundant-
# alias imports which the type checker already honors). `const` / `extend` / the
# exception classes are deliberately absent: they are plugin-author / framework API,
# importable but out of the agent's view. The SDK install (`ava.sdk_surface.install`)
# appends plugin namespaces to this list and the env's AVA_SDK_DISABLE entries
# (applied first, inside install()) remove from it, so it must be defined before
# the SDK install runs.
__all_for_ava__ = [
    "agents",
    "context",
    "files",
    "help",
    "mcps",
    "self",
    "shell",
    "skills",
    "ui",
    "understand",
    "watcher",
    "web",
]

# The agent-facing FQN a help() heading shows comes from `fn.__module__`
# (`ava.help` → `# ava.help`). The implementations live in `ava/sdk_surface/`
# modules, so the re-exported entry points get the package-level `__module__`
# back and `help(ava.X)` headings read `ava.X`. (`ava.understand` keeps its own
# `ava.understand` module path.)
for _entry in (
    help,
    const,
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
# Only an agent-launched child reaches this load — the one import-time trigger
# the ambient-state lint keeps for the ava package. Metering installs with the
# SDK surface (`ava.sdk_surface.install.install`), not here.
from .sdk_surface import agent_identity

if agent_identity.is_launched_child():
    ensure_plugins_loaded()
