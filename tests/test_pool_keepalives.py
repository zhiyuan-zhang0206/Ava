"""Every long-lived Postgres pool carries `shared.db.PG_KEEPALIVE_KWARGS`, so a
connection that went dead while parked in the pool fails instead of stalling.

## The failure mode, and how it differs from PR #940's

#940 bounded the bare `psycopg.connect` sites: against a peer that black-holes
packets an unbounded connect never errors, so a daemon whose first act is a
connect reads as "hung" forever. It deliberately left the three `ConnectionPool`
sites alone, and correctly — `ConnectionPool(open=True)` does not block at
construction, and a borrow is capped by the pool's own acquire timeout, so a pool
cannot produce that unbounded boot hang.

What the pools lacked is the other half of the constant: TCP keepalives on
connections that live as long as the process. A laptop-grade runner sleeps or
changes networks and wakes holding dead TCP flows; the pool hands one out, and a
query already in flight on that half-dead socket has no application-level bound —
it waits out the OS TCP-retransmit timeout (minutes), while the acquire timeout,
already satisfied because the borrow itself succeeded, has nothing left to say
about it. Keepalives are what turn that into an error the pool's `check` can see.

## What is proven here, and what is not

`test_borrowed_connection_arms_the_kernel_keepalive` is the strongest of these: it
reads the keepalive socket options back off a **real borrowed connection's** file
descriptor, so it establishes that the kwargs reach `setsockopt` with our exact
values rather than merely sitting in a dict psycopg might ignore.

It is still not a behavioural test of recovery. Nothing here makes a socket go
half-dead — that needs ~60s of a genuinely unreachable peer holding an
established connection, which CI cannot stage. #940's `silent_peer_url` fixture
does not close the gap either: it can show a pool's *connect* is bounded, which is
the boot-wedge mode #940 already covers and the one explicitly not at issue for a
pool. So the per-site tests are **configuration** assertions — each pins that a
site hands psycopg the constant, which is the whole mechanism — and the
socket-option test pins that the constant does what it claims.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

import shared.log
from gateway.app import app
from services.agent_ops import daemon
from shared import db
from shared.config import settings

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The posture any pool in the tree is expected to carry: transaction-pooling-safe
# borrows plus the resilience constant. One dict so each site asserts against the
# same thing instead of restating keys.
_EXPECTED_KWARGS: dict[str, Any] = {"prepare_threshold": None, **db.PG_KEEPALIVE_KWARGS}


def _pool_kwargs(pool: ConnectionPool) -> dict[str, Any]:
    """The connection kwargs a pool will apply to each borrow.

    psycopg_pool's `kwargs=` is `dict | Callable[[], dict] | None` — the callable
    form lets a pool recompute per connection — so read it through this rather than
    assuming the dict shape.
    """
    kwargs = pool.kwargs
    if callable(kwargs):
        return kwargs()
    return kwargs or {}


def _assert_pool_posture(pool: ConnectionPool, site: str) -> None:
    """Assert a constructed pool's connection kwargs carry the full posture.

    Superset, not equality: a site may add its own kwargs (row factories,
    autocommit). `prepare_threshold` is asserted alongside the keepalives because
    the two are easy to trade for one another — the defect being fixed is exactly a
    site that wrote one and forgot the other.
    """
    kwargs = _pool_kwargs(pool)
    missing = {k: v for k, v in _EXPECTED_KWARGS.items() if kwargs.get(k) != v}
    assert not missing, (
        f"{site}'s pool is missing {missing} from its connection kwargs "
        f"(got {kwargs!r}) — build it with `shared.db.pool()`"
    )


# ─── the single definition ─────────────────────────────────────────────────────


def test_shared_db_pool_is_the_single_definition() -> None:
    """`shared.db.pool()` merges both halves, so a caller cannot take one without
    the other. Every site below inherits the posture by calling it."""
    pool = db.pool()
    try:
        _assert_pool_posture(pool, "shared.db.pool()")
    finally:
        pool.close()


def test_prepare_threshold_survives_the_merge() -> None:
    """`prepare_threshold=None` is what makes the pooled URL safe to point at
    PgBouncer: psycopg3 never prepares server-side statements, so a statement made
    on one backend never has to exist on the next one a transaction pooler hands
    out (cli/commands/_pgbouncer.py). Adding the keepalives must not displace it,
    and no keepalive key may collide with it."""
    pool = db.pool()
    try:
        assert _pool_kwargs(pool)["prepare_threshold"] is None
    finally:
        pool.close()
    assert "prepare_threshold" not in db.PG_KEEPALIVE_KWARGS


def test_borrowed_connection_arms_the_kernel_keepalive() -> None:
    """The kwargs reach the kernel: a borrowed connection's socket carries our
    exact idle / interval / count on the fd libpq opened.

    This is what a dict assertion cannot show — that psycopg forwards these into
    the conninfo and libpq translates them into `setsockopt`, on a connection the
    pool actually handed out. It does not show recovery behaviour (see the module
    docstring); it shows the probes are armed and would fire.

    Option names differ by platform: Linux exposes the idle time as
    `TCP_KEEPIDLE`, macOS/BSD as `TCP_KEEPALIVE`. Whichever name this interpreter
    has is asserted, so the test is not silently vacuous on either.
    """
    pool = db.pool(min_size=1, max_size=1)
    try:
        with pool.connection() as conn:
            sock = socket.socket(fileno=conn.fileno())
            try:
                # macOS reports the flag as a non-1 truthy int, so assert
                # truthiness rather than == 1.
                assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), (
                    "SO_KEEPALIVE is off on a borrowed pooled connection — libpq "
                    "never saw keepalives=1, so no probe will ever be sent"
                )
                idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(
                    socket, "TCP_KEEPALIVE", None
                )
                assert idle_opt is not None, "no TCP keepalive-idle option on this platform"
                expected = {
                    idle_opt: db.PG_KEEPALIVE_KWARGS["keepalives_idle"],
                    socket.TCP_KEEPINTVL: db.PG_KEEPALIVE_KWARGS["keepalives_interval"],
                    socket.TCP_KEEPCNT: db.PG_KEEPALIVE_KWARGS["keepalives_count"],
                }
                for opt, want in expected.items():
                    assert sock.getsockopt(socket.IPPROTO_TCP, opt) == want
            finally:
                # detach, not close: the fd belongs to the pooled connection.
                sock.detach()
    finally:
        pool.close()


# ─── the three sites #940 flagged and left ────────────────────────────────────


def test_log_sink_pipeline_drain_thread_stays_alive() -> None:
    """The event emitter's pipeline — the longest-lived resource in every ava
    process (opened at `init_*` via `shared.log._add_postgres_sink` ->
    `shared.telemetry`, never closed), drained by a background thread nobody
    watches. A stalled drain there is the least likely to be noticed — and
    since the LGTM cutover (task #1197 close-C) the pipeline no longer owns a
    Postgres pool, only the queue + drain thread + JSONL mirror.

    Asserts the emitter pipeline `_add_postgres_sink` opens, matching
    tests/gateway/test_log_sink.py; only the sink handler this call adds is
    removed, the shared pipeline is left as the rest of the suite expects it.
    """
    sink_id = shared.log._add_postgres_sink()
    try:
        from shared import telemetry

        assert telemetry._state["pipeline"] is not None
        pipe = telemetry._state["pipeline"]
        assert pipe._thread is not None and pipe._thread.is_alive()
    finally:
        shared.log.logger.remove(sink_id)


def test_log_sink_import_of_shared_db_stays_deferred() -> None:
    """`shared/log.py` must import `shared.db` inside the function, not at module
    scope — `shared/db.py` imports `shared.log` for `logger`, so a top-level
    import is a hard circular-import failure for any process that reaches
    `shared.db` first, which is the common case.

    A fresh interpreter is the only way to see it: this process imported both
    modules long ago, so an in-process import would pass either way. The
    subprocess imports `shared.db` first, the direction that breaks.
    """
    proc = subprocess.run(
        [sys.executable, "-c", "import shared.db; import shared.log"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        "importing shared.db in a fresh interpreter failed — shared/log.py has "
        f"probably acquired a module-level shared.db import:\n{proc.stderr}"
    )


def test_gateway_pool_carries_keepalives() -> None:
    """`gateway/app.py`'s request-serving pool, asserted on the object the real
    lifespan builds. Sizing (max_size=8) is the site's own business; the posture
    is not."""
    with TestClient(app):
        pool = app.state.db_pool
        _assert_pool_posture(pool, "gateway/app.py:lifespan")
        assert pool.max_size == 8


def test_ops_daemon_pool_carries_keepalives() -> None:
    """`services/agent_ops/daemon.py`'s dispatch pool — the longest-lived process
    on an agent-runner, i.e. the box most likely to have slept."""
    pool = daemon._open_db_pool()
    try:
        _assert_pool_posture(pool, "services/agent_ops/daemon.py:_open_db_pool")
        assert pool.max_size == max(2, settings.services.ops_concurrency + 2)
        # Checkout-time dead-conn eviction (Task #1027): the ops daemon's
        # borrows are rare, so idle conns get server-closed; the check makes the
        # first dispatch of the day survive instead of dying on
        # "the connection is closed".
        assert pool._check is not None
    finally:
        pool.close()
