"""Verified native data-plane stop for an already drained maintenance hold.

No force escalation, snapshots, or remote management. Redis NOSAVE requires the
operator's separately verified final snapshot. PID disappearance is checked in
addition to command completion; an uncertain result always leaves the hold set.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from cli.commands import _cluster_instance as instance
from cli.commands import _pgbouncer as pooler
from cli.commands._maintenance_stop import (
    OwnedProcess,
    capture_tree,
    deadline_after,
    remaining,
    wait_for_exit,
)
from shared.config import settings


def _pidfile(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().splitlines()[0])
    except (ValueError, IndexError):
        raise RuntimeError(f"cannot verify data-plane PID file: {path.name}") from None
    if pid <= 0:
        raise RuntimeError(f"invalid data-plane PID file: {path.name}")
    return pid


def _process(pid: int) -> OwnedProcess | None:
    try:
        identity = OwnedProcess.capture(psutil.Process(pid))
        return identity if identity.live() else None
    except psutil.NoSuchProcess:
        return None


def _capture_postgres() -> OwnedProcess | None:
    data = instance._pg_data_dir().resolve()
    pidfile = data / "postmaster.pid"
    pid = _pidfile(pidfile)
    if pid is None:
        return None
    identity = _process(pid)
    if identity is None:
        return None
    lines = pidfile.read_text().splitlines()
    process = psutil.Process(pid)
    argv = process.cmdline()
    try:
        valid = (
            Path(lines[1]).resolve() == data
            and abs(float(lines[2]) - identity.birth) < 2
            and process.name() in {"postgres", "postmaster"}
            and Path(argv[argv.index("-D") + 1]).resolve() == data
        )
    except (ValueError, IndexError):
        valid = False
    if not valid or not identity.live():
        raise RuntimeError("cannot verify this home's PostgreSQL process")
    return identity


def _capture_pooler() -> OwnedProcess | None:
    pid = _pidfile(pooler._pidfile_path())
    if pid is None:
        return None
    identity = _process(pid)
    if identity is None:
        return None
    if not pooler._pid_is_our_pooler(pid) or not identity.live():
        raise RuntimeError("cannot verify this home's PgBouncer process")
    return identity


def _require_no_unrecorded(captured: dict[str, OwnedProcess]) -> None:
    """A missing pidfile or refused port is not evidence of no local process."""
    directories = {
        "postgres": instance._pg_data_dir().resolve(),
        "redis-server": instance._redis_data_dir().resolve(),
    }
    for process in psutil.process_iter(["pid", "name"]):
        name = process.info["name"]
        if name not in {"postgres", "postmaster", "redis-server", "pgbouncer"}:
            continue
        try:
            if process.uids().real != os.getuid():
                continue
            if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                continue
            if name == "pgbouncer":
                belongs = pooler._pid_is_our_pooler(process.pid)
                key = "pgbouncer"
            else:
                key = "redis" if name == "redis-server" else "postgres"
                directory = directories["redis-server" if key == "redis" else "postgres"]
                belongs = Path(process.cwd()).resolve() == directory
            if not belongs:
                continue
            expected = captured.get(key)
            if expected is not None and expected.live():
                # PostgreSQL worker processes share the postmaster's data dir.
                family = {
                    expected.pid,
                    *(child.pid for child in psutil.Process(expected.pid).children(recursive=True)),
                }
                if process.pid in family:
                    continue
            raise RuntimeError(f"unrecorded or replacement {key} process prevents maintenance stop")
        except psutil.NoSuchProcess:
            continue


def _signal(identity: OwnedProcess) -> None:
    # A psutil.Process object retains its own PID-reuse guard during delivery.
    process = psutil.Process(identity.pid)
    if not identity.live():
        raise RuntimeError("data-plane identity changed before stop")
    process.send_signal(signal.SIGTERM)


async def _redis_command(client: Redis, deadline: float, *args: str) -> object:
    return await asyncio.wait_for(client.execute_command(*args), remaining(deadline))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType] — redis command stubs


async def _capture_redis(client: Redis, deadline: float) -> OwnedProcess:
    info = await asyncio.wait_for(client.info("server"), remaining(deadline))  # pyright: ignore[reportUnknownMemberType] — redis command stubs
    config = await asyncio.wait_for(client.config_get("dir"), remaining(deadline))  # pyright: ignore[reportUnknownMemberType] — redis command stubs
    directory = config["dir"]
    if not isinstance(directory, str):
        raise TypeError("Redis did not expose its data directory")
    identity = _process(int(info["process_id"]))
    if (
        identity is None
        or Path(directory).resolve() != instance._redis_data_dir().resolve()
        or psutil.Process(identity.pid).name() != "redis-server"
    ):
        raise RuntimeError("cannot verify this home's Redis process")
    return identity


async def _stop(deadline: float) -> list[str]:
    pg = _capture_postgres()
    pgb = _capture_pooler()
    endpoint = instance._redis_endpoint()
    if endpoint is None:
        raise RuntimeError("maintenance requires this home's explicit Redis endpoint")
    port, _runtime_password = endpoint
    # The default user is the native instance's admin identity. The runtime URL
    # can carry a restricted ACL user's different password and is not admin auth.
    password = (
        settings.data_plane.redis_admin_password or settings.data_plane.cluster_secret or None
    )
    client = Redis(
        host="127.0.0.1",
        port=port,
        password=password,
        decode_responses=True,
        single_connection_client=True,
        socket_connect_timeout=remaining(deadline),
        socket_timeout=remaining(deadline),
        retry=Retry(NoBackoff(), 0),
    )
    try:
        # Only a refused local TCP connection proves this configured endpoint is
        # absent. Authentication failures, resets, and timeouts are not absence.
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=remaining(deadline)):
                pass
        except ConnectionRefusedError:
            redis_process = None
        else:
            redis_process = await _capture_redis(client, deadline)
        captured = {
            name: process
            for name, process in (("pgbouncer", pgb), ("postgres", pg), ("redis", redis_process))
            if process is not None
        }
        _require_no_unrecorded(captured)
        trees = {name: capture_tree(process) for name, process in captured.items()}
        # Every service is identified before the first stop; no foreign Redis
        # endpoint can be discovered only after the local database was stopped.
        stopped: list[str] = []
        for name, identity in captured.items():
            remaining(deadline)
            if not identity.live():
                raise RuntimeError(f"{name} identity changed before stop")
            if name == "postgres":
                result = subprocess.run(
                    [
                        instance._pg_bin("pg_ctl"),
                        "-D",
                        str(instance._pg_data_dir()),
                        "-m",
                        "smart",
                        "-W",
                        "stop",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=remaining(deadline),
                    check=False,
                )
                if result.returncode:
                    raise RuntimeError(f"PostgreSQL smart stop failed (exit {result.returncode})")
            elif name == "redis":
                # Do not use redis-py's shutdown helper: it accepts any connection
                # error as success. Expected EOF is accepted only if the exact PID
                # subsequently exits; a timeout or other command failure is loud.
                from redis.exceptions import ConnectionError as RedisConnectionError

                with contextlib.suppress(RedisConnectionError):
                    await _redis_command(client, deadline, "SHUTDOWN", "NOSAVE")
            else:
                _signal(identity)
            wait_for_exit(trees[name], deadline)
            stopped.append(name)
        remaining(deadline)
        if _capture_postgres() is not None or _capture_pooler() is not None:
            raise RuntimeError("data-plane process appeared during held stop")
        _require_no_unrecorded({})
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=remaining(deadline)):
                raise RuntimeError("Redis endpoint still accepts connections after held stop")
        except ConnectionRefusedError:
            return stopped
    except RedisError as exc:
        raise RuntimeError(f"Redis maintenance stop failed ({type(exc).__name__})") from None
    finally:
        # Cleanup must not wait another full socket timeout after the shared
        # deadline. Close this private transport without waiting for peer EOF.
        if client.connection is not None:
            await client.connection.disconnect(nowait=True)
        await asyncio.wait_for(client.aclose(), max(0.001, deadline - time.monotonic()))


def stop(timeout: float) -> list[str]:
    deadline = deadline_after(timeout)
    if sys.platform == "win32":
        raise RuntimeError("native maintenance data-plane stop requires POSIX")
    if settings.data_plane.is_remote:
        raise RuntimeError("maintenance cannot verify a remote-managed data-plane stop")
    return asyncio.run(_stop(deadline))
