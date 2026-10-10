"""The connections a process holds for its `AvaContext`: built on first use, closed together.

An exec child (or any process that runs as an agent) depends on a SQL connection, a Redis client
and a gateway HTTP client, plus whatever its SDK layer adds (the MCP clients). None of it persists
and none of it is a graph-state fact, so it belongs to the context. A `ClientSet` is the context's
owner of those objects:

- nothing is built at construction: importing psycopg / redis / httpx waits for the first call that
  needs it, which keeps a child that never touches the database off that stack;
- a set built in a child resolves credentials from the child's own settings and environment; the
  description a host sends (`AvaContext.describe`) carries endpoints, never a secret;
  its composition root supplies lazy builders and the endpoint resolver;
- `close()` releases everything the set built. A client a test or an embedder supplied with
  `using_gateway` is not the set's to close. Constructor-specific factory builders
  remain configured after close, so subsequent use can build fresh clients.

A failure to build a client raises at the call that needed it; nothing here swallows one.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from base.log import logger

if TYPE_CHECKING:
    import httpx
    import psycopg

    from base.db import Database


type DatabaseHandle = Database
type DatabaseFactory = Callable[[], DatabaseHandle]


def _connection_dead(conn: object) -> bool:
    """True when psycopg has observed the connection's death (`closed` / `broken`)."""
    if getattr(conn, "closed", False):
        return True
    return bool(getattr(conn, "broken", False))


class LazyConnection:
    """A connection proxy: attribute access is forwarded to a connection built on first use.

    A dead connection is rebuilt on the next access: `closed` / `broken` are checked before every
    use, so a network outage that kills the socket cannot wedge the proxy for the life of the
    process (agent 2147: `pause_heartbeat` failed for hours after the network returned). The check
    happens on access, so a half-open connection whose death psycopg has not yet observed still
    fails the first call after the outage (bounded by the keepalive settings); that call marks it
    broken and the next access rebuilds.
    """

    def __init__(self, factory: Callable[[], object], name: str) -> None:
        self._factory = factory
        self._name = name
        self._conn: object | None = None
        self._lock = threading.Lock()

    def _get(self) -> object:
        with self._lock:
            conn = self._conn
            if conn is not None and not _connection_dead(conn):
                return conn
            if conn is not None:
                try:
                    conn.close()  # type: ignore[attr-defined]
                except Exception:
                    logger.opt(exception=True).warning(
                        "closing the dead {} connection failed; replacing it anyway", self._name
                    )
            self._conn = self._factory()
            return self._conn

    def close(self) -> None:
        """Close the connection if one was built; the proxy can build another afterwards."""
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()  # type: ignore[attr-defined]

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._get(), attr)

    def __repr__(self) -> str:
        if self._conn is None:
            return f"<lazy {self._name} (not connected)>"
        return repr(self._conn)


class ClientSet:
    """The lazily-built clients of one context (see the module docstring)."""

    def __init__(
        self,
        *,
        gateway_url: str | Callable[[], str] | None = None,
        database: DatabaseFactory | None = None,
        redis: Callable[[], object] | None = None,
        gateway: Callable[[str], httpx.Client] | None = None,
        factories: Mapping[Callable[..., object], Callable[[], object]] | None = None,
    ) -> None:
        """`database` hands over the cluster database handle, which is the composition root's to
        name (the exec child's is built from its settings, the host passes the one it holds); a set
        without one has no SQL connection to offer. The other builders and endpoint resolver are
        also supplied explicitly; this owner never discovers process configuration.
        `factories` maps an SDK factory key to its configured zero-argument builder,
        preserving `get(factory)` identity and the existing close/rebuild lifecycle."""
        self._gateway_url = gateway_url
        self._database = database
        self._redis_factory = redis
        self._gateway_factory = gateway
        self._lock = threading.RLock()
        self._sql: LazyConnection | None = None
        self._redis: LazyConnection | None = None
        self._gateway: httpx.Client | None = None
        self._gateway_provided = False
        self._factories = dict(factories) if factories is not None else {}
        self._made: dict[Callable[..., object], object] = {}

    # ── the three base clients ───────────────────────────────────────────

    @property
    def gateway_url(self) -> str:
        """The explicitly supplied endpoint, resolved once on first use."""
        with self._lock:
            if self._gateway_url is None:
                raise RuntimeError("this context has no gateway endpoint")
            if callable(self._gateway_url):
                self._gateway_url = self._gateway_url()
            return self._gateway_url

    @property
    def sql(self) -> LazyConnection:
        """The cluster database as one autocommit connection (every SDK op is a single statement).

        `base.db` owns the rest of the posture: a 5s connect cap so a black-holed database cannot
        freeze the agent's tool call, no server-side prepared statements behind pgbouncer, and a
        session scrub on every (re)connect."""
        with self._lock:
            if self._sql is None:
                self._sql = LazyConnection(self._connect_sql, "sql")
            return self._sql

    @property
    def redis(self) -> LazyConnection:
        """The cluster Redis, for short SDK get/set/publish calls (a 10s read bound sits under
        the TCP keepalive, which takes about a minute to declare a peer dead)."""
        with self._lock:
            if self._redis is None:
                self._redis = LazyConnection(self._connect_redis, "redis")
            return self._redis

    @property
    def gateway(self) -> httpx.Client:
        """The HTTP client for the gateway API, with this machine's bearer when it has one."""
        with self._lock:
            if self._gateway is None:
                self._gateway = self._build_gateway()
            return self._gateway

    @contextmanager
    def using_gateway(self, client: httpx.Client) -> Generator[httpx.Client]:
        """Route this set's gateway calls through `client` for the block (a test's in-process
        gateway, or any `httpx.Client`), then put back what was there. The set never closes a
        client it was given."""
        with self._lock:
            previous = (self._gateway, self._gateway_provided)
            self._gateway, self._gateway_provided = client, True
        try:
            yield client
        finally:
            with self._lock:
                self._gateway, self._gateway_provided = previous

    # ── the SDK layer's own clients ──────────────────────────────────────

    def get[T](self, factory: Callable[..., T]) -> T:
        """The client `factory` builds for this set, built on first use and closed with the set
        (its `close()`, when it has one). The factory itself is the key; an explicitly
        configured builder may supply its constructor inputs without changing that key."""
        with self._lock:
            if factory not in self._made:
                self._made[factory] = self._factories.get(factory, factory)()
            return self._made[factory]  # type: ignore[return-value]

    # ── release ──────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close every client this set built, newest first; one that fails to close is logged and
        does not keep the rest open."""
        with self._lock:
            owned: list[object] = list(reversed(self._made.values()))
            self._made.clear()
            if self._redis is not None:
                owned.append(self._redis)
                self._redis = None
            if self._sql is not None:
                owned.append(self._sql)
                self._sql = None
            if self._gateway is not None and not self._gateway_provided:
                owned.append(self._gateway)
            self._gateway = None
            self._gateway_provided = False
        for client in owned:
            close = getattr(client, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception:
                logger.opt(exception=True).warning(
                    "[clients] closing {} failed; the rest are still closed", type(client).__name__
                )

    # ── builders ─────────────────────────────────────────────────────────

    def _connect_sql(self) -> psycopg.Connection[Any]:
        if self._database is None:
            raise RuntimeError(
                "this context has no database: its ClientSet was built without a database handle"
            )
        return self._database().connect(autocommit=True)

    def _connect_redis(self) -> object:
        if self._redis_factory is None:
            raise RuntimeError("this context has no Redis client factory")
        return self._redis_factory()

    def _build_gateway(self) -> httpx.Client:
        if self._gateway_factory is None:
            raise RuntimeError("this context has no gateway client factory")
        return self._gateway_factory(self.gateway_url)
