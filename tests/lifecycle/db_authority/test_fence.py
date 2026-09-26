"""The fence on real PostgreSQL 17: revoke, closure by census, prepared
transactions, irreversibility, pruning without CASCADE, and crash retries."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import psycopg.conninfo
import pytest
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from shared.cluster.authority import (
    CatalogRefusedError,
    ClosureRefusedError,
    OperationAuthority,
    activate,
    check_invariant,
    close_revoked,
    ensure_groups,
    fenced_roles,
    mint_generation,
    prove_closure,
    prune,
    require_ledger,
    revoke,
    sweep,
)
from shared.cluster.authority import fence as fence_module
from shared.cluster.authority import ledger as ledger_module
from shared.cluster.authority import roles as roles_module
from shared.cluster.authority.model import SurvivingSession
from tests.lifecycle.db_authority.conftest import AuthorityCluster


def _operation() -> OperationAuthority:
    return OperationAuthority(operation=uuid4(), direction="candidate")


def _rotate(cluster: AuthorityCluster, authority: OperationAuthority) -> None:
    """One full rotation: revoke, prove closure, mint and admit the next generation."""
    with cluster.admin() as conn:
        revoke(conn, cluster.home, authority)
        close_revoked(conn, cluster.home, authority)
        verified = mint_generation(conn, cluster.home, authority)
    activate(cluster.home, authority, verified)


def _refused_login(
    cluster: AuthorityCluster,
    role: str,
    password: str,
    *,
    reason: str = "password authentication failed",
) -> None:
    """Over TCP and the socket. A revoked login loses its verifier, so SCRAM
    refuses first; a NOLOGIN role with a valid password is refused after it."""
    for via in ("tcp", "socket"):
        with pytest.raises(psycopg.OperationalError, match=reason):
            cluster.login(role, password, via=via)  # type: ignore[arg-type]


def test_rotation_revokes_closes_mints_and_prunes(authority_postgres: AuthorityCluster) -> None:
    cluster = authority_postgres
    old = cluster.active_secret()
    authority = _operation()
    _rotate(cluster, authority)
    with cluster.admin() as conn:
        check_invariant(conn, cluster.home, database=cluster.database)
        result = prune(conn, cluster.home, authority)
        assert result.dropped == ("ava_g0_gateway", "ava_g0_runner") and result.retained == ()
        check_invariant(conn, cluster.home, database=cluster.database)
    ledger = require_ledger(cluster.home)
    assert ledger.counter == 1 and ledger.active is not None and ledger.active.number == 1
    assert [(e.number, e.state, e.dropped) for e in ledger.revoked] == [(0, "closed", True)]
    assert not (cluster.home / "db-authority" / "generations" / "0.json").exists()
    for cls in ("gateway", "runner"):
        role = old.roles.of(cls)
        _refused_login(cluster, role.name, role.password)
        with cluster.connect_class(cls) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)


def test_revoked_login_never_logs_in_again(authority_postgres: AuthorityCluster) -> None:
    cluster = authority_postgres
    old = cluster.active_secret().roles.gateway
    authority = _operation()
    with cluster.admin() as conn:
        revoke(conn, cluster.home, authority)
    _refused_login(cluster, old.name, old.password)
    with cluster.admin() as conn:
        # Even with a known password restored, NOLOGIN alone refuses.
        conn.execute("ALTER ROLE ava_g0_gateway PASSWORD 'restored-password-value'")
        _refused_login(
            cluster, old.name, "restored-password-value", reason="not permitted to log in"
        )
        conn.execute("ALTER ROLE ava_g0_gateway PASSWORD NULL")
        close_revoked(conn, cluster.home, authority)
        verified = mint_generation(conn, cluster.home, authority)
        activate(cluster.home, authority, verified)
        ensure_groups(conn, owner=cluster.owner, database=cluster.database, groups=cluster.groups)
        sweep(conn, cluster.home)
        row = conn.execute(
            "SELECT rolcanlogin, rolpassword IS NULL FROM pg_authid WHERE rolname = %s", (old.name,)
        ).fetchone()
        assert row == (False, True)
        assert conn.execute(
            "SELECT count(*) FROM pg_auth_members WHERE member = %s::regrole", (old.name,)
        ).fetchone() == (0,)
    _refused_login(cluster, old.name, old.password)


@pytest.mark.parametrize("membership", [True, False])
def test_restored_login_is_detected_and_swept(
    authority_postgres: AuthorityCluster, membership: bool
) -> None:
    """A restored catalog may resurrect LOGIN on a revoked generation, with or
    without its group membership; the ledger record alone identifies it."""
    cluster = authority_postgres
    _rotate(cluster, _operation())
    with cluster.admin() as conn:
        conn.execute("ALTER ROLE ava_g0_runner LOGIN PASSWORD 'restored-old-credential'")
        if membership:
            conn.execute("GRANT ava_runner TO ava_g0_runner")
        with pytest.raises(CatalogRefusedError, match="revoked login ava_g0_runner can log in"):
            check_invariant(conn, cluster.home, database=cluster.database)
        result = sweep(conn, cluster.home)
        assert result.demoted == ("ava_g0_runner",)
        expected = (("ava_runner", "ava_g0_runner"),) if membership else ()
        assert result.memberships_revoked == expected
        check_invariant(conn, cluster.home, database=cluster.database)
    _refused_login(cluster, "ava_g0_runner", "restored-old-credential")


def _hold_idle_in_transaction(cluster: AuthorityCluster) -> psycopg.Connection[Any]:
    conn = cluster.connect_class("gateway")
    conn.execute("SELECT count(*) FROM agents")
    return conn


def test_live_sessions_block_closure_until_terminated(authority_postgres: AuthorityCluster) -> None:
    cluster = authority_postgres
    idle = _hold_idle_in_transaction(cluster)
    active = cluster.connect_class("runner", autocommit=True)
    errors: list[BaseException] = []

    def sleep_forever() -> None:
        try:
            active.execute("SELECT pg_sleep(600)")
        except BaseException as exc:
            errors.append(exc)

    sleeper = threading.Thread(target=sleep_forever, daemon=True)
    sleeper.start()
    authority = _operation()
    try:
        with cluster.admin() as conn:
            revoke(conn, cluster.home, authority)
            # Membership revocation alone fails the next statement but is not closure.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                idle.execute("SELECT count(*) FROM agents")
            census = fence_module._census(conn, fenced_roles(conn, cluster.home))
            assert {s.role for s in census} == {"ava_g0_gateway", "ava_g0_runner"}
            evidence, closed = close_revoked(conn, cluster.home, authority)
            assert evidence.terminated == 2
            assert [entry.number for entry in closed] == [0]
            assert fence_module._census(conn, evidence.roles) == ()
        sleeper.join(10)
        assert not sleeper.is_alive()
        assert isinstance(errors[0], psycopg.OperationalError)
        with pytest.raises(psycopg.OperationalError):
            idle.execute("SELECT 1")
    finally:
        idle.close()
        active.close()


async def test_fence_closes_a_pool_and_a_reconnect_loop_of_the_old_generation(
    authority_postgres: AuthorityCluster,
) -> None:
    """A pooled client shaped like the hosted runner's, and a lazy reconnect loop."""
    cluster = authority_postgres
    old = cluster.active_secret().roles.gateway
    dsn = psycopg.conninfo.make_conninfo(
        host="127.0.0.1",
        port=cluster.instance.port,
        user=old.name,
        password=old.password,
        dbname=cluster.database,
    )
    pool = AsyncConnectionPool[psycopg.AsyncConnection[Any]](
        dsn,
        min_size=2,
        max_size=2,
        timeout=1,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": None},
    )
    await pool.open(wait=True)
    authority = _operation()
    try:
        async with pool.connection() as borrowed:
            await borrowed.execute("SELECT count(*) FROM agents")
            with cluster.admin() as conn:
                revoke(conn, cluster.home, authority)
                evidence, _ = close_revoked(conn, cluster.home, authority)
            assert evidence.terminated == 2
            with pytest.raises(psycopg.OperationalError):
                await borrowed.execute("SELECT 1")
        # The pool first hands out its terminated idle session, then its own
        # reconnects fail authentication until the borrow times out.
        with pytest.raises(psycopg.OperationalError):
            async with pool.connection() as fresh:
                await fresh.execute("SELECT 1")
        with pytest.raises(PoolTimeout):
            async with pool.connection():
                pass
    finally:
        await pool.close()
    for _ in range(3):  # the lazy reconnect loop keeps dialing the old generation
        _refused_login(cluster, old.name, old.password)
    with cluster.admin() as conn:
        assert fence_module._census(conn, evidence.roles) == ()


def test_fence_closes_an_unrecorded_member_swept_by_the_revoke(
    authority_postgres: AuthorityCluster,
) -> None:
    """A login outside the ledger that held group membership is swept, and its
    live session is closed even though it is no longer a member afterwards."""
    cluster = authority_postgres
    with cluster.admin() as conn:
        conn.execute("CREATE ROLE stray_writer LOGIN PASSWORD 'stray-writer-password'")
        conn.execute("GRANT ava_runner TO stray_writer")
    stray = cluster.login("stray_writer", "stray-writer-password")
    stray.execute("SELECT count(*) FROM agents")
    authority = _operation()
    try:
        with cluster.admin() as conn:
            swept = revoke(conn, cluster.home, authority)
            assert "stray_writer" in swept.demoted
            assert "stray_writer" not in fenced_roles(conn, cluster.home)
            evidence, _ = close_revoked(conn, cluster.home, authority)
            assert evidence.terminated == 1
        with pytest.raises(psycopg.OperationalError):
            stray.execute("SELECT 1")
    finally:
        stray.close()


def test_a_census_survivor_holds_closure(
    authority_postgres: AuthorityCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = authority_postgres
    authority = _operation()
    ghost = SurvivingSession(2_000_000_000, "ava_g0_gateway", cluster.database, "idle", None, None)

    def census(_conn: object, _roles: tuple[str, ...]) -> tuple[SurvivingSession, ...]:
        return (ghost,)

    monkeypatch.setattr(fence_module, "_census", census)
    with cluster.admin() as conn:
        revoke(conn, cluster.home, authority)
        with pytest.raises(ClosureRefusedError, match="sessions survived") as refused:
            close_revoked(conn, cluster.home, authority, rounds=2, timeout_ms=100)
    assert refused.value.survivors == (ghost,)
    assert refused.value.unconfirmed_signals == (ghost.pid, ghost.pid)
    assert [entry.state for entry in require_ledger(cluster.home).revoked] == ["revoking"]


def test_a_sent_signal_never_counts_as_closed(
    authority_postgres: AuthorityCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = authority_postgres
    held = _hold_idle_in_transaction(cluster)
    pid = held.info.backend_pid
    authority = _operation()

    def signal_sent(_conn: object, _pid: int, _timeout_ms: int) -> bool:
        return True  # a sent signal, with no effect on the backend

    monkeypatch.setattr(fence_module, "_terminate", signal_sent)
    try:
        with cluster.admin() as conn:
            revoke(conn, cluster.home, authority)
            with pytest.raises(ClosureRefusedError) as refused:
                close_revoked(conn, cluster.home, authority, rounds=2, timeout_ms=100)
        assert [session.pid for session in refused.value.survivors] == [pid]
        assert require_ledger(cluster.home).revoked[0].state == "revoking"
        assert (cluster.home / "db-authority" / "generations" / "0.json").exists()
    finally:
        held.close()


def test_a_prepared_transaction_blocks_closure(authority_postgres: AuthorityCluster) -> None:
    cluster = authority_postgres
    with cluster.connect_class("gateway") as stale:
        stale.execute("SELECT count(*) FROM agents")
        stale.execute("PREPARE TRANSACTION 'stale-writer-g0'")
    authority = _operation()
    with cluster.admin() as conn:
        revoke(conn, cluster.home, authority)
        with pytest.raises(ClosureRefusedError, match="prepared transactions") as refused:
            close_revoked(conn, cluster.home, authority)
        assert [(p.gid, p.owner) for p in refused.value.prepared] == [
            ("stale-writer-g0", "ava_g0_gateway")
        ]
        assert require_ledger(cluster.home).revoked[0].state == "revoking"
        conn.execute("ROLLBACK PREPARED 'stale-writer-g0'")
        close_revoked(conn, cluster.home, authority)
    assert require_ledger(cluster.home).revoked[0].state == "closed"


def test_closure_refuses_roles_that_can_still_log_in(authority_postgres: AuthorityCluster) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn, pytest.raises(CatalogRefusedError, match="can still log in"):
        prove_closure(conn, ("ava_g0_gateway",))


def test_closure_counts_sessions_of_a_dropped_role(authority_postgres: AuthorityCluster) -> None:
    """PostgreSQL drops a role while its session lives on; the census still sees it."""
    cluster = authority_postgres
    held = _hold_idle_in_transaction(cluster)
    try:
        with cluster.admin() as conn:
            conn.execute("ALTER ROLE ava_g0_gateway NOLOGIN")
            conn.execute("REVOKE ava_gateway FROM ava_g0_gateway")
            conn.execute("DROP ROLE ava_g0_gateway")
            orphans = fence_module._census(conn, ())
            assert [session.pid for session in orphans] == [held.info.backend_pid]
            evidence = prove_closure(conn, ())
            assert evidence.terminated == 1
    finally:
        held.close()


def test_prune_retains_an_inert_tombstone_without_cascade(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    authority = _operation()
    _rotate(cluster, authority)
    with cluster.admin() as conn:
        conn.execute("GRANT SELECT ON agents TO ava_g0_runner")
        before = conn.execute("SELECT count(*) FROM information_schema.table_privileges").fetchone()
        result = prune(conn, cluster.home, authority)
        assert result.dropped == ("ava_g0_gateway",)
        assert [name for name, _ in result.retained] == ["ava_g0_runner"]
        assert "privileges for table agents" in result.retained[0][1]
        assert conn.execute("SELECT to_regclass('public.agents') IS NOT NULL").fetchone() == (True,)
        after = conn.execute("SELECT count(*) FROM information_schema.table_privileges").fetchone()
        assert after == before
        row = conn.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'ava_g0_runner'"
        ).fetchone()
        assert row == (False,)
        with pytest.raises(CatalogRefusedError, match="grants SELECT to ava_g0_runner"):
            check_invariant(conn, cluster.home, database=cluster.database)
        entry = require_ledger(cluster.home).revoked[0]
        assert not entry.dropped and entry.drop_error is not None
        assert "ava_g0_runner" in entry.drop_error
        conn.execute("REVOKE SELECT ON agents FROM ava_g0_runner")
        assert prune(conn, cluster.home, authority).dropped == ("ava_g0_runner",)
        check_invariant(conn, cluster.home, database=cluster.database)
    entry = require_ledger(cluster.home).revoked[0]
    assert entry.dropped and entry.drop_error is None


def test_prune_refuses_a_revoked_role_that_regained_login(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    authority = _operation()
    _rotate(cluster, authority)
    with cluster.admin() as conn:
        conn.execute("ALTER ROLE ava_g0_gateway LOGIN")
        with pytest.raises(CatalogRefusedError, match="regained LOGIN"):
            prune(conn, cluster.home, authority)


class _CrashError(Exception):
    pass


def test_revoke_retry_after_crash_before_the_sweep(
    authority_postgres: AuthorityCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = authority_postgres
    authority = _operation()

    def crash(_conn: Any, _home: Path) -> None:
        raise _CrashError

    with monkeypatch.context() as patched:
        patched.setattr(fence_module, "sweep", crash)
        with cluster.admin() as conn, pytest.raises(_CrashError):
            revoke(conn, cluster.home, authority)
    assert require_ledger(cluster.home).revoked[0].state == "revoking"
    with cluster.admin() as conn:
        # The login still has LOGIN, so closure refuses until the retry sweeps it.
        with pytest.raises(CatalogRefusedError, match="can still log in"):
            close_revoked(conn, cluster.home, authority)
        result = revoke(conn, cluster.home, authority)
        assert result.demoted == ("ava_g0_gateway", "ava_g0_runner")
        close_revoked(conn, cluster.home, authority)
    assert require_ledger(cluster.home).revoked[0].state == "closed"


def test_close_retry_after_crash_before_secret_deletion(
    authority_postgres: AuthorityCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = authority_postgres
    authority = _operation()
    secret = cluster.home / "db-authority" / "generations" / "0.json"
    with monkeypatch.context() as patched:
        patched.setattr(ledger_module, "_secret_numbers", _crash_once_closed())
        with cluster.admin() as conn:
            revoke(conn, cluster.home, authority)
            with pytest.raises(_CrashError):
                close_revoked(conn, cluster.home, authority)
    assert require_ledger(cluster.home).revoked[0].state == "closed"
    assert secret.exists()
    with cluster.admin() as conn:
        evidence, closed = close_revoked(conn, cluster.home, authority)
    assert closed == () and evidence.terminated == 0
    assert not secret.exists()


def _crash_once_closed() -> Any:
    """Crash when secret deletion starts, i.e. after the closed ledger is durable."""
    original = ledger_module._secret_numbers

    def numbers(home: Path) -> set[int]:
        if require_ledger(home).revoked[0].state == "closed":
            raise _CrashError
        return original(home)

    return numbers


def test_prune_retry_after_crash_before_recording_the_drop(
    authority_postgres: AuthorityCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = authority_postgres
    authority = _operation()
    _rotate(cluster, authority)

    def crash(*_args: Any) -> None:
        raise _CrashError

    with monkeypatch.context() as patched:
        patched.setattr(roles_module, "record_drops", crash)
        with cluster.admin() as conn, pytest.raises(_CrashError):
            prune(conn, cluster.home, authority)
    assert not require_ledger(cluster.home).revoked[0].dropped
    with cluster.admin() as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname ~ '^ava_g0_'"
        ).fetchone() == (0,)
        result = prune(conn, cluster.home, authority)
    # The roles are already gone; the retry only records the completed effect.
    assert result.dropped == () and result.retained == ()
    assert require_ledger(cluster.home).revoked[0].dropped
