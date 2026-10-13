"""Boot this process's SDK entry: its lazy client set, a launched script's context, plugin loading.

`ava/__init__.py` calls `ensure_plugins_loaded` once a launched process has imported the SDK; the
execution child, the schedule runner and an external attachment call it (or `initialize_context`)
with the inputs they own. The slots a context is published into live in
`ava.sdk_surface.process_context`. This module sits above the namespaces whose clients it
configures (`ava.mcps`, `ava.gateway_client`) and below the namespaces and roots that boot a process.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ava.gateway_client.transport import GatewayTransportInputs
from ava.mcps import McpClients
from ava.sdk_surface import ava_module, process_context
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet, DatabaseFactory
from base.agents.context.identity import AgentIdentity
from base.clock import Clock, clock_config_from_boot
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.plugins.loading import StateGate


def _redis(*, url_reader: Callable[[], str]) -> object:
    import redis as redis_lib

    from base.events.live.redis_client import RESILIENCE_KWARGS

    url = url_reader()
    if not url:
        raise RuntimeError(
            "AVA_REDIS_URL not set — Redis ops should not be called in container mode"
        )
    client_class: Any = redis_lib.Redis
    return client_class.from_url(
        url,
        decode_responses=True,
        **{**RESILIENCE_KWARGS, "socket_timeout": 10.0},
    )


def _gateway_url(config: ConfigBoot) -> str:
    from base.cluster.machine import resolve_gateway_api_base

    return resolve_gateway_api_base(config.view.gateway.gateway_url)


def _gateway(url: str, *, config: ConfigBoot, timeout_reader: Callable[[], float]) -> Any:
    import httpx

    from base.cluster.machine import resolve_gateway_bearer
    from base.host.env.bootstrap import config_source_is_local
    from base.host.env.dotenv_boot import launcher_context
    from base.host.net.auth import bearer_header, delivered_token
    from base.host.net.http_dial import transport_for_url

    bearer = resolve_gateway_bearer(
        token_reader=delivered_token,
        secret_reader=lambda: config.view.data_plane.cluster_secret,
        profile_reader=launcher_context,
        no_tokens_reader=lambda: config_source_is_local() and config.view.data_plane.is_remote,
    )
    return httpx.Client(
        base_url=url,
        timeout=httpx.Timeout(timeout_reader()),
        headers=bearer_header(bearer),
        transport=transport_for_url(url),
    )


def database_factory(*, config: ConfigBoot, machine: MachineIdentity) -> DatabaseFactory:
    """Retain one lazy database gate on this client set, without importing its dial stack."""
    from threading import Lock

    from base.native_process.code_version import CodeVersion

    version = CodeVersion(process_context.loaded_code_image())
    lock = Lock()
    # The dial stack stays cold until the first database request. The factory,
    # not a module slot or an independently returned handle, owns this gate.
    gate: Any = None

    def database() -> Any:
        nonlocal gate
        from base.cluster.dataplane.direct_url import ClusterDirectUrl
        from base.db import Database
        from base.db.code_version_gate import ProcessDbGate
        from base.db.config import db_config_from_boot

        if not config.view.data_plane.db_url:
            raise RuntimeError(
                "AVA_DB_URL not set — SQL ops should not be called in container mode"
            )
        with lock:
            if gate is None:
                gate = ProcessDbGate(version=version.get, process="unknown")
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    return database


def process_clients(
    *,
    gateway_url: str | None = None,
    database: DatabaseFactory | None = None,
    mcp_timeout_seconds: Callable[[], float] | None = None,
    config: ConfigBoot | None = None,
    machine: MachineIdentity | None = None,
) -> ClientSet:
    """Build lazy process clients without reading their configuration or credentials.

    Explicit startup owners retain their live views. Omission captures the already
    delivered environment at the first resource read; SDK use never redelivers it.
    """
    owner = config if config is not None else ConfigBoot()
    identity = machine if machine is not None else machine_from_boot(owner)

    def read_config() -> ConfigBoot:
        if config is None:
            owner.read_process_environment()
        return owner

    timeout_reader = (
        mcp_timeout_seconds
        if mcp_timeout_seconds is not None
        else lambda: read_config().view.sandbox.mcp_connect_timeout_seconds
    )

    def pipeline() -> Any:
        from base.paths.home import resolve_ava_home
        from base.telemetry import build_pipeline

        read_config()
        return build_pipeline(database=make_database, machine=identity, home=resolve_ava_home())

    database_builder = (
        database if database is not None else database_factory(config=owner, machine=identity)
    )

    def default_database() -> Any:
        read_config()
        return database_builder()

    make_database = database if database is not None else default_database

    return ClientSet(
        gateway_url=gateway_url if gateway_url is not None else lambda: _gateway_url(read_config()),
        database=make_database,
        pipeline_factory=pipeline,
        redis=lambda: _redis(url_reader=lambda: read_config().view.data_plane.redis_url),
        gateway=lambda url: _gateway(
            url,
            config=read_config(),
            timeout_reader=lambda: read_config().view.gateway.gateway_client_http_timeout_seconds,
        ),
        factories={
            McpClients: lambda: McpClients(timeout_reader),
            GatewayTransportInputs: lambda: GatewayTransportInputs(
                max_retries_reader=lambda: read_config().view.gateway.gateway_client_max_retries,
                retry_delay_reader=lambda: (
                    read_config().view.gateway.gateway_client_retry_delay_seconds
                ),
                memory_deadline_reader=lambda: (
                    read_config().view.services.memory_search_deadline_seconds
                ),
            ),
        },
    )


def context_from_description(
    description: dict[str, Any],
    *,
    original_incarnation: RuntimeIncarnation | None = None,
    config: ConfigBoot | None = None,
) -> AvaContext:
    """Rebuild an execution context without copying its host's live clients or secrets."""
    owner = config if config is not None else ConfigBoot()
    if config is None:
        owner.read_process_environment()
    machine = machine_from_boot(owner)
    return AvaContext.from_description(
        description,
        clients=process_clients(
            gateway_url=description["gateway_url"], config=owner, machine=machine
        ),
        machine=machine,
        original_incarnation=original_incarnation,
    )


def has_launched_identity() -> bool:
    """Inspect the launched identity channel without constructing a context."""
    return "AVA_AGENT_ID" in os.environ  # env-ok: launched script identity channel


def launched_context(*, config: ConfigBoot | None = None) -> AvaContext | None:
    """Construct a launched script's context from its explicit environment channel.

    The SDK entry rejects shared-host startup posture before calling this.
    This function never infers identity from a native turn.
    Identity binding precedes plugin installation; the execution root attaches
    its catalog after that installation completes.
    """
    raw = os.environ.get("AVA_AGENT_ID")  # env-ok: launched script identity channel
    if raw is None:
        return None
    if config is None:
        config = ConfigBoot()
    config.read_process_environment()
    machine = machine_from_boot(config)
    return AvaContext(
        identity=AgentIdentity(agent_id=int(raw), owns_loop=False),
        clients=process_clients(config=config, machine=machine),
        machine=machine,
        clock_factory=lambda: Clock(clock_config_from_boot(config)),
    )


def _door_import_complete() -> bool:
    """Whether `ava/__init__.py` has finished importing (it sets `_init_complete` last).

    The lazy plugin load must stay dormant during `import ava`: a `from . import agents` there
    triggers `__getattr__('agents')` through importlib's fromlist probe, and in a launched child
    (AVA_AGENT_ID already in the env) that would run `load_extensions()` against a
    half-initialized `ava` singleton - the reverse `ava -> agent` import on an incomplete module
    the history doc rejected. Gating on the flag keeps `import ava` byte-identical to before; the
    lazy path only arms once the module is whole."""
    return bool(ava_module().__dict__.get("_init_complete", False))


def ensure_plugins_loaded(
    *,
    surface: bool = True,
    config: ConfigBoot | None = None,
    clock_factory: Callable[[], Clock] | None = None,
    producer: Callable[[], Any] | None = None,
    machine: MachineIdentity | None = None,
    context: AvaContext | None = None,
    validate_state: StateGate | None = None,
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
    and failed (no retry)"; the failed state preserves the original exception.

    The loader is `ava.sdk_surface.extensions.load_extensions` over `base.packages.plugins.loading`; neither imports `agent`. A load
    that imports agent-runtime faces (`surface=False`) validates the state they declare with
    `validate_state` (`agent.state.plugin_state_schema`), which only a caller that runs agent
    code can supply.

    Expected typed declaration refusals are isolated by the installer after a
    successful rollback. Unknown inventory, configuration, programming or I/O
    failures propagate unchanged; a later call re-raises the same failure without
    retrying the load. The loader's earlier per-module import containment is a
    separate boundary in `ava.sdk_surface.extensions`.

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
    from ava.sdk_surface import install as _sdk_install
    from ava.sdk_surface import sdk_disable
    from ava.sdk_surface.installation import installed

    _sdk_install.raise_load_failure()
    installation = installed()
    if installation is not None and (surface or installation.faces):
        _bind_installed_catalog(context)
        return
    if _sdk_install.load_attempted():
        return  # An actual re-entrant miss while a load is in flight.
    if installation is None and config is None and context is None and is_launched_process():
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
        validate_state=validate_state,
    )
    _bind_installed_catalog(context)


def _load_plugin_stages(
    *,
    upgrade_faces: bool,
    surface: bool,
    config: ConfigBoot | None,
    clock_factory: Callable[[], Clock] | None,
    machine: MachineIdentity | None,
    producer: Callable[[], Any] | None,
    validate_state: StateGate | None,
) -> None:
    """Run the existing installation stage and retain its original load failure."""
    from ava.sdk_surface import install as _sdk_install
    from ava.sdk_surface.extensions import load_extensions
    from base.packages.plugins.loading.faces import load_agent_faces

    try:
        if upgrade_faces:
            load_agent_faces()
            _sdk_install.mark_faces_loaded()
        else:
            from base import paths
            from base.config.service_read import ConfigAuthority
            from base.packages.plugin_providers import build_model_catalog

            if config is None:
                config = ConfigBoot()
                config.read_process_environment()
            import atexit

            # The installation outlives attachments. Bound calls capture their
            # own context's producer; bare calls keep this distinct cold owner.
            if producer is None:
                clients = process_clients(config=config, machine=machine)
                atexit.register(clients.close)
                producer = clients.event_pipeline
            authority = ConfigAuthority.deferred(
                runtime=config.view,
                build_all_domains=config.complete_read_model,
                env_path=paths.ava_home() / ".env",
            )
            load_extensions(
                surface=surface,
                catalog=build_model_catalog(),
                authority=authority,
                producer=producer,
                clock_factory=clock_factory or (lambda: Clock(clock_config_from_boot(config))),
                validate_state=validate_state,
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
    validate_state: StateGate | None = None,
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
        validate_state=validate_state,
    )


def _bind_installed_catalog(context: AvaContext | None) -> None:
    """Complete a bound context only after its installation is actually ready."""
    if context is None:
        return
    from ava.sdk_surface import settings as _settings
    from ava.sdk_surface.installation import installed

    # A loader still being imported deferred its work; it is not ready yet.
    if installed() is not None:
        catalog = _settings.model_catalog()
        if context.catalog is not catalog:
            process_context.bind_context(replace(context, catalog=catalog))


def _prepare_launched_context() -> tuple[AvaContext, ConfigBoot]:
    """Capture one boot owner and publish its cold launched context."""
    config = ConfigBoot()
    context = launched_context(config=config)
    if context is None:
        raise RuntimeError("launched process identity disappeared during SDK initialization")
    import atexit

    atexit.register(context.clients.close)
    process_context.bind_context(context)
    return context, config


def is_launched_process() -> bool:
    """Inspect startup identity without triggering the ambient context getter."""
    if process_context.is_host_process():
        return False
    bound = process_context.bound_context()
    if bound is not None:
        identity = bound.identity
        return identity is not None and identity.agent_id is not None and not identity.owns_loop
    return has_launched_identity()


def maybe_load_plugins_for_missing(name: str) -> bool:
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
    if name.startswith("_") or not _door_import_complete():
        return False
    from ava.sdk_surface import install as _sdk_install
    from ava.sdk_surface.installation import installed

    _sdk_install.raise_load_failure()
    if installed() is not None or _sdk_install.load_attempted():
        return False
    from ava.sdk_surface import agent_identity

    if not agent_identity.is_launched_child():
        return False
    ensure_plugins_loaded()
    return installed() is not None


def bind_launched_context() -> AvaContext | None:
    """Build and bind a launched script's context from its environment, or None without one.

    The first read of `ava.context` in a launched process whose import did not already bind it.
    The context's clients close with the interpreter.
    """
    import atexit

    launched = launched_context()
    if launched is None:
        return None
    process_context.bind_context(launched)
    atexit.register(launched.clients.close)
    return launched
