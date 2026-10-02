"""Capture loguru output in tests (pytest's `caplog` only sees stdlib logging)."""

from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture
def loguru_records() -> Iterator[list[dict[str, Any]]]:
    """Capture loguru output for tests. pytest's built-in caplog captures stdlib logging,
    loguru doesn't go through stdlib, requires separate bridging.

    Usage:
        def test_foo(loguru_records):
            do_something_that_logs()
            assert any("expected" in r["message"] for r in loguru_records)
    """
    from loguru import logger as _logger

    captured: list[dict[str, Any]] = []
    handler_id = _logger.add(lambda msg: captured.append(dict(msg.record)), level="DEBUG")
    try:
        yield captured
    finally:
        _logger.remove(handler_id)
