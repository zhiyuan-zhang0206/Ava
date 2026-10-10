"""Invocation-owned op executors for daemon contracts."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from services.agent_runner.agent_ops import daemon
from tests.fixtures.model_catalog import add_bindings as add_bindings
from tests.fixtures.model_catalog import add_models as add_models
from tests.fixtures.model_catalog import model_catalog as model_catalog
from tests.fixtures.model_catalog import set_prices as set_prices
from tests.fixtures.unit.config_authority import config_authority as config_authority
from tests.fixtures.unit.homes import unit_home as unit_home


@pytest.fixture
def op_executor() -> Iterator[ThreadPoolExecutor]:
    executor = daemon._op_thread_pool()
    try:
        yield executor
    finally:
        executor.shutdown(wait=True)
