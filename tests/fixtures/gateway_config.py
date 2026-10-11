"""Explicit configuration ownership for an assembled in-process Gateway."""

from collections.abc import Generator
from contextlib import contextmanager, nullcontext
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from tests.fixtures.configuration import snapshot_process_config

__all__ = ["gateway_test_client", "gateway_test_process"]


@contextmanager
def gateway_test_process(
    application: FastAPI, *, process: GatewayProcess | None = None
) -> Generator[GatewayProcess]:
    """Own one explicit entry, or borrow it across overlapping app lifespans.

    Each lifespan still owns its original pools and tasks. A compatible routing
    app can borrow the same entry without replacing or closing its writer.
    """
    if process is None:
        process = getattr(application.state, "gateway_process_input", None)
    owned = process is None
    if owned:
        process = GatewayProcess(config=snapshot_process_config(), image=LOADED_IMAGE)
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
    application: FastAPI, *, process: GatewayProcess | None = None, **kwargs: Any
) -> Generator[TestClient]:
    """Run an assembled Gateway with this test's current explicit entry inputs.

    Startup-delivery tests and local FastAPI stand-ins keep their own roots.
    Borrowed entries retain their configuration and writer; only the outer
    context closes resources it constructed.
    """
    with (
        gateway_test_process(application, process=process),
        TestClient(application, **kwargs) as client,
    ):
        yield client
