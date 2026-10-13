"""The process-local SDK entry: its context and exec-turn state slots, and the contexts built for them.

The execution child, schedule runner and external attachment publish their context
here; ``ava.context`` / ``ava.state`` / ``ava.state_update`` on the ``ava`` module are attribute
delegations to the slots below. Hosted graph runs receive their context through LangGraph Runtime
and never publish an agent identity: a shared host only records its posture (`bind_host_process`).
The slots are kept in the ``ava`` module object's own dictionary (as the SDK installation is), so
reloading ``ava`` neither resets nor duplicates them. This module owns no thread patch or live
clients; the clients a context carries are built lazily by `process_clients`.

Framework-internal. Plugin authors do not touch the slots: exec-side state read/write goes through
`agent.state.PluginStateHandle`, and host-side logic (graph hooks) never touches them at all - it
reads the graph `state` argument and returns an update dict.

Lifecycle of the exec-turn state slot (framework side): before agent code runs, the exec child
(after loading plugins) sets `set_state(<snapshot validated from the request envelope>)` and
`set_state_update({})` (`agent/execution/child.py:_build_state_slot`). `PluginStateHandle.read`
reads the state; `.update` synchronously mutates its working copy and accumulates the raw delta
into the update dict. When the child exits it writes the update dict into its result envelope;
exec_node reads it from there and merges it into `Command(update=...)`, going through the
LangGraph reducer. Nothing resets the slots in the child: they are discarded with the process. An
external attachment unbinds them at detach. No other process - the agent host included - ever binds
them. Under the LangGraph cycling topology nodes run sequentially, so there is no cross-turn race;
if parallel branches (fan-out) are introduced, this one-slot model must be re-evaluated.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from enum import StrEnum
from typing import Any, cast

from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet, DatabaseFactory
from base.agents.context.identity import AgentIdentity
from base.clock import Clock, clock_config_from_boot
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.native_process.runtime_incarnation import RuntimeIncarnation

from . import ava_module


class SdkProcessPurpose(StrEnum):
    """Non-agent startup posture stored in the SDK's existing process entry."""

    SHARED_HOST = "shared_host"


class ContextOutsideProcessError(AttributeError):
    """The SDK's local context was read outside an execution process."""


class PluginStateOutsideTurnError(AttributeError):
    """`ava.state` / `ava.state_update` (or a `PluginStateHandle` read / update) touched
    outside an exec turn - the framework binds the state slot only in the exec child that
    runs agent code.

    An AttributeError, so the attribute simply does not exist elsewhere. Common misuse: a
    plugin reads state at module load time, or from a graph hook in the agent host - host
    logic reads the graph `state` argument its hook was handed, never the SDK's slot.
    """


# The three slots live on the `ava` module object itself, like the installation
# (`installation.SLOT`): the SDK entry is the one process-wide thing they describe, and its
# dictionary survives `importlib.reload(ava)`. `_context` holds a bound `AvaContext`, the shared
# host's posture marker or nothing; `_state` / `_state_update` exist only while an exec turn is
# bound. Reading the state slots anywhere else raises `PluginStateOutsideTurnError`, never a None.
_CONTEXT_KEY = "_exec_context"
_STATE_KEY = "_exec_state"
_UPDATE_KEY = "_exec_state_update"


def _slots() -> dict[str, Any]:
    return ava_module().__dict__


def _bound() -> AvaContext | SdkProcessPurpose | None:
    return _slots().get(_CONTEXT_KEY)


def outside_exec_turn(name: str) -> PluginStateOutsideTurnError:
    return PluginStateOutsideTurnError(
        f"ava.{name} exists only inside execute_code (an exec turn); this process has no bound "
        "turn state. Host code reads the graph state its hook was handed instead."
    )


def outside_process(name: str) -> ContextOutsideProcessError:
    return ContextOutsideProcessError(
        f"ava.{name} requires an execution child, launched script or external attachment; "
        "host code uses the context its caller supplied"
    )


def get_state() -> Any:
    """The exec turn's working state, or `PluginStateOutsideTurnError` outside a turn."""
    value = _slots().get(_STATE_KEY)
    if value is None:
        raise outside_exec_turn("state")
    return value


def set_state(value: Any) -> None:
    """Bind the exec turn's working state; end the turn with `unbind_exec_turn`."""
    if value is None:
        raise TypeError(
            "ava.state cannot be None; end a turn with process_context.unbind_exec_turn()"
        )
    _slots()[_STATE_KEY] = value


def get_state_update() -> Any:
    """Whatever the exec code left in the delta slot (the exec child validates it at exit)."""
    slots = _slots()
    if slots.get(_STATE_KEY) is None:
        raise outside_exec_turn("state_update")
    return slots.get(_UPDATE_KEY)


def set_state_update(value: Any) -> None:
    _slots()[_UPDATE_KEY] = value


def in_exec_turn() -> bool:
    """Whether this process is running an exec turn - the framework has bound its state slot.

    The one explicit answer for the SDK's own call sites (cwd-aware wraps, the security
    scan): true in an exec child while agent code runs and in an attached external
    controller, false in the agent host and in bare scripts. Framework-internal - not in the
    `ava.help()` view.
    """
    return _slots().get(_STATE_KEY) is not None


def unbind_exec_turn() -> None:
    """End the bound turn: drop both slot values (an external attachment's detach, a test)."""
    slots = _slots()
    slots.pop(_STATE_KEY, None)
    slots.pop(_UPDATE_KEY, None)


def is_host_process() -> bool:
    """Whether this interpreter explicitly booted as the shared host."""
    return _bound() is SdkProcessPurpose.SHARED_HOST


def bind_host_process() -> None:
    """Establish shared-host startup posture before plugins or graph work.

    This process never binds a current agent to the SDK, even if its environment
    inherited an agent id. The posture lasts until this interpreter exits.
    """
    bound = _bound()
    if bound is not None and bound is not SdkProcessPurpose.SHARED_HOST:
        raise RuntimeError("cannot boot a shared host with an established SDK process context")
    _slots()[_CONTEXT_KEY] = SdkProcessPurpose.SHARED_HOST


def bound_context() -> AvaContext | None:
    """Inspect the existing entry without turning host posture into a context."""
    bound = _bound()
    return None if isinstance(bound, SdkProcessPurpose) else bound


def current_context() -> AvaContext:
    """The bound context; a launched script's first read builds and binds its own."""
    bound = _bound()
    if bound is SdkProcessPurpose.SHARED_HOST:
        raise ContextOutsideProcessError(
            "the shared agent host has no SDK process context; "
            "host code uses the context its caller supplied"
        )
    if bound is not None:
        return bound
    launched = launched_context()
    if launched is None:
        raise outside_process("context")
    import atexit

    bind_context(launched)
    atexit.register(launched.clients.close)
    return launched


def ambient_context() -> AvaContext | None:
    """The bound context, or None where this process has none (never builds one for a host)."""
    try:
        return current_context()
    except ContextOutsideProcessError:
        return None


def bind_context(context: AvaContext) -> None:
    """Initialize this process's SDK entry with an explicit context.

    Framework-internal: used by disposable children and exclusive attachments,
    never by the shared host to select its current agent.
    """
    if is_host_process():
        raise RuntimeError("the shared agent host cannot bind an SDK process context")
    if not isinstance(context, AvaContext):
        raise TypeError("ava.context must be an AvaContext")
    _slots()[_CONTEXT_KEY] = context


def unbind_context() -> AvaContext | None:
    """Release the SDK binding without creating a launched-script context.

    The caller owns restoration and closing its clients.
    """
    if is_host_process():
        raise RuntimeError("the shared agent host cannot release its startup posture")
    return cast(AvaContext | None, _slots().pop(_CONTEXT_KEY, None))


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

    import ava
    from base.native_process.code_version import CodeVersion

    version = CodeVersion(ava.loaded_code_image())
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
    from ava.gateway_client.transport import GatewayTransportInputs
    from ava.mcps import McpClients

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
