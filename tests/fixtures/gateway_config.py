"""Explicit configuration ownership for an assembled in-process Gateway."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fixtures.configuration import snapshot_process_config

__all__ = ["gateway_test_client"]


@contextmanager
def gateway_test_client(application: FastAPI, **kwargs: Any) -> Generator[TestClient]:
    """Run the real Gateway lifespan with this test's current configuration inputs.

    Only assembled Gateway apps belong here. Startup-delivery tests and local
    FastAPI stand-ins keep their own explicit composition boundaries.
    """
    from gateway import app as gateway_app

    with (
        patch.object(gateway_app, "ConfigBoot", snapshot_process_config),
        TestClient(application, **kwargs) as client,
    ):
        yield client
