"""Construct process-local SDK contexts at their execution entry points.

The execution child, schedule runner and external attachment publish their context
on the local ``ava`` module. Hosted graph runs receive it through LangGraph Runtime
and never publish an agent identity on that shared module. This module owns no
current context, thread patch or live clients.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Any

from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet, DatabaseFactory
from base.agents.context.identity import AgentIdentity
from base.native_process.runtime_incarnation import RuntimeIncarnation


class SdkProcessPurpose(StrEnum):
    """Non-agent startup posture stored in the SDK's existing process entry."""

    SHARED_HOST = "shared_host"


class ContextOutsideProcessError(AttributeError):
    """The SDK's local context was read outside an execution process."""


def _redis() -> object:
    import redis as redis_lib

    from base.config import settings
    from base.events.live.redis_client import RESILIENCE_KWARGS

    url = settings.data_plane.redis_url
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


def _gateway_url() -> str:
    from base.cluster.machine import gateway_api_base

    return gateway_api_base()


def _gateway(url: str) -> Any:
    import httpx

    from base.cluster.auth import bearer_header
    from base.cluster.machine import gateway_bearer
    from base.config import settings
    from base.host.net.http_dial import transport_for_url

    bearer = gateway_bearer()
    return httpx.Client(
        base_url=url,
        timeout=httpx.Timeout(settings.gateway.gateway_client_http_timeout_seconds),
        headers=bearer_header(bearer) if bearer else {},
        transport=transport_for_url(url),
    )


def process_clients(
    *, gateway_url: str | None = None, database: DatabaseFactory | None = None
) -> ClientSet:
    """Build lazy process clients; resolve configuration and credentials only at first use."""
    from ava.sdk_surface import settings

    return ClientSet(
        gateway_url=gateway_url if gateway_url is not None else _gateway_url,
        database=database if database is not None else settings.database,
        redis=_redis,
        gateway=_gateway,
    )


def context_from_description(
    description: dict[str, Any], *, original_incarnation: RuntimeIncarnation | None = None
) -> AvaContext:
    """Rebuild an execution context without copying its host's live clients or secrets."""
    return AvaContext.from_description(
        description,
        clients=process_clients(gateway_url=description["gateway_url"]),
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
