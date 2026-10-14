"""ava-ops boot helpers — the leaf lookups `serve_ops_daemon` needs before it can serve.

This definition owner holds registration, singleton resources and invocation
executor/pool operations. It receives the root's config, runtime, authority and
database factory at their original read points. The daemon calls this definition owner directly; helpers do not
construct a process gate or discover a process image.

- `register_boot` — the boot-time `machine_units` re-announce.
- `ops_bind_host` / `ops_acceptance` — where this daemon listens, and what it
  requires on the way in.
- `open_db_pool` — the shared pool every in-process op borrows a connection from.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentity, daemon_acceptance
from base.cluster.port_preflight import serves_off_box
from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.db import Database
from base.events.live.bus import EventBus, EventBusConfig
from services import pidfile as pidfile_operations

__all__ = [
    "endpoint",
    "is_running",
    "op_thread_pool",
    "open_db_pool",
    "ops_acceptance",
    "ops_bind_host",
    "ops_handles",
    "pidfile",
    "register_boot",
    "remove_pidfile",
    "shutdown_op_pool",
    "write_pidfile",
]

_log = logging.getLogger("services.agent_runner.agent_ops.daemon")


def register_boot(
    *,
    database: Callable[[], Database],
    config: ConfigBoot,
    machine: MachineIdentity,
    ops_port: int,
    home: Path,
) -> None:
    """Re-announce this unit as up, now that the ops daemon is actually serving.

    The `machine_units` row is a liveness record, and until this call the only
    writer was `ava start`. A host can come back up without one completing — an
    OS-scheduled autostart, a watchdog respawn, the restart leg of a rollout — and
    the row then kept the `stopped_at` latch `ava stop` set, so the roster reported
    the host stopped while this daemon answered every op (the 2026-07-28
    a runner reading). `register_self()` clears that latch and stamps a fresh
    `up_since_at`, which is why it belongs in the process whose liveness the row
    stands for rather than only in the command that starts it.

    Called after the health server is up, so the stamp records "serving", not
    "attempting to serve".

    **Non-fatal by design**, the one place in this startup sequence that is.
    `assert_schema_current` exits because a schema-skewed daemon dispatches ops
    incorrectly; a failed registration refresh leaves dispatch entirely correct —
    the row is stale, not wrong-acting. Exiting here would instead hand the
    watchdog a respawn loop that takes the host dark for the gateway, which is the
    outage this function exists to prevent. The failure is logged at error so a
    permanently unregistered runner is visible rather than quietly absent.
    """
    from base.cluster.machines import register_self, unit_dial_url

    try:
        roles = machine.role()
        gateway = config.view.gateway.gateway_url.strip() or None
        url = unit_dial_url(
            roles,
            host=machine.host(),
            ops_port=ops_port,
            gateway_port=config.view.gateway.gateway_port,
            telemetry_otlp_port=config.view.observability.telemetry_otlp_port,
            gateway=gateway,
        )
        register_self(
            database(),
            url=url,
            name=machine.name(),
            roles=roles,
            description=machine.description(),
            home=str(home),
            gateway=gateway,
        )
    except Exception:
        _log.exception(
            "boot registration failed — this unit's machines row keeps its previous "
            "state (a stale stop marker will show it as stopped while it serves ops)"
        )
        return
    _log.info("registered this unit as up at %s", url)


def ops_bind_host(*, machine: MachineIdentity) -> str:
    """The ops server's bind address — 0.0.0.0 when this host declares a reachable
    address other machines dial (`port_preflight.serves_off_box`), loopback otherwise.

    /ops is always authenticated: every POST must carry a machine API token
    (reachability is not trust; a single box's gateway self-dials its own /ops the
    same authenticated way over loopback). A box that declares no private-network
    address (`AVA_MACHINE_HOST` unset: single box) has no remote gateway to serve,
    so /ops binds 127.0.0.1, like its data plane and gateway. /healthz (watchdog) is
    served on the same port and stays reachable via localhost (unauthenticated —
    it leaks no secret).
    """
    if serves_off_box(machine=machine):
        return "0.0.0.0"  # noqa: S104 — inbound ops port; authenticated, the gateway is the trust boundary
    return "127.0.0.1"


def ops_acceptance() -> frozenset[str]:
    """SHA-256 digests of the bearers /ops accepts: the
    daemon acceptance of `base.cluster.machine.daemon_acceptance` (the gateway dials /ops with its
    gateway-class machine API token, a runner-class process of the unit with its runner token)."""
    return daemon_acceptance()


def ops_handles(
    *, database: Callable[[], Database], authority: ConfigAuthority
) -> tuple[Database, EventBus]:
    """The cluster handles every in-process lifecycle op works with, as this root's authority names them."""
    return database(), EventBus(
        EventBusConfig(
            redis_url=authority.service_field_value("redis_url"),
            events_channel=authority.service_field_value("events_channel"),
        )
    )


def open_db_pool(
    *, database: Callable[[], Database], authority: ConfigAuthority
) -> ConnectionPool[psycopg.Connection[TupleRow]]:
    """The shared pool the in-process ops dispatch path borrows from.

    Sized for `ops_concurrency` plus a small margin — each lifecycle op holds a
    connection only briefly, so the cap bounds concurrent dispatch rather than
    request rate.

    Built by `Database.pool()` so the borrows carry `prepare_threshold=None` and
    `PG_KEEPALIVE_KWARGS` from the one place that defines them. The keepalives are
    not incidental for this daemon in particular: it is the longest-lived ava
    process on an agent-runner, which is typically a laptop-grade box that sleeps
    and changes networks, so its pooled connections are exactly the ones that come
    back on a dead TCP flow. Unbounded, a borrow taken on such a socket parks the
    op — and the gateway's synchronous `/ops` round-trip behind it — on the OS
    TCP-retransmit timeout.

    A separate function only so a test can assert what the daemon really builds
    without standing up the pidfile / health-server half of `serve_ops_daemon`.
    """
    # check_connections=True: this is the longest-lived process on an agent-runner
    # and its borrows are rare, so pooled conns sit idle long enough for the
    # server side (PgBouncer lifetime) to close them — without a checkout check
    # the first dispatch of the day dies with psycopg.OperationalError
    # 'the connection is closed' (Task #1027). The check discards the dead conn
    # and hands out a fresh one.
    return database().pool(
        min_size=0,  # an idle daemon holds no client connection; borrows open lazily
        max_size=max(2, authority.service_field_value("ops_concurrency") + 2),
        check_connections=True,
    )


def endpoint(config: ConfigBoot, *, runtime: Path) -> ServiceEndpoint:
    return ServiceEndpoints.from_boot(config, runtime=runtime).of("ops")


def pidfile(config: ConfigBoot, *, runtime: Path) -> Path:
    return endpoint(config, runtime=runtime).pidfile


def write_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    if not pidfile_operations.acquire_pidfile(
        pidfile(config, runtime=runtime), "services.agent_runner.agent_ops.daemon"
    ):
        _log.info(
            "[agent_ops] daemon already running (pidfile=%s), exiting",
            pidfile(config, runtime=runtime),
        )
        sys.exit(1)


def remove_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    pidfile_operations.remove_pidfile(pidfile(config, runtime=runtime))


def is_running(config: ConfigBoot, *, runtime: Path) -> bool:
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_operations.pidfile_holds_daemon(
        pidfile(config, runtime=runtime), "services.agent_runner.agent_ops.daemon"
    )


# The op arms' own thread pool, instead of asyncio's default executor.
#
# `asyncio.run` closes by awaiting `loop.shutdown_default_executor()`, which JOINS
# every default-executor thread. A wedged op arm — the 2026-08-12 shape — is a
# thread that never finishes, so SIGTERM unwinds normally, `serve_ops_daemon`'s finally runs
# (health server stopped, pidfile removed, pool closed), and then the interpreter
# sits in that join. Python 3.12 bounds it at `asyncio.constants.THREAD_JOIN_TIMEOUT`
# (300 s) and then gives up with `shutdown(wait=False)`, so this is a five-minute
# stall rather than a permanent one — but the supervisor's graceful window is 15 s,
# so in practice a wedged daemon stops being something that exits and becomes
# something that gets force-killed.
#
# Owning the pool buys nameable threads and a capacity bound — NOT a free exit.
# `shutdown(wait=False)` does not release the daemon: `_python_exit` (registered by
# `threading._register_atexit`) joins every worker still in `_threads_queues` with no
# bound, `wait=False` does not take a running thread out of that mapping, and 3.9+
# forces those workers non-daemon. Measured: own pool + wedged worker +
# `shutdown(wait=False)` + `sys.exit(0)` is still alive minutes later. `_hard_exit`
# is what actually gets the process out.
#
# `max_workers` tracks `ops_concurrency` so the semaphore stays the binding limit;
# `thread_name_prefix` makes a stuck thread findable in a dump, which the refusal
# runbook above tells an operator to look for.
def op_thread_pool(*, authority: ConfigAuthority) -> ThreadPoolExecutor:
    """Create one invocation's executor; workers still start on first submission."""
    return ThreadPoolExecutor(
        max_workers=max(1, authority.service_field_value("ops_concurrency")),
        thread_name_prefix="ava-ops-arm",
    )


def shutdown_op_pool(executor: ThreadPoolExecutor) -> None:
    """Stop accepting work without joining running arms.

    `wait=False` avoids joining here; the interpreter still joins workers at
    teardown, which is why the daemon also retains its existing hard exit.
    """
    executor.shutdown(wait=False)
