import sys as _sys
from collections.abc import Callable
from dataclasses import replace as _replace
from pathlib import Path as _Path
from types import ModuleType as _ModuleType
from types import SimpleNamespace
from typing import Any

from base.native_process.loaded_commit import LoadedCommit as _LoadedCommit
from base.native_process.loaded_commit import capture_commit as _capture_commit

# This immutable value belongs to the SDK module's first code load, before any
# database or plugin use. Reloading a surface must not relabel already-loaded
# Python code with a checkout that moved later. It owns no clients or gate.
if "_LOADED_IMAGE" not in globals():
    _source_root = _Path(__file__).resolve().parents[1]
    _LOADED_IMAGE = _LoadedCommit(source_root=_source_root, sha=_capture_commit(_source_root))


def loaded_code_image() -> _LoadedCommit:
    """Return the immutable image captured at this SDK module's first load.

    Process composition roots consume this fact without rereading Git or
    relabeling already-loaded Python after a checkout move or surface reload.
    """
    return _LOADED_IMAGE


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
from base.clock import Clock, clock_config_from_boot
from base.cluster.machine import MachineIdentity as _MachineIdentity
from base.config import ConfigBoot

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

# ── The SDK entry's slots ───────────────────────────────────────────────────
#
# `ava.context`, `ava.state` and `ava.state_update` are attribute delegations to the slots in
# `ava.sdk_surface.process_context`, which owns their storage, lifecycle and errors. The values
# are not kept in this module's dictionary, so `importlib.reload(ava)` neither resets nor
# duplicates them. An assignment such as `ava.state = <snapshot>` or `ava.context = ctx` goes
# through the property setters below; `del ava.context` releases the binding.

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


class _SdkModule(_ModuleType):
    """The `ava` module, whose three framework slots delegate to `process_context`."""

    @property
    def state(self) -> Any:
        return process_context.get_state()

    @state.setter
    def state(self, value: Any) -> None:
        process_context.set_state(value)

    @property
    def state_update(self) -> Any:
        return process_context.get_state_update()

    @state_update.setter
    def state_update(self, value: Any) -> None:
        process_context.set_state_update(value)

    @property
    def context(self) -> AvaContext:
        return process_context.current_context()

    @context.setter
    def context(self, value: AvaContext) -> None:
        process_context.bind_context(value)

    @context.deleter
    def context(self) -> None:
        process_context.unbind_context()

    @property
    def DB(self) -> Any:  # noqa: N802 — the SDK's name for the SQL connection
        return self.context.sql

    @property
    def REDIS(self) -> Any:  # noqa: N802 — the SDK's name for the Redis client
        return self.context.redis

    def __dir__(self) -> list[str]:
        return sorted({*super().__dir__(), "DB", "REDIS", "context", "state", "state_update"})


_sys.modules[__name__].__class__ = _SdkModule


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


def ensure_plugins_loaded(
    *,
    surface: bool = True,
    config: ConfigBoot | None = None,
    clock_factory: Callable[[], Clock] | None = None,
    producer: Callable[[], Any] | None = None,
    machine: _MachineIdentity | None = None,
    context: AvaContext | None = None,
) -> None:
    """Idempotently load plugin namespaces (`ava.tasks` etc.) into *this* process.

    The entry point for a process an agent launched: a watcher / schedule
    bootstrap calls it explicitly before running agent code. A persistent-shell
    child completes the same composition before its first parent import returns;
    `__getattr__` remains the backstop for a deferred plugin load.
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

    A supplied context binds before loading and receives the installed catalog.
    A launched process with no context first constructs one from the same boot
    owner used for its installation. Startup identity inspection does not invoke
    the ambient context getter. An explicit composition root supplies its
    configuration, clock and optional machine owner. If no producer is supplied, the installation retains its own
    cold clients with those inputs until process exit; it never borrows an
    attachment's shorter-lived clients. Bound SDK calls use their own context's
    producer instead.
    """
    if context is not None:
        process_context.bind_context(context)
    else:
        context = process_context.bound_context()
    from .sdk_surface import install as _sdk_install
    from .sdk_surface import sdk_disable
    from .sdk_surface.installation import installed

    _sdk_install.raise_load_failure()
    installation = installed()
    if installation is not None and (surface or installation.faces):
        _bind_installed_catalog(context)
        return
    if _sdk_install.load_attempted():
        return  # An actual re-entrant miss while a load is in flight.
    if installation is None and config is None and context is None and _is_launched_process():
        context, config = _prepare_launched_context()
    if context is not None:
        clock_factory = clock_factory or context.clock_factory
        machine = machine if machine is not None else context.machine
    if installation is None:
        # Operator errors in AVA_SDK_DISABLE are fatal, before plugin admission.
        sdk_disable.apply_entries(sdk_disable.env_entries())
    _sdk_install.mark_load_attempt()
    _load_plugin_stages(
        upgrade_faces=installation is not None,
        surface=surface,
        config=config,
        clock_factory=clock_factory,
        machine=machine,
        producer=producer,
    )
    _bind_installed_catalog(context)


def _load_plugin_stages(
    *,
    upgrade_faces: bool,
    surface: bool,
    config: ConfigBoot | None,
    clock_factory: Callable[[], Clock] | None,
    machine: _MachineIdentity | None,
    producer: Callable[[], Any] | None,
) -> None:
    """Run the existing installation stage and retain its original load failure."""
    import importlib

    from .sdk_surface import install as _sdk_install

    try:
        loader = importlib.import_module("agent.extensions")
        if getattr(loader.__spec__, "_initializing", False):
            # Python returned the module during its own import: no load ran yet.
            _sdk_install.clear_load_attempt()
            return
        if upgrade_faces:
            loader.load_agent_faces()
            _sdk_install.mark_faces_loaded()
        else:
            from base import paths
            from base.config.service_read import ConfigAuthority
            from base.lm.plugin_providers import build_model_catalog

            if config is None:
                config = ConfigBoot()
                config.read_process_environment()
            import atexit

            # The installation outlives attachments. Bound calls capture their
            # own context's producer; bare calls keep this distinct cold owner.
            if producer is None:
                clients = process_context.process_clients(config=config, machine=machine)
                atexit.register(clients.close)
                producer = clients.event_pipeline
            authority = ConfigAuthority.deferred(
                runtime=config.view,
                build_all_domains=config.complete_read_model,
                env_path=paths.ava_home() / ".env",
            )
            loader.load_extensions(
                surface=surface,
                catalog=build_model_catalog(),
                authority=authority,
                producer=producer,
                clock_factory=clock_factory or (lambda: Clock(clock_config_from_boot(config))),
            )
            if not surface:
                _sdk_install.mark_faces_loaded()
    except BaseException as exc:
        _sdk_install.mark_load_failed(exc)
        raise


def initialize_context(
    context: AvaContext,
    *,
    surface: bool = True,
    config: ConfigBoot | None = None,
    clock_factory: Callable[[], Clock] | None = None,
    producer: Callable[[], Any] | None = None,
) -> None:
    """Bind a root-supplied context and complete its SDK installation.

    Framework-internal: callers validate their request or lease first and own
    context-client cleanup and prior-context restoration. Installation producers
    keep their separate lifetime. Shared hosts cannot bind an ambient context.
    """
    ensure_plugins_loaded(
        context=context,
        surface=surface,
        config=config,
        clock_factory=clock_factory or context.clock_factory,
        machine=context.machine,
        producer=producer,
    )


def _bind_installed_catalog(context: AvaContext | None) -> None:
    """Complete a bound context only after its installation is actually ready."""
    if context is None:
        return
    from .sdk_surface import settings as _settings
    from .sdk_surface.installation import installed

    # A loader still being imported deferred its work; it is not ready yet.
    if installed() is not None:
        catalog = _settings.model_catalog()
        if context.catalog is not catalog:
            process_context.bind_context(_replace(context, catalog=catalog))


def _prepare_launched_context() -> tuple[AvaContext, ConfigBoot]:
    """Capture one boot owner and publish its cold launched context."""
    config = ConfigBoot()
    context = process_context.launched_context(config=config)
    if context is None:
        raise RuntimeError("launched process identity disappeared during SDK initialization")
    import atexit

    atexit.register(context.clients.close)
    process_context.bind_context(context)
    return context, config


def _is_launched_process() -> bool:
    """Inspect startup identity without triggering the ambient context getter."""
    if process_context.is_host_process():
        return False
    bound = process_context.bound_context()
    if bound is not None:
        identity = bound.identity
        return identity is not None and identity.agent_id is not None and not identity.owns_loop
    return process_context.has_launched_identity()


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
    from .sdk_surface.installation import installed

    _sdk_install.raise_load_failure()
    if installed() is not None or _sdk_install.load_attempted():
        return False
    from .sdk_surface import agent_identity

    if not agent_identity.is_launched_child():
        return False
    ensure_plugins_loaded()
    return installed() is not None


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
        raise process_context.outside_exec_turn(name)
    if name == "context":
        raise process_context.outside_process("context")
    if name == "DB":
        return _sys.modules[__name__].context.sql
    if name == "REDIS":
        return _sys.modules[__name__].context.redis
    if name == "external":
        # Plain `import` binds the submodule on the package; `from . import external`
        # would re-enter this `__getattr__`.
        import ava.external

        return ava.external
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
# The formal execution entry validates its request and supplies its own boot
# owner before loading the surface. An arbitrary request env string in a bare
# launched script cannot select this posture; the interpreter entry owns it.
if (
    getattr(getattr(_sys.modules.get("__main__"), "__spec__", None), "name", None)
    != "agent.execution.child"
    and _is_launched_process()
):
    ensure_plugins_loaded()
