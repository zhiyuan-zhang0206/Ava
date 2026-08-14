"""`ava/_batch.py` unit tests — the one concurrent batch executor.

The public SDK entry points (search / fetch / understand) each have their own
integration tests for the batch behaviour (order, concurrency cap, error
messages); these are the direct unit tests of the shared executor and its
`max_concurrent` contract.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from ava._batch import run_batch, validate_max_concurrent

# ─── run_batch ────────────────────────────────────────────────────────────


def test_run_batch_preserves_input_order() -> None:
    """Results come back in input order regardless of which worker finishes
    first."""

    def worker(n: int) -> int:
        time.sleep((10 - n) / 100)  # later items finish first
        return n * 2

    assert asyncio.run(run_batch([1, 2, 3], worker, None)) == [2, 4, 6]


def test_run_batch_empty_list() -> None:
    assert asyncio.run(run_batch([], lambda x: x, None)) == []  # pyright: ignore[reportUnknownArgumentType]


def test_run_batch_caps_inflight_with_semaphore() -> None:
    """max_concurrent=N keeps at most N workers in flight — the observed peak
    never exceeds N (the semaphore is the shared copy of the three old
    `_*_all` implementations)."""
    lock = threading.Lock()
    inflight = 0
    peak = 0

    def worker(_: int) -> int:
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.05)
        with lock:
            inflight -= 1
        return 1

    asyncio.run(run_batch(list(range(8)), worker, 3))
    assert peak <= 3
    assert peak >= 2  # actually ran concurrently, not serialized


def test_run_batch_propagates_first_error() -> None:
    """A failing worker propagates its exception out of the batch — callers
    see the failure, not a partial result list."""

    def worker(n: int) -> int:
        if n == 2:
            raise LookupError("boom")
        return n

    with pytest.raises(LookupError, match="boom"):
        asyncio.run(run_batch([1, 2, 3], worker, None))


# ─── validate_max_concurrent ──────────────────────────────────────────────


def test_validate_max_concurrent_accepts_valid_values() -> None:
    validate_max_concurrent(None, example="x")
    validate_max_concurrent(1, example="x")
    validate_max_concurrent(4, example="x")


def test_validate_max_concurrent_rejects_bad_values() -> None:
    """The shared knob keeps the per-API messages: bad values fail fast with
    the fragments the entry-point tests assert ("at least 1" / "int or None")."""
    with pytest.raises(ValueError, match="at least 1"):
        validate_max_concurrent(0, example="x")
    with pytest.raises(ValueError, match="at least 1"):
        validate_max_concurrent(-3, example="x")
    with pytest.raises(TypeError, match="int or None"):
        validate_max_concurrent(2.5, example="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="int or None"):
        validate_max_concurrent("4", example="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="int or None"):
        validate_max_concurrent(True, example="x")  # bool is an int subclass


def test_validate_max_concurrent_message_names_the_calling_api() -> None:
    with pytest.raises(TypeError, match=r"ava\.web\.fetch\(targets, max_concurrent=4\)"):
        validate_max_concurrent(2.5, example="ava.web.fetch(targets, max_concurrent=4)")  # type: ignore[arg-type]
