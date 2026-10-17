# meiva:note
# The SDK entry completes its own namespace imports before allowing lazy plugin loading.
# A bare non-agent import leaves context unbound and does not import database, Redis,
# HTTP or model connection stacks. Process clients retain their supplied configuration
# and build connections only on first use; installation clients outlive an attachment,
# while bound calls retain the attachment's own producer. A reload must preserve the
# first-loaded code image and process-context slots instead of relabeling or clearing
# already-running code.
# meiva:link meiva/docs/boot-contracts.meiva.okf.md

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet, DatabaseFactory
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.agents.context.identity import AgentIdentity
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.plugins.loading import StateGate
from meiva.mcps import McpClients
from meiva.sdk_surface import meiva_module, process_context


def _redis(*, url_reader: Callable[[], str]) -> object:
    import redis as redis_lib

    from base.events.live.redis_client import RESILIENCE_KWARGS

    url = url_reader()
    if not url:
        raise RuntimeError(
            "MEIVA_REDIS_URL not set — Redis ops should not be called in container mode"
        )
    client_class: Any = redis_lib.Redis
    return client_class.from_url(
        url,
        decode_responses=True,
        **{**RESILIENCE_KWARGS, "socket_timeout": 10.0},
    )


def _gateway_url(config: ConfigBoot) -> str:
    from base.cluster.machine import gateway_api_base

    return gateway_api_base(config.view.gateway.gateway_url)


def _gateway(url: str, *, config: ConfigBoot, timeout_reader: Callable[[], float]) -> Any:
    import httpx

    from base.cluster.machine import gateway_bearer
    from base.cluster.machine.boot import gateway_access_from_boot
    from base.host.net.auth import bearer_header
    from base.host.net.http_dial import transport_for_url

    bearer = gateway_bearer(gateway_access_from_boot(config))
    return httpx.Client(
        base_url=url,
        timeout=httpx.Timeout(timeout_reader()),
        headers=bearer_header(bearer),
        transport=transport_for_url(url),
    )


def database_factory(*, config: ConfigBoot, machine: MachineIdentity) -> DatabaseFactory:
    from threading import Lock

    from base.native_process.code_version import CodeVersion

    version = CodeVersion(process_context.loaded_code_image())
    lock = Lock()

    gate: Any = None

    def database() -> Any:
        nonlocal gate
        from base.cluster.dataplane.direct_url import ClusterDirectUrl
        from base.db import Database
        from base.db.code_version_gate import ProcessDbGate
        from base.db.config import db_config_from_boot

        if not config.view.data_plane.db_url:
            raise RuntimeError(
                "MEIVA_DB_URL not set — SQL ops should not be called in container mode"
            )
        with lock:
            if gate is None:
                gate = ProcessDbGate(version=version.get, process="unknown")
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(
                machine.host, gateway_url_reader=lambda: config.view.gateway.gateway_url
            ),
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
        from base.paths.home import resolve_meiva_home
        from base.telemetry import build_pipeline

        read_config()
        return build_pipeline(database=make_database, machine=identity, home=resolve_meiva_home())

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
) -> MeivaContext:
    owner = config if config is not None else ConfigBoot()
    if config is None:
        owner.read_process_environment()
    machine = machine_from_boot(owner)
    return MeivaContext.from_description(
        description,
        clients=process_clients(
            gateway_url=description["gateway_url"], config=owner, machine=machine
        ),
        machine=machine,
        original_incarnation=original_incarnation,
    )


def has_launched_identity() -> bool:
    return "MEIVA_AGENT_ID" in os.environ


def launched_context(*, config: ConfigBoot | None = None) -> MeivaContext | None:
    raw = os.environ.get("MEIVA_AGENT_ID")
    if raw is None:
        return None
    if config is None:
        config = ConfigBoot()
    config.read_process_environment()
    machine = machine_from_boot(config)
    return MeivaContext(
        identity=AgentIdentity(agent_id=int(raw), owns_loop=False),
        clients=process_clients(config=config, machine=machine),
        machine=machine,
        clock_factory=lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
    )


def _door_import_complete() -> bool:
    return bool(meiva_module().__dict__.get("_init_complete", False))


def ensure_plugins_loaded(
    *,
    surface: bool = True,
    config: ConfigBoot | None = None,
    clock_factory: Callable[[], Clock] | None = None,
    producer: Callable[[], Any] | None = None,
    machine: MachineIdentity | None = None,
    context: MeivaContext | None = None,
    validate_state: StateGate | None = None,
) -> None:
    if context is not None:
        process_context.bind_context(context)
    else:
        context = process_context.bound_context()
    from meiva.sdk_surface import install as _sdk_install
    from meiva.sdk_surface import sdk_disable
    from meiva.sdk_surface.installation import installed

    _sdk_install.raise_load_failure()
    installation = installed()
    if installation is not None and (surface or installation.faces):
        _bind_installed_catalog(context)
        return
    if _sdk_install.load_attempted():
        return
    if installation is None and config is None and context is None and is_launched_process():
        context, config = _prepare_launched_context()
    if context is not None:
        clock_factory = clock_factory or context.clock_factory
        machine = machine if machine is not None else context.machine
    if installation is None:
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
    from base.packages.plugins.loading.faces import load_agent_faces
    from meiva.sdk_surface import install as _sdk_install
    from meiva.sdk_surface.extensions import load_extensions

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

            if producer is None:
                clients = process_clients(config=config, machine=machine)
                atexit.register(clients.close)
                producer = clients.event_pipeline
            authority = ConfigAuthority.deferred(
                runtime=config.view,
                build_all_domains=config.complete_read_model,
                env_path=paths.meiva_home() / ".env",
            )
            load_extensions(
                surface=surface,
                catalog=build_model_catalog(),
                authority=authority,
                producer=producer,
                clock_factory=clock_factory
                or (lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS)),
                validate_state=validate_state,
            )
            if not surface:
                _sdk_install.mark_faces_loaded()
    except BaseException as exc:
        _sdk_install.mark_load_failed(exc)
        raise


def initialize_context(
    context: MeivaContext,
    *,
    surface: bool = True,
    config: ConfigBoot | None = None,
    clock_factory: Callable[[], Clock] | None = None,
    producer: Callable[[], Any] | None = None,
    validate_state: StateGate | None = None,
) -> None:
    ensure_plugins_loaded(
        context=context,
        surface=surface,
        config=config,
        clock_factory=clock_factory or context.clock_factory,
        machine=context.machine,
        producer=producer,
        validate_state=validate_state,
    )


def _bind_installed_catalog(context: MeivaContext | None) -> None:
    if context is None:
        return
    from meiva.sdk_surface import settings as _settings
    from meiva.sdk_surface.installation import installed

    if installed() is not None:
        catalog = _settings.model_catalog()
        if context.catalog is not catalog:
            process_context.bind_context(replace(context, catalog=catalog))


def _prepare_launched_context() -> tuple[MeivaContext, ConfigBoot]:
    config = ConfigBoot()
    context = launched_context(config=config)
    if context is None:
        raise RuntimeError("launched process identity disappeared during SDK initialization")
    import atexit

    atexit.register(context.clients.close)
    process_context.bind_context(context)
    return context, config


def is_launched_process() -> bool:
    if process_context.is_host_process():
        return False
    bound = process_context.bound_context()
    if bound is not None:
        identity = bound.identity
        return identity is not None and identity.agent_id is not None and not identity.owns_loop
    return has_launched_identity()


def maybe_load_plugins_for_missing(name: str) -> bool:
    if name.startswith("_") or not _door_import_complete():
        return False
    from meiva.sdk_surface import install as _sdk_install
    from meiva.sdk_surface.installation import installed

    _sdk_install.raise_load_failure()
    if installed() is not None or _sdk_install.load_attempted():
        return False
    from meiva.sdk_surface import agent_identity

    if not agent_identity.is_launched_child():
        return False
    ensure_plugins_loaded()
    return installed() is not None


def bind_launched_context() -> MeivaContext | None:
    import atexit

    launched = launched_context()
    if launched is None:
        return None
    process_context.bind_context(launched)
    atexit.register(launched.clients.close)
    return launched
