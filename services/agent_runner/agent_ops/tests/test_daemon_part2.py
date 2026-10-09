"""Unit tests for services/agent_runner/agent_ops/daemon.py — the agent-runner ops server.

Covers:
- _dispatch routing for each op kind (kind, payload) -> (status, result)
- wire-error proxying (AvaAgentError -> failed result carrying reason)
- _ops_route: body parsing, {status, result} envelope, malformed-body 400,
  required semaphore binding
- concurrency cap (Semaphore) across concurrent /ops requests
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from base.db import Database
from ops.rpc_schemas import LaunchAgentRequest

_db = Database.from_settings


_REPO = Path(__file__).resolve().parents[4]


def _stub_pool() -> ConnectionPool:
    """A closed real pool for mocked arms; no connection is borrowed."""
    return ConnectionPool(open=False)


# ─── _dispatch routing ─────────────────────────────────────────────────────────


# ─── _ops_route (the POST /ops handler) ─────────────────────────────────────────


# ─── main top-level crash handling ─────────────────────────────────────────────


# ─── boot self-registration ────────────────────────────────────────────────────


# ─── idempotency-key dedup (Task #961) ────────────────────────────────────────


@pytest.fixture
def ops_pool() -> object:
    """A real ConnectionPool on the session test DB (the dedup path writes the
    shared `api_idempotency` table, method='ops' rows; `_stub_pool` is a
    non-DB stand-in and cannot serve it)."""
    from psycopg_pool import ConnectionPool

    from base.config import settings

    pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield pool
    finally:
        pool.close()


def _fake_spawn_factory(calls: dict[str, int]) -> object:
    """A launch_agent_op stand-in that counts executions and returns id 777."""
    from ops.rpc_schemas import SpawnedAgent

    async def _fake_spawn(body: LaunchAgentRequest, pool: ConnectionPool | None) -> SpawnedAgent:
        calls["n"] = calls.get("n", 0) + 1
        return SpawnedAgent(id=777)

    return _fake_spawn


# ─── _dispatch_idempotent retry on closed connection (Task #1059) ──────────────


async def _noop_sleep(_seconds: float) -> None:
    """Stand-in for daemon._sleep in retry tests — no real backoff wait."""


# ─── blocking ops run off the event loop ───────────────────────────────────────


def test_a_wedged_arm_does_not_hold_the_process_exit(tmp_path: Path) -> None:
    """The empirical one, and the only shape that can catch this.

    `shutdown(wait=False)` looks like it releases the daemon and does not:
    `concurrent.futures.thread._python_exit` — registered via
    `threading._register_atexit` — joins every worker still in `_threads_queues` with
    NO bound, and `wait=False` does not remove a running thread from that mapping;
    3.9+ also forces those workers non-daemon, so there is no way around it from the
    executor side. Measured before this fix: own pool + wedged worker +
    `shutdown(wait=False)` + `sys.exit(0)` was still alive minutes later.

    Nothing in-process can assert that: the failure IS the interpreter refusing to
    stop. So this drives the daemon's own `_op_thread_pool` / `_shutdown_op_pool` /
    shared `_hard_exit` alias in a real subprocess with a genuinely stuck arm, and asserts the
    process is gone. It fails by timing out against the pre-fix code.
    """
    ready = tmp_path / "wedged"
    script = textwrap.dedent(f"""
        import pathlib, sys, time
        sys.path.insert(0, {str(_REPO)!r})
        from services.agent_runner.agent_ops import daemon

        pool = daemon._op_thread_pool()
        pool.submit(lambda: (pathlib.Path({str(ready)!r}).write_text("1"), time.sleep(3600)))
        while not pathlib.Path({str(ready)!r}).exists():
            time.sleep(0.01)
        daemon._shutdown_op_pool(pool)
        daemon._hard_exit(0)
    """)
    started = time.monotonic()
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,  # the pre-fix code never returns; the timeout IS the failure
        check=False,
    )
    elapsed = time.monotonic() - started

    assert done.returncode == 0, done.stderr[-2000:]
    # Generous: the point is "seconds, not never", not a latency budget.
    assert elapsed < 20, f"exit took {elapsed:.1f}s with an arm still wedged"


def test_the_exit_code_survives_the_hard_exit(tmp_path: Path) -> None:
    """`_hard_exit` replaced a `raise` on the crash path, so the code a supervisor
    reads has to still distinguish a crash from a clean stop."""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(_REPO)!r})
        from services.agent_runner.agent_ops import daemon
        daemon._hard_exit(1)
    """)
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 1
