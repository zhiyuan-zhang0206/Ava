"""Verified native data-plane stop for an already drained maintenance hold.

No force escalation of the pooler or Redis, no snapshots, no remote management. A Postgres
fast shutdown that outlives its share of the budget is ended by an immediate shutdown
(docs/decisions/data/database/2026-10-02-pg-stop-escalates-to-immediate.md). The stop semantics match
what the preceding drain has already proven: the pooler gets SIGINT (safe
shutdown — it disconnects clients and waits only for in-flight server
transactions) and PostgreSQL native fast shutdown (disconnect idle sessions, roll
back stragglers, checkpoint), so neither waits on idle client connections a
paused runner may still hold (issue #2307). Redis saves its current in-memory
data before shutdown. An explicit save=False is reserved for callers that
already verified a final snapshot. PID disappearance is checked in addition to
command completion; an uncertain result always leaves the hold set.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
import time
from pathlib import Path
from typing import cast

import psutil
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from base.cluster import ownership
from base.cluster import postgres as owned_postgres
from base.cluster.authority import read_pooler_admin
from base.cluster.dataplane import pooler as pooler_files
from base.config import settings
from base.log import logger
from base.paths import ava_home
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.data_plane._pooler_stop import OwnedPooler
from cli.commands.lifecycle.service_stop import (
    PROCESS_CLEANUP_WAIT_S,
    PROCESS_KILL_WAIT_S,
    OwnedProcess,
    capture_tree,
    deadline_after,
    remaining,
    report_postgres_stop_escalation,
    wait_for_exit,
)


def capture_postgres() -> OwnedProcess | None:
    return ownership.postgres(instance._pg_data_dir())


def _capture_pooler() -> OwnedProcess | None:
    return ownership.pooler(pooler_files.ini_path(), pooler_files.pidfile_path())


def _require_no_unrecorded(captured: dict[str, OwnedProcess]) -> None:
    """A missing pidfile or refused port is not evidence of no local process."""
    directories = {
        "postgres": instance._pg_data_dir().resolve(),
        "redis-server": ownership.redis_data_dir().resolve(),
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


_CLIENTS_LISTED = 10


def _report_pooler_clients(identity: OwnedProcess, report: list[str] | None) -> None:
    """Say who is still connected to the pooler when it is about to stop (report only).

    The pooler stops by SIGINT, which disconnects its clients, so nothing here gates or
    changes the stop: the line (stderr, log, and `report` for the stop journal) names the
    remote clients a coordinated stop should already have stopped first. A console that
    cannot be read is reported as that, never as an empty list.
    """
    try:
        port = OwnedPooler.from_config(identity, pooler_files.ini_path()).port
        found = pooler_files.clients(port, read_pooler_admin(ava_home()).password)
    except Exception as exc:
        line = f"pooler clients could not be listed before the pooler stop: {exc!r}"
    else:
        if not found:
            logger.info("pooler has no clients at its stop")
            return
        shown = "; ".join(client.describe() for client in found[:_CLIENTS_LISTED])
        more = f"; +{len(found) - _CLIENTS_LISTED} more" if len(found) > _CLIENTS_LISTED else ""
        line = f"{len(found)} client(s) still connected when the pooler stops: {shown}{more}"
    print(f"  ! {line}", file=sys.stderr, flush=True)
    logger.warning(line)
    if report is not None:
        report.append(line)


async def _redis_command(client: Redis, deadline: float, *args: str) -> object:
    return cast(
        "object",
        await asyncio.wait_for(client.execute_command(*args), remaining(deadline)),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType] — redis command stubs
    )


def _postgres_fast_budget(deadline: float) -> float:
    """How long Postgres' fast shutdown may take: what is left of the stop's deadline minus
    the legs that follow it, so a stuck fast shutdown cannot spend the whole shared budget.

    The reserve is the immediate shutdown's wait, the SIGKILL wait, and the same cleanup
    wait for Redis' save. A stop whose remaining time is not larger than the reserve
    gives the fast shutdown all of it; the escalation legs then overrun the deadline by
    their own bounded waits, like the terminal closure's SIGKILL leg.
    """
    left = remaining(deadline)
    reserve = PROCESS_CLEANUP_WAIT_S + PROCESS_KILL_WAIT_S + PROCESS_CLEANUP_WAIT_S
    return left - reserve if left > reserve else left


async def _request_stop(
    name: str,
    identity: OwnedProcess,
    client: Redis,
    deadline: float,
    *,
    save: bool,
    notes: list[str] | None = None,
) -> None:
    if name == "postgres":
        # SIGINT requests PostgreSQL's fast, checkpointed shutdown; a fast shutdown stuck
        # past its budget (an archive command that never returns) is ended by SIGQUIT.
        escalation = owned_postgres.stop(
            instance._pg_data_dir(),
            expected=identity,
            timeout=_postgres_fast_budget(deadline),
            immediate_wait=PROCESS_CLEANUP_WAIT_S,
            kill_wait=PROCESS_KILL_WAIT_S,
        )
        if escalation is not None:
            report_postgres_stop_escalation(escalation, notes)
    elif name == "redis":
        # Do not use redis-py's shutdown helper: it accepts any connection
        # error as success. Expected EOF is accepted only if the exact PID
        # subsequently exits; a timeout or other command failure is loud.
        from redis.exceptions import ConnectionError as RedisConnectionError

        with contextlib.suppress(RedisConnectionError):
            await _redis_command(client, deadline, "SHUTDOWN", "SAVE" if save else "NOSAVE")
    else:
        custodian = OwnedPooler.from_config(identity, pooler_files.ini_path())
        if not custodian.stop(deadline=deadline):
            raise TimeoutError("PgBouncer stop incomplete; custody retained")


async def _stop(
    deadline: float,
    *,
    save: bool = True,
    notes: list[str] | None = None,
    clients: list[str] | None = None,
) -> list[str]:
    pg = capture_postgres()
    pgb = _capture_pooler()
    port = ownership.configured_redis_port()
    if port is None:
        raise RuntimeError("maintenance requires this home's explicit Redis endpoint")
    # The default user is the native instance's admin identity. The runtime URL
    # can carry a restricted ACL user's different password and is not admin auth.
    password = settings.data_plane.redis_admin_password or None
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
    custody = ownership.RedisConnectionCustody()
    try:
        redis_process = await custody.capture(
            client, port=port, data_dir=ownership.redis_data_dir(), deadline=deadline
        )
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
            if name == "pgbouncer":
                _report_pooler_clients(identity, clients)
            await _request_stop(name, identity, client, deadline, save=save, notes=notes)
            wait_for_exit(trees[name], deadline)
            stopped.append(name)
        remaining(deadline)
        if capture_postgres() is not None or _capture_pooler() is not None:
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


def stop(
    timeout: float,
    *,
    save: bool = True,
    notes: list[str] | None = None,
    clients: list[str] | None = None,
) -> list[str]:
    deadline = deadline_after(timeout)
    if sys.platform == "win32":
        raise RuntimeError("native maintenance data-plane stop requires POSIX")
    if settings.data_plane.is_remote:
        raise RuntimeError("maintenance cannot verify a remote-managed data-plane stop")
    return asyncio.run(_stop(deadline, save=save, notes=notes, clients=clients))
