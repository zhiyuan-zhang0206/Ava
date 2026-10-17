import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager, nullcontext
from typing import Any, Literal, Self

import httpx
import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.host.system.tests.fake_commands import fake_host
from base.paths import meiva_home
from gateway.app import create_app
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from gateway.http.config import GatewayConfig, gateway_config_from_boot

__all__ = [
    "SPAWN_PATH",
    "GatewayTestClient",
    "create_mcp_client",
    "gateway_test_app",
    "gateway_test_client",
    "gateway_test_process",
    "post_spawn",
    "spawn_headers",
    "spawn_via_http",
]

SPAWN_PATH = "/api/keyed/v1/agents"


@contextmanager
def gateway_test_process(
    *,
    machine: MachineIdentity | None = None,
    config: ConfigBoot | None = None,
    process: GatewayProcess | None = None,
) -> Generator[GatewayProcess]:
    owned = process is None
    if owned:
        resolved_config = snapshot_process_config() if config is None else config
        resolved_machine = machine_from_boot(resolved_config) if machine is None else machine
        process = GatewayProcess(
            home=meiva_home(),
            config=resolved_config,
            image=LOADED_IMAGE,
            machine=resolved_machine,
            host=fake_host(),
        )
    assert process is not None
    with process.lifetime() if owned else nullcontext(process):
        yield process


def gateway_test_app(
    *,
    process: GatewayProcess | None = None,
    config: GatewayConfig | None = None,
    boot: ConfigBoot | None = None,
    machine: MachineIdentity | None = None,
    process_ownership: Literal["borrowed", "owned"] | None = None,
) -> FastAPI:
    if process is None:
        resolved_boot = snapshot_process_config() if boot is None else boot
        resolved_machine = machine_from_boot(resolved_boot) if machine is None else machine
        process = GatewayProcess(
            home=meiva_home(),
            config=resolved_boot,
            image=LOADED_IMAGE,
            machine=resolved_machine,
            host=fake_host(),
        )
        ownership: Literal["borrowed", "owned"] = (
            "owned" if process_ownership is None else process_ownership
        )
    else:
        ownership = "borrowed" if process_ownership is None else process_ownership
    resolved_config = gateway_config_from_boot(process.config) if config is None else config
    return create_app(
        resolved_config,
        process=process,
        process_ownership=ownership,
    )


class GatewayTestClient(TestClient):
    app: FastAPI

    def __enter__(self) -> Self:
        super().__enter__()
        return self


@contextmanager
def gateway_test_client(
    application: FastAPI | None = None,
    *,
    machine: MachineIdentity | None = None,
    config: GatewayConfig | None = None,
    boot: ConfigBoot | None = None,
    process: GatewayProcess | None = None,
    **kwargs: Any,
) -> Generator[GatewayTestClient]:
    if application is not None:
        if any(arg is not None for arg in (machine, config, boot, process)):
            raise ValueError(
                "gateway_test_client(application, ...) borrows an already assembled FastAPI app; "
                "pass machine/config/boot/process to gateway_test_process or gateway_test_app instead."
            )
        with GatewayTestClient(application, **kwargs) as client:
            yield client
        return
    with gateway_test_process(machine=machine, config=boot, process=process) as active_process:
        assembled = gateway_test_app(process=active_process, config=config)
        with GatewayTestClient(assembled, **kwargs) as client:
            yield client


def spawn_headers(key: str | None = None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
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
    return client.post(SPAWN_PATH, json=dict(body or {}), headers=spawn_headers(key, headers))


def post_spawn(gateway_url: str, body: Mapping[str, Any], *, timeout: float) -> httpx.Response:
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
    return client.post(
        "/api/keyed/v1/mcp/clients",
        json={"name": name, "scope": scope},
        headers={
            "Idempotency-Key": uuid.uuid4().hex,
            SCOPE_HEADER: PRINCIPAL_SCOPE,
            **(headers or {}),
        },
    )
