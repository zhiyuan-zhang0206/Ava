"""Explicit configuration ownership and keyed agent creation for an assembled Gateway."""

import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx
import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER
from tests.fixtures.configuration import snapshot_process_config

__all__ = [
    "SPAWN_PATH",
    "create_mcp_client",
    "gateway_test_client",
    "post_spawn",
    "spawn_headers",
    "spawn_via_http",
]

SPAWN_PATH = "/api/keyed/v1/agents"


@contextmanager
def gateway_test_client(application: FastAPI, **kwargs: Any) -> Generator[TestClient]:
    """Run the real Gateway lifespan with this test's current configuration inputs.

    Only assembled Gateway apps belong here. Startup-delivery tests and local
    FastAPI stand-ins keep their own explicit composition boundaries. The client
    is authenticated by default (`tests.fixtures.unit.gateway_auth`).
    """
    from gateway import app as gateway_app

    with (
        patch.object(gateway_app, "ConfigBoot", snapshot_process_config),
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
