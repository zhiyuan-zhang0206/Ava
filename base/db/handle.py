"""`Database`: the injected handle to the cluster Postgres.

Components take a `Database` (or a connection/pool it opened), never the URL or `DbConfig`:
the URL carries the login, and only the code that dials needs it. A composition root builds
the handle once (`Database.from_settings(gate=gate)`) and passes it down. Every handle
and cluster dial requires the entry's `ProcessDbGate`; independent pools and live-config
rebuilds retain that same loaded image, admission posture and minimum-read budget.

The dial decisions stay in `base/db/connections.py` (the `postgres-dial` single owner); this
class only binds one `DbConfig` to them.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.db import connections
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig, db_config_from_settings


class Database:
    """One cluster Postgres, dialed with one `DbConfig`."""

    def __init__(
        self,
        config: DbConfig,
        *,
        gate: ProcessDbGate,
        local_host: Callable[[], str] | None = None,
    ) -> None:
        self._config = config
        self._gate = gate
        self._local_host = local_host

    @classmethod
    def from_settings(
        cls,
        *,
        gate: ProcessDbGate,
        local_host: Callable[[], str] | None = None,
    ) -> Database:
        """The composition-root constructor: the config as the live settings hold it now."""
        return cls(db_config_from_settings(), gate=gate, local_host=local_host)

    def connect(
        self, *, autocommit: bool = False, direct: bool = False, unbounded: bool = False
    ) -> psycopg.Connection:
        """`base.db.connect()` on this handle's config."""
        return connections.connect(
            autocommit=autocommit,
            direct=direct,
            unbounded=unbounded,
            config=self._config,
            gate=self._gate,
            local_host=self._local_host,
        )

    def pool(
        self,
        *,
        min_size: int | None = None,
        max_size: int | None = None,
        direct: bool = False,
        timeout: float = connections.DEFAULT_POOL_TIMEOUT_S,
        check_connections: bool = False,
        autocommit: bool = False,
        row_factory: Any | None = None,
    ) -> ConnectionPool:
        """`base.db.pool()` on this handle's config."""
        return connections.pool(
            min_size=min_size,
            max_size=max_size,
            direct=direct,
            timeout=timeout,
            check_connections=check_connections,
            autocommit=autocommit,
            row_factory=row_factory,
            config=self._config,
            gate=self._gate,
            local_host=self._local_host,
        )

    def async_pool(
        self,
        pool_class: type[AsyncConnectionPool[psycopg.AsyncConnection]],
        *,
        min_size: int,
        max_size: int,
        timeout: float,
        **pool_kwargs: Any,
    ) -> AsyncConnectionPool[psycopg.AsyncConnection]:
        """`base.db.async_pool()` on this handle's config."""
        return connections.async_pool(
            pool_class,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            config=self._config,
            gate=self._gate,
            **pool_kwargs,
        )

    def direct_url(self) -> str:
        """The admin-plane URL of this handle's cluster (never through the pooler)."""
        return connections.direct_db_url(self._config, local_host=self._local_host)

    @contextmanager
    def write_transaction(self) -> Generator[psycopg.Connection, None, None]:
        """One explicitly writable transaction on a fresh connection of this handle."""
        with self.connect() as conn:
            conn.execute("SET TRANSACTION READ WRITE")
            yield conn
