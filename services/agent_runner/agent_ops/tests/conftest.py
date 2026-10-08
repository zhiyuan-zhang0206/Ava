"""Invocation-owned op executors for daemon contracts."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from services.agent_runner.agent_ops import daemon


@pytest.fixture
def op_executor() -> Iterator[ThreadPoolExecutor]:
    executor = daemon._op_thread_pool()
    try:
        yield executor
    finally:
        executor.shutdown(wait=True)
