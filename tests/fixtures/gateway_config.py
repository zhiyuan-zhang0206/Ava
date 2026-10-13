"""Explicit configuration ownership and keyed agent creation for an assembled Gateway."""

import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager, nullcontext
from typing import Any
from unittest.mock import patch

import httpx
import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER
from base.cluster.machine import MachineIdentity
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from tests.fixtures.configuration import snapshot_process_config

__all__ = [
    "SPAWN_PATH",
    "create_mcp_client",
    "gateway_test_client",
    "gateway_test_process",
    "post_spawn",
    "spawn_headers",
    "spawn_via_http",
]

SPAWN_PATH = "/api/keyed/v1/agents"


@contextmanager
def gateway_test_process(
    application: FastAPI, *, machine: MachineIdentity, process: GatewayProcess | None = None
) -> Generator[GatewayProcess]:
    """Own one explicit entry, or borrow it across overlapping app lifespans.

    Each lifespan still owns its original pools and tasks. A compatible routing
    app can borrow the same entry without replacing or closing its writer.
    """
    if process is None:
        process = getattr(application.state, "gateway_process_input", None)
    owned = process is None
    if owned:
        process = GatewayProcess(
            config=snapshot_process_config(), image=LOADED_IMAGE, machine=machine
        )
    assert process is not None
    with (
        process.lifetime() if owned else nullcontext(process),
        patch.object(
            application.state,
            "gateway_process_input",
            process,
            create=not hasattr(application.state, "gateway_process_input"),
        ),
    ):
        yield process


@contextmanager
def gateway_test_client(
    application: FastAPI,
    *,
    machine: MachineIdentity,
    process: GatewayProcess | None = None,
    **kwargs: Any,
) -> Generator[TestClient]:
    """Run an assembled Gateway with this test's current explicit entry inputs.

    Startup-delivery tests and local FastAPI stand-ins keep their own roots.
    Borrowed entries retain their configuration and writer; only the outer
    context closes resources it constructed. The client is authenticated by
    default (`gateway.tests.agents.gateway_auth`).
    """
    with (
        gateway_test_process(application, machine=machine, process=process),
        TestClient(application, **kwargs) as client,
    ):
        yield client


def spawn_headers(key: str | None = None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Admission headers for one creation intent (a fresh key unless one is given)."""
    return {
        **(extra or {}),
        "Idempotency-Key": key if key is not None else uuid.uuid4().hex,
        SCOPE_HEADER: PRINCIPAL_SCOPE,
    }


def spawn_via_http(
    client: TestClient,
    body: Mapping[str, Any] | None = None,
    *,
    key: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> httpx2.Response:
    """POST one creation intent through the in-process gateway; the response is unparsed.

    Each call is a new intent unless the caller passes the same `key` to replay one.
    """
    return client.post(SPAWN_PATH, json=dict(body or {}), headers=spawn_headers(key, headers))


def post_spawn(gateway_url: str, body: Mapping[str, Any], *, timeout: float) -> httpx.Response:
    """POST one creation intent to a running gateway (e2e stacks)."""
    return httpx.post(
        f"{gateway_url}{SPAWN_PATH}", json=dict(body), headers=spawn_headers(), timeout=timeout
    )


def create_mcp_client(
    client: TestClient,
    name: str,
    scope: str = "read",
    *,
    headers: Mapping[str, str] | None = None,
) -> httpx2.Response:
    """POST one fresh MCP client creation intent; only this first response carries the token."""
    return client.post(
        "/api/keyed/v1/mcp/clients",
        json={"name": name, "scope": scope},
        headers={
            "Idempotency-Key": uuid.uuid4().hex,
            SCOPE_HEADER: PRINCIPAL_SCOPE,
            **(headers or {}),
        },
    )
