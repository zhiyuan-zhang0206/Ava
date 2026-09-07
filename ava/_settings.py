"""Internal config — DB / Redis lazy proxy.

The agent accesses these directly via `ava.DB` / `ava.REDIS`; they don't
appear in `ava.help()` output. (The agent's own id lives at `ava.self.AGENT_ID`.)

DB and REDIS are lazy proxies — no connection on import, first method
call triggers connection. In container mode (no Postgres / Redis),
import ava doesn't blow up; only when ava DB ops are actually used does
it raise.

URL source: `shared.config.settings` single source of truth. Settings'
infra-pointing fields (db_url / redis_url) have no default; when env is
missing, Settings() instantiation throws ValidationError immediately,
not reaching here.
"""

import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from shared.config import settings

# DB_URL / REDIS_URL / GATEWAY_URL are exposed via module __getattr__ (PEP
# 562) so each access reads the current `settings.X` value rather than a
# load-time snapshot. This removes the conftest "must mutate settings before
# this module is imported" invariant: tests can flip settings.data_plane.db_url at any
# point and the next ava DB op picks it up.


def __getattr__(name: str) -> Any:
    if name == "DB_URL":
        return settings.data_plane.db_url
    if name == "REDIS_URL":
        return settings.data_plane.redis_url
    if name == "GATEWAY_URL":
        from shared.machine import gateway_api_base

        return gateway_api_base()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _conn_dead(conn: object) -> bool:
    """True when psycopg has observed the connection's death.

    `closed` / `broken` are psycopg Connection properties (both bool); the
    getattr fallbacks keep the proxy safe for any future factory that returns
    a non-psycopg object exposing neither attribute.
    """
    closed = getattr(conn, "closed", False)
    if closed:
        return True
    broken = getattr(conn, "broken", False)
    return bool(broken)


class _LazyConnection:
    """On-demand connection proxy — attribute access forwarded to the real connection.

    No connection triggered on import ava (container mode / tests-without-db
    don't need a real DB); first `ava.DB.cursor(...)` triggers the factory
    to build a connection.

    A dead connection is rebuilt on next access: `closed` / `broken` are
    checked before every use, so a network outage that kills the socket
    (psycopg marks the conn broken after a failed read/write) cannot wedge the
    proxy permanently — the next call gets a fresh connection instead of
    reusing the dead one. Without this, every ava.DB op in the process fails
    until restart (agent 2147: pause_heartbeat failed for hours after the
    network returned, while the agent main-loop pool — which checks every
    borrow — self-healed). The check happens on access, so a half-open
    connection whose death psycopg has not yet observed still fails the first
    call after the outage (bounded by PG_KEEPALIVE_KWARGS); that call is the
    one that marks it broken, and the next access rebuilds.
    """

    def __init__(self, factory: Callable[[], object], name: str) -> None:
        self._factory = factory
        self._name = name
        self._conn: object | None = None
        self._lock = threading.Lock()

    def _get(self) -> object:
        with self._lock:
            conn = self._conn
            if conn is not None and not _conn_dead(conn):
                return conn
            if conn is not None:
                with suppress(Exception):
                    conn.close()  # type: ignore[attr-defined]
            self._conn = self._factory()
            return self._conn

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._get(), attr)

    def __repr__(self) -> str:
        if self._conn is None:
            return f"<lazy {self._name} (not connected)>"
        return repr(self._conn)


def _connect_db() -> "psycopg.Connection":  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    import psycopg

    from shared.db import PG_STATEMENT_TIMEOUT_KWARGS

    if not settings.data_plane.db_url:
        raise RuntimeError("AVA_DB_URL not set — ava DB ops should not be called in container mode")
    # autocommit=True: every op at the SDK layer is a single-statement
    # INSERT/SELECT, no multi-statement transaction need. autocommit keeps
    # the connection from staying in a transaction state, avoiding:
    # 1. SELECT leaving 'idle in transaction' blocking ACCESS EXCLUSIVE
    #    locks on other connections
    # 2. SQL errors putting conn into INERROR state, with subsequent SDK
    #    calls all failing in a chain (especially hard to diagnose when
    #    caller forgets rollback)
    # Multi-statement transactions don't go through this conn — the caller
    # opens its own `psycopg.connect()` (shared/agents.py's spawn_agent /
    # resurrect_agent already follow this pattern).
    #
    # PG_STATEMENT_TIMEOUT_KWARGS (not `shared.db.connect()`, which dials the pooled URL
    # and would put every SDK op behind PgBouncer): the same connect-timeout +
    # keepalive posture as the rest of the cluster's DB access, from that one
    # constant. `ava.DB` is dialled from inside the agent's exec sandbox, so
    # without the 5s cap a database that black-holes packets freezes the agent's
    # tool call on the OS TCP-retransmit timeout with no application-level bound.
    #
    # The URL is the pooled front door, and pgbouncer never resets backend
    # session state between clients — scrub the session back to baseline on
    # every (re)connect so another client's session-level SET (2026-09-02 P0
    # read-only pollution) cannot break this connection's writes.
    conn = psycopg.connect(
        settings.data_plane.db_url, autocommit=True, **PG_STATEMENT_TIMEOUT_KWARGS
    )
    from shared.db import _restore_pooled_session

    _restore_pooled_session(conn)
    return conn


def _connect_redis() -> "redis.Redis":  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    import redis as _redis_lib

    from shared.redis_client import _RESILIENCE_KWARGS

    if not settings.data_plane.redis_url:
        raise RuntimeError(
            "AVA_REDIS_URL not set — ava Redis ops should not be called in container mode"
        )
    # The same weak-network posture as the shared clients: connect timeout,
    # TCP keepalive + health_check bound a half-dead socket in tens of
    # seconds instead of the OS TCP-retransmit timeout. Unlike the shared
    # clients, socket_timeout IS set here: ava.REDIS serves short SDK
    # get/set/publish from the exec sandbox (no pubsub long-read — the
    # inbound listener uses its own aredis client), so a 10s read bound is a
    # hard floor under keepalive (which takes ~60s to declare a peer dead).
    # Audit #689 G3 (Task #690 timeout ruling).
    return _redis_lib.Redis.from_url(
        settings.data_plane.redis_url,
        decode_responses=True,
        # Merge so the explicit 10s read bound overrides the shared None
        # instead of colliding with it as a duplicate keyword.
        **{**_RESILIENCE_KWARGS, "socket_timeout": 10.0},
    )


DB = _LazyConnection(_connect_db, "DB")
REDIS = _LazyConnection(_connect_redis, "REDIS")


# ── Plugin config hierarchical view ──
#
# `ava._settings.plugins.<plugin_name>` dynamically resolves the frozen Pydantic
# BaseModel instance for the current turn's agent (bound in by
# `register_plugin_config` + `bind_from_disk`, agent-scoped by
# `shared/plugin_config_view.py`).
#
# Design:
# - Private module (underscore prefix) → not in `ava.help()`, for plugin authors not the agent
# - lazy attribute access → no cache here, so restart / test monkeypatch changes
#   to the registry are immediately visible
# - lazy import shared.plugin_config_registry → avoids ava module load triggering agent
#   module import (test fixture / container mode can still import ava
#   without connecting agent)


class _PluginsView:
    """`ava._settings.plugins` — attribute access routes to the turn's config
    for that plugin (`shared/plugin_config_view.py`).

    Plugins not registered raise AttributeError listing known plugin names,
    so typos / "bind hasn't run yet" are immediately visible.
    """

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        from shared.plugin_config_registry import _PLUGIN_CONFIGS
        from shared.plugin_config_view import turn_plugin_config

        if name not in _PLUGIN_CONFIGS:
            known = sorted(_PLUGIN_CONFIGS.keys())
            raise AttributeError(
                f"ava._settings.plugins.{name} does not exist — plugin {name!r} has no "
                f"register_plugin_config, or framework `bind_from_disk` hasn't run yet. "
                f"Known plugins: {known or '<empty>'}"
            )
        # Agent-scoped read: the turn's config_overlay is layered over the disk
        # image, so in the hosted runner two agents sharing this process see
        # their own overrides. Unbound service code reads the registry instance.
        return turn_plugin_config(name)


plugins = _PluginsView()
