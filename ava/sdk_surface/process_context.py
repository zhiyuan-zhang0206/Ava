"""Construct process-local SDK contexts at their execution entry points.

The execution child, schedule runner and external attachment publish their context
on the local ``ava`` module. Hosted graph runs receive it through LangGraph Runtime
and never publish an agent identity on that shared module. This module owns no
current context, thread patch or live clients.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet, DatabaseFactory
from base.agents.context.identity import AgentIdentity
from base.config import ConfigBoot
from base.native_process.runtime_incarnation import RuntimeIncarnation


class SdkProcessPurpose(StrEnum):
    """Non-agent startup posture stored in the SDK's existing process entry."""

    SHARED_HOST = "shared_host"


class ContextOutsideProcessError(AttributeError):
    """The SDK's local context was read outside an execution process."""


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

    from base.cluster.auth import bearer_header, delivered_token
    from base.cluster.machine import resolve_gateway_bearer
    from base.host.env.bootstrap import config_source_is_local
    from base.host.env.dotenv_boot import launcher_context
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
        headers=bearer_header(bearer) if bearer else {},
        transport=transport_for_url(url),
    )


def process_clients(
    *,
    gateway_url: str | None = None,
    database: DatabaseFactory | None = None,
    mcp_timeout_seconds: Callable[[], float] | None = None,
    config: ConfigBoot | None = None,
) -> ClientSet:
    """Build lazy process clients without reading their configuration or credentials.

    The MCP timeout reader is passed to each freshly built MCP client, which reads it
    at its operation and session boundaries. Omission uses this root's ConfigBoot view.
    """
    from ava.gateway_client.transport import GatewayTransportInputs
    from ava.mcps import McpClients
    from ava.sdk_surface.settings import database_factory

    owner = config if config is not None else ConfigBoot()
    timeout_reader = (
        mcp_timeout_seconds
        if mcp_timeout_seconds is not None
        else lambda: owner.view.sandbox.mcp_connect_timeout_seconds
    )

    def pipeline() -> Any:
        from base.telemetry import build_pipeline

        return build_pipeline(database=make_database)

    make_database = database if database is not None else database_factory(config=owner)
    return ClientSet(
        gateway_url=gateway_url if gateway_url is not None else lambda: _gateway_url(owner),
        database=make_database,
        pipeline_factory=pipeline,
        redis=lambda: _redis(url_reader=lambda: owner.view.data_plane.redis_url),
        gateway=lambda url: _gateway(
            url,
            config=owner,
            timeout_reader=lambda: owner.view.gateway.gateway_client_http_timeout_seconds,
        ),
        factories={
            McpClients: lambda: McpClients(timeout_reader),
            GatewayTransportInputs: lambda: GatewayTransportInputs(
                max_retries_reader=lambda: owner.view.gateway.gateway_client_max_retries,
                retry_delay_reader=lambda: owner.view.gateway.gateway_client_retry_delay_seconds,
                memory_deadline_reader=lambda: owner.view.services.memory_search_deadline_seconds,
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
    return AvaContext.from_description(
        description,
        clients=process_clients(gateway_url=description["gateway_url"], config=config),
        original_incarnation=original_incarnation,
    )


def launched_context() -> AvaContext | None:
    """Construct a launched script's context from its explicit environment channel.

    The SDK entry rejects shared-host startup posture before calling this.
    This function never infers identity from a native turn.
    Identity binding precedes plugin installation; the execution root attaches
    its catalog after that installation completes.
    """
    raw = os.environ.get("AVA_AGENT_ID")  # env-ok: launched script identity channel
    if raw is None:
        return None
    return AvaContext(
        identity=AgentIdentity(agent_id=int(raw), owns_loop=False), clients=process_clients()
    )
