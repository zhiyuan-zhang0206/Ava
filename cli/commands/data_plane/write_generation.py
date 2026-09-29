"""A release operation's write-generation effects on this home's owned data plane.

The old generation's fence, the next generation's admission, and their
read-only checks; the release journal records intent and receipts around
these, the home's ledger is the authority. Each effect runs as the OS-user
administrator over the home's owner-only socket
(`cli.commands.data_plane.bringup.admin_session`), which no fence ever
terminates.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.data_plane.pgbouncer import PoolerStop
from shared.cluster import ownership
from shared.cluster.authority import (
    AuthorityRefusedError,
    ClosureEvidence,
    Generation,
    OperationAuthority,
)
from shared.cluster.registry import ClusterRecord
from shared.config import settings


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
    from cli.commands.data_plane.bringup import READONLY_GRANTEES, admin_session
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
    from cli.commands.data_plane.bringup import admin_session
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

    Mint (secret, then ledger `pending`, then the two LOGIN roles) -> a direct
    `SELECT 1` as each login on the home's own Postgres -> ledger `active`.
    Every step is idempotent, so a retry continues the same number and never
    mints another.

    The finite executor births no pooler here (the fence stopped the old one):
    a process it forks shares its native custody, on Linux the transient
    unit's cgroup (`KillMode=control-group`), and dies when the executor
    exits. The stage's ordinary start, run by the root boot owner, births the
    fresh pooler serving exactly this pair and proves a pooled login of each
    before any service launches (`complete_gateway_data_plane`); observation
    proves it again (`verify_write_generation`).
    """
    from cli.commands.data_plane.bringup import (
        admin_session,
        db_endpoint,
        prove_generation_logins,
    )
    from shared.cluster import record_postgres_port
    from shared.cluster.authority import (
        activate,
        mint_generation,
        require_ledger,
        verify_generation,
    )
    from shared.host.net.url_secret import url_with_port

    home, record, database = _write_authority()
    with admin_session(record, database) as conn:
        mint_generation(conn, home, authority)
        generation = require_ledger(home).unrevoked
        if generation is None:
            raise AuthorityRefusedError("the operation minted no write generation")
        direct = url_with_port(db_endpoint(), record_postgres_port(record))
        prove_generation_logins(home, generation, direct)
        return activate(home, authority, verify_generation(conn, home))


def verify_write_generation(number: int, credential_digest: str) -> None:
    """After start: exactly the issued generation writes, and nothing older can.

    The ledger's active generation is the issued one; the catalog invariant
    holds (a revoked login can neither log in nor inherit); the pooler serves
    only that pair; no session of a stale application login, the owner, the
    groups or a dropped role survives; both logins answer through the endpoint.
    """
    from cli.commands.data_plane.bringup import (
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
