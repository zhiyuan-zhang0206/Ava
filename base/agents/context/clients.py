# meiva:link base/agents/context/docs/clients.meiva.okf.md
from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from base.log import logger

__all__ = ["ClientSet", "DatabaseFactory", "DatabaseHandle", "LazyConnection"]

if TYPE_CHECKING:
    import httpx
    import psycopg

    from base.db import Database


type DatabaseHandle = Database
type DatabaseFactory = Callable[[], DatabaseHandle]
# The gateway client by name, for SDK modules on the `import meiva` path that must not import httpx.
type GatewayHandle = httpx.Client


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
        pipeline_factory: Callable[[], Any] | None = None,
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
        self._factories = dict(factories) if factories is not None else {}
        self._made: dict[Callable[..., object], object] = {}
        self._pipeline_factory = pipeline_factory
        self._event_pipeline: Any = None

    # ── the three base clients ───────────────────────────────────────────

    def database(self) -> DatabaseHandle:
        """Build a handle using this context's supplied factory and retained gate.

        The factory owns configuration refresh and process admission. Returning
        another handle never changes that owner's minimum-check budget.
        """
        if self._database is None:
            raise RuntimeError("this context has no database handle factory")
        return self._database()

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

    def with_gateway(self, builder: Callable[[str], httpx.Client]) -> ClientSet:
        # meiva:link base/agents/context/docs/clients.meiva.okf.md
        return ClientSet(
            gateway_url=self._gateway_url,
            database=self._database,
            redis=self._redis_factory,
            gateway=builder,
            factories=self._factories,
        )

    # ── the SDK layer's own clients ──────────────────────────────────────

    def get[T](self, factory: Callable[..., T]) -> T:
        """The client `factory` builds for this set, built on first use and closed with the set
        (its `close()`, when it has one). The factory itself is the key; an explicitly
        configured builder may supply its constructor inputs without changing that key."""
        with self._lock:
            if factory not in self._made:
                self._made[factory] = self._factories.get(factory, factory)()
            return self._made[factory]  # type: ignore[return-value]

    def event_pipeline(self) -> Any:
        """Construct this owner's event writer only when its producer needs enqueue.

        A constructed writer is returned without taking the set lock. This is the
        producer behind the loguru event sink, which runs on whichever thread logs;
        that thread can be the one building the eager config chain (the timezone
        warning is logged from inside it) while the SDK thread holds the set lock in
        `gateway`, waiting for that same build to finish. Taking the lock here would
        make the two wait on each other until the config wait bound expired."""
        pipeline = self._event_pipeline
        if pipeline is not None:
            return pipeline
        with self._lock:
            if self._event_pipeline is None:
                if self._pipeline_factory is None:
                    raise RuntimeError("this context has no event pipeline factory")
                self._event_pipeline = self._pipeline_factory()
            return self._event_pipeline

    def sync_events(self, timeout: float = 5.0) -> Any:
        """Observe this owner's constructed writer without creating one during flush."""
        from base.telemetry.delivery.receipts import DrainPhase, DrainResult, DrainStatus

        with self._lock:
            pipeline = self._event_pipeline
        if pipeline is None:
            return DrainResult(DrainStatus.COMPLETED, DrainPhase.DRAIN)
        return pipeline.sync(timeout=timeout)

    def finalize_events(self) -> None:
        """Finalize this owner's constructed exporter without creating a cold pipeline."""
        with self._lock:
            pipeline = self._event_pipeline
        if pipeline is not None:
            pipeline.finalize_exports()

    def exports_deferred(self) -> bool | None:
        """Read an existing pipeline's export posture; a cold owner stays cold."""
        with self._lock:
            pipeline = self._event_pipeline
        return None if pipeline is None else bool(pipeline.exports_deferred())

    # ── release ──────────────────────────────────────────────────────────

    def close(self, *, pipeline_timeout: float = 5.0) -> None:
        """Close constructed resources; retain a writer with unfinished or failed stop.

        Pipeline failure never prevents closing other clients. The original worker
        error propagates after that cleanup and remains available to repeated stop.
        A cold owner does not call its pipeline factory during close.
        """
        with self._lock:
            owned: list[object] = list(reversed(self._made.values()))
            self._made.clear()
            if self._redis is not None:
                owned.append(self._redis)
                self._redis = None
            if self._sql is not None:
                owned.append(self._sql)
                self._sql = None
            if self._gateway is not None:
                owned.append(self._gateway)
            self._gateway = None
        primary: BaseException | None = None
        with self._lock:
            pipeline = self._event_pipeline
        if pipeline is not None:
            from base.telemetry.delivery.receipts import DrainStatus

            try:
                result = pipeline.stop(timeout=pipeline_timeout)
                if result.status is DrainStatus.COMPLETED:
                    with self._lock:
                        if self._event_pipeline is pipeline:
                            self._event_pipeline = None
            except BaseException as exc:
                primary = exc
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

        if primary is not None:
            raise primary

    # ── builders ─────────────────────────────────────────────────────────

    def _connect_sql(self) -> psycopg.Connection[Any]:
        return self.database().connect(autocommit=True)

    def _connect_redis(self) -> object:
        if self._redis_factory is None:
            raise RuntimeError("this context has no Redis client factory")
        return self._redis_factory()

    def _build_gateway(self) -> httpx.Client:
        if self._gateway_factory is None:
            raise RuntimeError("this context has no gateway client factory")
        return self._gateway_factory(self.gateway_url)
