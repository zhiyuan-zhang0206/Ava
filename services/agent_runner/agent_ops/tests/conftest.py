"""Invocation-owned op executors for daemon contracts."""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.agent_ops import daemon
from tests.fixtures.model_catalog import add_bindings as add_bindings
from tests.fixtures.model_catalog import add_models as add_models
from tests.fixtures.model_catalog import model_catalog as model_catalog
from tests.fixtures.model_catalog import set_prices as set_prices
from tests.fixtures.unit.config_authority import config_authority as config_authority
from tests.fixtures.unit.homes import unit_home as unit_home
from tests.fixtures.unit.sdk import sdk_environment as sdk_environment
from tests.fixtures.unit.sdk import sdk_identity as sdk_identity
from tests.fixtures.unit.sdk import sdk_metering as sdk_metering


@pytest.fixture
def op_executor() -> Iterator[ThreadPoolExecutor]:
    executor = daemon._op_thread_pool()
    try:
        yield executor
    finally:
        executor.shutdown(wait=True)


@pytest.fixture
def ops_database(database: Database) -> Callable[[], Database]:
    """The isolated test root's handle supplied to each actual dispatch consumer."""
    return lambda: database


@pytest.fixture
def ops_image(tmp_path: Path) -> LoadedCommit:
    """An explicit unknown image for routing cases that do not claim a commit."""
    return LoadedCommit(source_root=tmp_path, sha=None)
