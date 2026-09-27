"""Verified data-plane effects of a held maintenance operation.

Two groups. The native stop of an already drained hold, and a release
operation's write-generation effects (the old generation's fence, the next
generation's admission, and their read-only checks); the release journal
records intent and receipts around those, the home's ledger is the authority.

Native stop: no force escalation, snapshots, or remote management. The stop semantics match
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
import hashlib
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import psutil
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from cli.commands import cluster_instance as instance
from cli.commands import pgbouncer as pooler
from cli.commands._pooler_stop import OwnedPooler
from cli.commands.maintenance_stop import (
    OwnedProcess,
    capture_tree,
    deadline_after,
    remaining,
    wait_for_exit,
)
from cli.commands.pgbouncer import PoolerStop
from cli.release_transition.pitr.evidence import DataOwner, DataStop
from shared.cluster import ownership
from shared.cluster import postgres as owned_postgres
from shared.cluster.authority import (
    AuthorityRefusedError,
    ClosureEvidence,
    Generation,
    OperationAuthority,
)
from shared.cluster.registry import ClusterRecord
from shared.config import settings
from shared.process_evidence import ExpectedProcess
from shared.verified_file import regular_bytes


def capture_postgres() -> OwnedProcess | None:
    return ownership.postgres(instance._pg_data_dir())


def _capture_pooler() -> OwnedProcess | None:
    return ownership.pooler(pooler.ini_path(), pooler.pidfile_path())


def _require_no_unrecorded(captured: dict[str, OwnedProcess]) -> None:
    """A missing pidfile or refused port is not evidence of no local process."""
    directories = {
        "postgres": instance._pg_data_dir().resolve(),
        "redis-server": instance.redis_data_dir().resolve(),
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


async def _redis_command(client: Redis, deadline: float, *args: str) -> object:
    return cast(
        "object",
        await asyncio.wait_for(client.execute_command(*args), remaining(deadline)),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType] — redis command stubs
    )


async def _request_stop(
    name: str, identity: OwnedProcess, client: Redis, deadline: float, *, save: bool
) -> None:
    if name == "postgres":
        # SIGINT requests PostgreSQL's fast, checkpointed shutdown. The same
        # durable receipt gates ordinary stop and retained PITR custody.
        owned_postgres.stop(instance._pg_data_dir(), expected=identity, timeout=remaining(deadline))
    elif name == "redis":
        # Do not use redis-py's shutdown helper: it accepts any connection
        # error as success. Expected EOF is accepted only if the exact PID
        # subsequently exits; a timeout or other command failure is loud.
        from redis.exceptions import ConnectionError as RedisConnectionError

        with contextlib.suppress(RedisConnectionError):
            await _redis_command(client, deadline, "SHUTDOWN", "SAVE" if save else "NOSAVE")
    else:
        custodian = OwnedPooler.from_config(identity, pooler.ini_path())
        if not custodian.stop(deadline=deadline):
            raise TimeoutError("PgBouncer stop incomplete; custody retained")


async def _stop(deadline: float, *, save: bool = True) -> list[str]:
    pg = capture_postgres()
    pgb = _capture_pooler()
    port = instance.configured_redis_port()
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
            client, port=port, data_dir=instance.redis_data_dir(), deadline=deadline
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
            await _request_stop(name, identity, client, deadline, save=save)
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


def stop(timeout: float, *, save: bool = True) -> list[str]:
    deadline = deadline_after(timeout)
    if sys.platform == "win32":
        raise RuntimeError("native maintenance data-plane stop requires POSIX")
    if settings.data_plane.is_remote:
        raise RuntimeError("maintenance cannot verify a remote-managed data-plane stop")
    return asyncio.run(_stop(deadline, save=save))


def _receipt_owner(
    identity: OwnedProcess, directory: Path, port: int, *, config: Path | None = None
) -> DataOwner:
    def evidence(process: OwnedProcess) -> ExpectedProcess:
        return ExpectedProcess(
            pid=process.pid, create_time=process.birth, starttime=process.starttime
        )

    ownership.require_listener(identity, port)
    tree = capture_tree(identity)
    if not identity.live():
        raise RuntimeError("data owner exited while capturing durable custody")
    return DataOwner(
        process=evidence(identity),
        tree=tuple(evidence(p) for p in sorted(tree, key=lambda p: p.pid)),
        directory=str(directory.resolve()),
        port=port,
        config_digest=None if config is None else hashlib.sha256(regular_bytes(config)).hexdigest(),
    )


def _receipt_client(port: int) -> Redis:
    return Redis(
        host="127.0.0.1",
        port=port,
        password=settings.data_plane.redis_admin_password or None,
        decode_responses=True,
        single_connection_client=True,
        socket_connect_timeout=5,
        socket_timeout=5,
        retry=Retry(NoBackoff(), 0),
    )


async def _close_receipt_client(client: Redis) -> None:
    if client.connection is not None:
        await client.connection.disconnect(nowait=True)
    await asyncio.wait_for(client.aclose(), 1)


def _receipt_resources() -> tuple[int, int, int]:
    from shared.cluster import (
        get_record,
        record_pgbouncer_port,
        record_postgres_port,
        record_redis_port,
    )
    from shared.paths import ava_home

    if sys.platform == "win32" or settings.data_plane.is_remote:
        raise RuntimeError("durable data stop requires an owned POSIX data plane")
    record = get_record(ava_home())
    if record is None:
        raise RuntimeError("durable data stop requires the home's retained registry")
    return record_postgres_port(record), record_redis_port(record), record_pgbouncer_port(record)


async def _capture_custody(deadline: float) -> DataStop:
    pg_port, redis_port, pooler_port = _receipt_resources()
    pg, pgb = capture_postgres(), _capture_pooler()
    client = _receipt_client(redis_port)
    custody = ownership.RedisConnectionCustody()
    try:
        redis = await custody.capture(
            client, port=redis_port, data_dir=instance.redis_data_dir(), deadline=deadline
        )
        if pg is None or redis is None:
            raise RuntimeError("PITR must capture live PostgreSQL and Redis before any data stop")
        receipt = DataStop(
            postgres=_receipt_owner(pg, instance._pg_data_dir(), pg_port),
            redis=_receipt_owner(redis, instance.redis_data_dir(), redis_port),
            pgbouncer=None
            if pgb is None
            else _receipt_owner(
                pgb, pooler.ini_path().parent, pooler_port, config=pooler.ini_path()
            ),
        )
        _require_no_unrecorded({name: owner.identity for name, owner in receipt.owners().items()})
        return receipt
    finally:
        await _close_receipt_client(client)


def capture_custody(timeout: float) -> DataStop:
    """Capture without effects; the operation must persist this before stopping."""
    return asyncio.run(_capture_custody(deadline_after(timeout)))


def _validate_receipt(receipt: DataStop) -> None:
    pg_port, redis_port, pooler_port = _receipt_resources()
    expected = {
        "postgres": (instance._pg_data_dir(), pg_port),
        "redis": (instance.redis_data_dir(), redis_port),
        "pgbouncer": (pooler.ini_path().parent, pooler_port),
    }
    for name, owner in receipt.owners().items():
        directory, port = expected[name]
        if (owner.directory, owner.port) != (str(directory.resolve()), port):
            raise RuntimeError("retained data custody names another configured resource")
        if (
            name == "pgbouncer"
            and owner.config_digest != hashlib.sha256(regular_bytes(pooler.ini_path())).hexdigest()
        ):
            raise RuntimeError("PgBouncer configuration changed after custody capture")
    _require_no_unrecorded({name: owner.identity for name, owner in receipt.owners().items()})


async def _stop_captured(receipt: DataStop, deadline: float) -> None:
    _validate_receipt(receipt)
    client = _receipt_client(receipt.redis.port)
    custody = ownership.RedisConnectionCustody()
    try:
        observed = await custody.capture(
            client,
            port=receipt.redis.port,
            data_dir=Path(receipt.redis.directory),
            deadline=deadline,
        )
        if observed is not None and not observed.same_birth(receipt.redis.identity):
            raise RuntimeError("Redis replacement cannot inherit retained stop authority")
        for name, owner in receipt.owners().items():
            identity = owner.identity
            if identity.live():
                ownership.require_listener(identity, owner.port)
                await _request_stop(name, identity, client, deadline, save=True)
            # Even after leader exit, retained descendants must all be gone.
            wait_for_exit(owner.identities, deadline)
            ownership.require_listener(None, owner.port, required=False)
        _require_no_unrecorded({})
    finally:
        await _close_receipt_client(client)


def stop_captured(receipt: DataStop, timeout: float) -> None:
    """Continue exact native stop without SQL drain or adopting new processes."""
    asyncio.run(_stop_captured(receipt, deadline_after(timeout)))


# ── Release write generations ─────────────────────────────────────────────────
# Each effect below runs as the OS-user administrator over the home's owner-only
# socket (`_data_plane.admin_session`), which no fence ever terminates.


@dataclass(frozen=True)
class WriteFence:
    """Receipt of a fenced generation: how the pooler ended and the closure census."""

    pooler: PoolerStop
    closure: ClosureEvidence


def _write_authority() -> tuple[Path, ClusterRecord, str]:
    from shared.cluster import db_identity, get_record
    from shared.paths import ava_home

    if sys.platform == "win32" or settings.data_plane.is_remote:
        raise RuntimeError("write generations exist only on an owned POSIX data plane")
    record = get_record(ava_home())
    if record is None:
        raise RuntimeError("write-generation effects require the home's retained registry")
    return ava_home().resolve(), record, db_identity()


def _require_served(home: Path, generation: Generation) -> None:
    """The pooler's userlist on disk holds exactly `generation` (and the admin console)."""
    if not settings.data_plane.pgbouncer_enabled:
        return
    from shared.cluster.authority import render_userlist

    try:
        served = (pooler.ini_path().parent / "userlist.txt").read_bytes()
    except FileNotFoundError:
        served = None
    if served != render_userlist(home, generation):
        raise AuthorityRefusedError(
            f"the pooler userlist does not serve exactly write generation {generation.number}"
        )


def preflight_write_authority() -> Generation:
    """Read-only admission of a release's database authority; returns the active
    generation the operation will fence.

    The ledger has an active generation, nothing pending and every revoked
    generation closed; the catalog invariant holds for exactly that generation;
    no prepared transaction exists; the pooler serves exactly that pair.
    """
    from cli.commands._data_plane import READONLY_GRANTEES, admin_session
    from shared.cluster.authority import check_invariant, read_pooler_admin, require_ledger

    home, record, database = _write_authority()
    ledger = require_ledger(home)
    active = ledger.active
    unclosed = [entry.number for entry in ledger.revoked if entry.state != "closed"]
    if active is None or ledger.pending is not None or unclosed:
        raise AuthorityRefusedError(
            "a release fences only an admitted write generation: "
            f"active={None if active is None else active.number} "
            f"pending={None if ledger.pending is None else ledger.pending.number} "
            f"unclosed={unclosed}"
        )
    with admin_session(record, database) as conn:
        verified = check_invariant(
            conn, home, database=database, readonly_grantees=READONLY_GRANTEES
        )
        prepared = [row[0] for row in conn.execute("SELECT gid FROM pg_prepared_xacts")]
    if verified is None or verified.number != active.number:
        raise AuthorityRefusedError("the catalog does not hold the ledger's active generation")
    if prepared:
        raise AuthorityRefusedError(f"prepared transactions exist: {prepared}")
    _require_served(home, active)
    read_pooler_admin(home)
    return active


def fence_write_generation(authority: OperationAuthority) -> WriteFence:
    """Close every writer of the unrevoked generation; each step is retry-safe.

    Revoke (ledger `revoking`, then the NOLOGIN sweep of every stale application
    login in one transaction) -> stop the owned pooler, escalating to a kill
    (this custody is quiescent and already revoked) and requiring no listener on
    its port -> terminate and census until no stale session and no prepared
    transaction remains (ledger `closed`, secret deleted) -> drop every closed
    login, keeping inert tombstones. A pooler reload never counts: PgBouncer
    keeps a removed user that already authenticated.
    """
    from cli.commands._data_plane import admin_session
    from shared.cluster import record_pgbouncer_port
    from shared.cluster.authority import close_revoked, prune, revoke

    home, record, database = _write_authority()
    with admin_session(record, database) as conn:
        revoke(conn, home, authority)
        stopped = pooler.stop_pgbouncer(force=True)
        ownership.require_listener(None, record_pgbouncer_port(record), required=False)
        closure, _closed = close_revoked(conn, home, authority)
        prune(conn, home, authority)
    return WriteFence(pooler=stopped, closure=closure)


def admit_write_generation(authority: OperationAuthority) -> Generation:
    """Mint (or exactly reconcile) the operation's generation and admit it.

    Mint (secret, then ledger `pending`, then the two LOGIN roles) -> a pooler
    serving exactly that pair (a changed userlist restarts it) -> a pooled
    `SELECT 1` as each login -> ledger `active`. Every step is idempotent, so
    a retry continues the same number and never mints another.
    """
    from cli.commands._data_plane import (
        _ensure_pooler,
        admin_session,
        db_endpoint,
        prove_generation_logins,
    )
    from shared.cluster.authority import (
        activate,
        mint_generation,
        require_ledger,
        verify_generation,
    )

    home, record, database = _write_authority()
    with admin_session(record, database) as conn:
        mint_generation(conn, home, authority)
        generation = require_ledger(home).unrevoked
        if generation is None:
            raise AuthorityRefusedError("the operation minted no write generation")
        _ensure_pooler(record, database, home, generation)
        prove_generation_logins(home, generation, db_endpoint())
        return activate(home, authority, verify_generation(conn, home))


def verify_write_generation(number: int, credential_digest: str) -> None:
    """After start: exactly the issued generation writes, and nothing older can.

    The ledger's active generation is the issued one; the catalog invariant
    holds (a revoked login can neither log in nor inherit); the pooler serves
    only that pair; no session of a stale application login, the owner, the
    groups or a dropped role survives; both logins answer through the endpoint.
    """
    from cli.commands._data_plane import (
        READONLY_GRANTEES,
        admin_session,
        db_endpoint,
        prove_generation_logins,
    )
    from shared.cluster.authority import active_generation, check_invariant, stale_sessions

    home, record, database = _write_authority()
    active = active_generation(home)
    if (active.number, active.credential_digest) != (number, credential_digest):
        raise AuthorityRefusedError(
            f"the ledger's active write generation {active.number} is not the issued {number}"
        )
    with admin_session(record, database) as conn:
        check_invariant(conn, home, database=database, readonly_grantees=READONLY_GRANTEES)
        survivors = stale_sessions(conn, home)
    if survivors:
        raise AuthorityRefusedError(f"sessions of fenced roles survive: {list(survivors)}")
    _require_served(home, active)
    prove_generation_logins(home, active, db_endpoint())
