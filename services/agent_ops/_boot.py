"""ava-ops boot helpers — the leaf lookups `_main` needs before it can serve.

Split out of `services/agent_ops/daemon.py` to keep that module inside the file-size
budget. These four are leaves: the daemon calls them, they call nothing back, and
nothing else in this package imports them. `daemon` re-imports them into its own
namespace, so `daemon._register_boot` and friends stay the names the daemon's own
callers (and its tests) address.

- `_register_boot` — the boot-time `machine_units` re-announce.
- `_ops_bind_host` / `_ops_acceptance` — where this daemon listens, and what it
  requires on the way in.
- `_open_db_pool` — the shared pool every in-process op borrows a connection from.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from base.config import settings
from base.db import Database

_log = logging.getLogger("services.agent_ops.daemon")


def _register_boot() -> None:
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
    from base.cluster.machine import machine_role
    from base.cluster.machines import register_self, unit_dial_url

    try:
        url = unit_dial_url(machine_role())
        register_self(url=url)
    except Exception:
        _log.exception(
            "boot registration failed — this unit's machines row keeps its previous "
            "state (a stale stop marker will show it as stopped while it serves ops)"
        )
        return
    _log.info("registered this unit as up at %s", url)


def _ops_bind_host(acceptance: frozenset[str] | None) -> str:
    """The ops server's bind address — 0.0.0.0 when /ops is authenticated
    (`acceptance` is not None), loopback when it is not.

    Authenticated, the gateway dials /ops over the network and every POST must
    carry a machine API token (reachability is not trust; a single box's
    gateway self-dials its own /ops the same authenticated way over loopback).
    An open cluster has no credential to present and nothing remote to serve
    (its data plane and gateway are loopback-only too), so /ops binds 127.0.0.1
    then — an unauthenticated control surface must never be LAN-reachable.
    /healthz (watchdog) is served on the same port and stays reachable via
    localhost (unauthenticated — it leaks no secret).
    """
    if acceptance is not None:
        return "0.0.0.0"  # noqa: S104 — inbound ops port; authenticated, the gateway is the trust boundary
    return "127.0.0.1"


def _ops_acceptance() -> frozenset[str] | None:
    """SHA-256 digests of the bearers /ops accepts, or None for the open posture.

    The gateway dials /ops with its gateway-class machine API token
    (`ops.cluster_rpc`), and a runner-class process of the unit (the
    agent-host's crash recovery) dials it with its runner token, so /ops
    accepts exactly its write generation's two tokens. A remote unit takes the
    gateway token digest its installed capability carries plus its own runner
    token's (none when its cluster's API is open): it never holds the gateway
    token or the human secret. The gateway home takes the active generation's
    two tokens while the cluster secret is set (none while no generation is
    active: fail closed). A remote-managed plane keeps no write generations and
    accepts the human secret its gateway presents instead.
    """
    from base.cluster.authority.api import acceptance, token_digest
    from base.cluster.authority.unit import load_unit_capability
    from base.paths import ava_home

    home = ava_home().resolve()
    capability = load_unit_capability(home)
    if capability is not None:
        api = capability.api
        return None if api is None else frozenset({api.gateway, token_digest(api.token)})
    secret = settings.data_plane.cluster_secret
    if not secret:
        return None
    if settings.data_plane.is_remote:
        return frozenset({token_digest(secret)})
    return frozenset(acceptance(home).values())


def _open_db_pool() -> ConnectionPool[psycopg.Connection[TupleRow]]:
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
    without standing up the pidfile / health-server half of `_main`.
    """
    # check_connections=True: this is the longest-lived process on an agent-runner
    # and its borrows are rare, so pooled conns sit idle long enough for the
    # server side (PgBouncer lifetime) to close them — without a checkout check
    # the first dispatch of the day dies with psycopg.OperationalError
    # 'the connection is closed' (Task #1027). The check discards the dead conn
    # and hands out a fresh one.
    return Database.from_settings().pool(
        min_size=0,  # an idle daemon holds no client connection; borrows open lazily
        max_size=max(2, settings.services.ops_concurrency + 2),
        check_connections=True,
    )
