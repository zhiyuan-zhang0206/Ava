"""Native storage ownership, shared by initialization and maintenance.

A successful protocol probe is readiness, not authority. Match the process's
home directory, captured native birth, and every listener before modifying it.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Mapping
from ipaddress import ip_address
from pathlib import Path
from typing import Any

import psutil
import psycopg
from redis.asyncio import Redis

from shared.native_process.ownership import OwnedProcess, capture_tree
from shared.port_preflight import strict_listeners_on


class RedisConnectionCustody:
    """Bind Redis observations to one transport; retain through any later effects.

    Redis keeps a weak reference to the reconnect callback. The caller must
    retain this custody object until its dedicated client is closed.
    """

    def refuse_reconnect(self, _connection: object) -> None:
        raise RuntimeError("Redis connection changed during ownership verification")

    async def capture(
        self, client: Redis, *, port: int, data_dir: Path, deadline: float
    ) -> OwnedProcess | None:
        """Read native ownership without changing Redis configuration or ACLs.

        A refused loopback connection proves absence. Authentication, timeout,
        native ownership and reconnect failures remain explicit errors.
        """
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=_remaining(deadline)):
                pass
        except ConnectionRefusedError:
            return None
        await asyncio.wait_for(client.initialize(), _remaining(deadline))
        if client.connection is None:
            raise RuntimeError("Redis ownership requires a dedicated connection")
        client.connection.register_connect_callback(self.refuse_reconnect)  # pyright: ignore[reportUnknownMemberType] — redis callback stubs
        info = await asyncio.wait_for(client.info("server"), _remaining(deadline))  # pyright: ignore[reportUnknownMemberType] — redis command stubs
        config = await asyncio.wait_for(client.config_get("dir"), _remaining(deadline))  # pyright: ignore[reportUnknownMemberType] — redis command stubs
        identity = redis_server(info, config, data_dir)
        require_listener(identity, port)
        return identity


def _remaining(deadline: float) -> float:
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError("Redis ownership capture deadline expired")
    return budget


def pidfile(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().splitlines()[0])
    except (ValueError, IndexError):
        raise RuntimeError(f"cannot verify data-plane PID file: {path.name}") from None
    if pid <= 0:
        raise RuntimeError(f"invalid data-plane PID file: {path.name}")
    return pid


def process_identity(pid: int) -> OwnedProcess | None:
    try:
        identity = OwnedProcess.capture(psutil.Process(pid))
        return identity if identity.live() else None
    except psutil.NoSuchProcess:
        return None


def postgres(data: Path) -> OwnedProcess | None:
    """Observe only a postmaster captured by the home's durable launch boundary."""
    from shared.cluster.postgres import observe

    return observe(data)


def redis_server(
    info: Mapping[str, object], config: Mapping[Any, object], data: Path
) -> OwnedProcess:
    directory, pid = config["dir"], info["process_id"]
    if not isinstance(directory, str) or not isinstance(pid, int):
        raise TypeError("Redis did not expose its data directory and native PID")
    identity = process_identity(pid)
    if (
        identity is None
        or Path(directory).resolve() != data.resolve()
        or psutil.Process(pid).name() != "redis-server"
        or not identity.live()
    ):
        raise RuntimeError("cannot verify this home's Redis process")
    return identity


def pooler(config: Path, record: Path) -> OwnedProcess | None:
    pid = pidfile(record)
    if pid is None or (identity := process_identity(pid)) is None:
        return None
    process = psutil.Process(pid)
    argv = process.cmdline()
    try:
        ini = Path(argv[argv.index("-d") + 1])
        if not ini.is_absolute():
            ini = Path(process.cwd()) / ini
        valid = process.name() == "pgbouncer" and ini.resolve() == config.resolve()
    except (ValueError, IndexError):
        valid = False
    if not valid or not identity.live():
        raise RuntimeError("cannot verify this home's PgBouncer process")
    return identity


def require_listener(
    identity: OwnedProcess | None, port: int, *, required: bool = True
) -> frozenset[int]:
    """Return the observed listeners only after validating their native owner."""
    listeners = frozenset(strict_listeners_on(port))
    if identity is None:
        if listeners or required:
            raise RuntimeError(f"no owned data-plane process for listener :{port}")
        return listeners
    if required and not listeners:
        raise RuntimeError(f"recorded data-plane process has no listener :{port}; custody retained")
    owned = {process.pid for process in capture_tree(identity)}
    if not identity.live() or not listeners <= owned:
        raise RuntimeError(f"data-plane listener :{port} does not belong to the recorded process")
    return listeners


def require_postgres(data: Path, port: int, *, required: bool = True) -> OwnedProcess | None:
    identity = postgres(data)
    require_listener(identity, port, required=required)
    return identity


def require_postgres_connection(conn: psycopg.Connection[Any], data: Path) -> None:
    """Bind the connected backend to this home's postmaster before any DDL.

    The protocol's backend PID must be a live native child of the validated
    postmaster in its actual data directory. A Unix endpoint must be canonical;
    TCP must be loopback and all listeners must belong to the same postmaster.
    """
    from shared.pg_admin import pg_socket_dir

    data = data.resolve()
    owner = postgres(data)
    if owner is None:
        raise RuntimeError("owned PostgreSQL postmaster is absent")
    if conn.info.host.startswith("/"):
        if Path(conn.info.host).resolve() != pg_socket_dir(home=data.parent).resolve():
            raise RuntimeError("PostgreSQL admin connection used a foreign Unix socket")
    else:
        if not ip_address(conn.info.hostaddr).is_loopback:
            raise RuntimeError("owned PostgreSQL connection is not local")
        require_listener(owner, conn.info.port)
    backend = psutil.Process(conn.info.backend_pid)
    identity = OwnedProcess.capture(backend)
    if (
        backend.ppid() != owner.pid
        or Path(backend.cwd()).resolve() != data
        or not identity.live()
        or not owner.live()
    ):
        raise RuntimeError("connected PostgreSQL backend is not owned by this home")
