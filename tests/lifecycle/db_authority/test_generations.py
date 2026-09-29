"""Generation logins on real PostgreSQL 17: birth, authentication, privilege
equality with the group, denials, and crash-safe exact-retry minting."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from shared.cluster.authority import (
    CatalogRefusedError,
    LedgerRefusedError,
    OperationAuthority,
    activate,
    check_invariant,
    close_revoked,
    mint_generation,
    require_ledger,
    revoke,
)
from shared.cluster.authority import ledger as ledger_module
from shared.cluster.authority import roles as roles_module
from tests.lifecycle.db_authority.conftest import OWNER, AuthorityCluster

_LOGIN_ATTRIBUTES = (
    "SELECT rolcanlogin, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolreplication,"
    " rolbypassrls FROM pg_roles WHERE rolname = %s"
)


def _generation_roles(conn: psycopg.Connection[Any]) -> list[str]:
    rows = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname ~ '^ava_g[0-9]+_(gateway|runner)$' ORDER BY 1"
    ).fetchall()
    return [row[0] for row in rows]


def test_birth_creates_exactly_generation_zero(authority_postgres: AuthorityCluster) -> None:
    cluster = authority_postgres
    ledger = require_ledger(cluster.home)
    assert ledger.counter == 0 and ledger.pending is None and ledger.revoked == ()
    assert ledger.active is not None and ledger.active.roles == ("ava_g0_gateway", "ava_g0_runner")
    assert ledger.active.origin.kind == "birth"
    with cluster.admin() as conn:
        verified = check_invariant(conn, cluster.home, database=cluster.database)
        assert verified is not None and verified.number == 0
        for login in ledger.active.roles:
            row = conn.execute(_LOGIN_ATTRIBUTES, (login,)).fetchone()
            assert row == (True, False, True, False, False, False, False)
        for role in (OWNER, "ava_gateway", "ava_runner"):
            row = conn.execute(_LOGIN_ATTRIBUTES, (role,)).fetchone()
            assert row is not None and row[0] is False and row[1] is False
        members = conn.execute(
            "SELECT g.rolname, m.rolname, am.admin_option, am.inherit_option, am.set_option"
            " FROM pg_auth_members am JOIN pg_roles g ON g.oid = am.roleid"
            " JOIN pg_roles m ON m.oid = am.member WHERE m.rolname ~ '^ava_g' ORDER BY 1"
        ).fetchall()
        assert members == [
            ("ava_gateway", "ava_g0_gateway", False, True, False),
            ("ava_runner", "ava_g0_runner", False, True, False),
        ]
    store = cluster.home / "db-authority"
    assert store.stat().st_mode & 0o777 == 0o700
    assert (store / "ledger.json").stat().st_mode & 0o777 == 0o600
    assert (store / "generations" / "0.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("via", ["tcp", "socket"])
def test_logins_authenticate_only_with_their_secret(
    authority_postgres: AuthorityCluster, via: str
) -> None:
    cluster = authority_postgres
    secret = cluster.active_secret()
    for cls in ("gateway", "runner"):
        role = secret.roles.of(cls)
        with cluster.login(role.name, role.password, via=via) as conn:  # type: ignore[arg-type]
            row = conn.execute("SELECT session_user, current_user").fetchone()
            assert row == (role.name, role.name)
        with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
            cluster.login(role.name, role.password + "x", via=via)  # type: ignore[arg-type]
    for nologin in (OWNER, "ava_gateway", "ava_runner"):
        with pytest.raises(psycopg.OperationalError):
            cluster.login(nologin, "anything", via=via)  # type: ignore[arg-type]


_PRIVILEGE_MATRIX = """
SELECT 'table', c.oid::regclass::text, p.priv,
       has_table_privilege(%(login)s, c.oid, p.priv), has_table_privilege(%(group)s, c.oid, p.priv)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES',
                        'TRIGGER','MAINTAIN']) p(priv)
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')
UNION ALL
SELECT 'sequence', c.oid::regclass::text, p.priv,
       has_sequence_privilege(%(login)s, c.oid, p.priv),
       has_sequence_privilege(%(group)s, c.oid, p.priv)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN unnest(ARRAY['USAGE','SELECT','UPDATE']) p(priv)
WHERE n.nspname = 'public' AND c.relkind = 'S'
UNION ALL
SELECT 'routine', p.oid::regprocedure::text, 'EXECUTE',
       has_function_privilege(%(login)s, p.oid, 'EXECUTE'),
       has_function_privilege(%(group)s, p.oid, 'EXECUTE')
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
"""


@pytest.mark.parametrize(
    ("login", "group"), [("ava_g0_gateway", "ava_gateway"), ("ava_g0_runner", "ava_runner")]
)
def test_login_privileges_equal_its_group(
    authority_postgres: AuthorityCluster, login: str, group: str
) -> None:
    with authority_postgres.admin() as conn:
        rows = conn.execute(_PRIVILEGE_MATRIX, {"login": login, "group": group}).fetchall()
        owned = conn.execute(
            "SELECT count(*) FROM pg_shdepend WHERE refclassid = 'pg_authid'::regclass"
            " AND refobjid = %s::regrole",
            (login,),
        ).fetchone()
    assert len(rows) > 300
    assert [row for row in rows if row[3] != row[4]] == []
    assert owned == (0,)


def test_gateway_group_holds_the_full_dml_surface(authority_postgres: AuthorityCluster) -> None:
    with authority_postgres.admin() as conn:
        rows = conn.execute(
            _PRIVILEGE_MATRIX, {"login": "ava_g0_gateway", "group": "ava_gateway"}
        ).fetchall()
    required = {
        "table": {"SELECT", "INSERT", "UPDATE", "DELETE"},
        "sequence": {"USAGE", "SELECT", "UPDATE"},
        "routine": {"EXECUTE"},
    }
    missing = [row[:3] for row in rows if row[2] in required[row[0]] and not row[3]]
    assert missing == []
    forbidden = {"TRUNCATE", "REFERENCES", "TRIGGER"}
    assert [row[:3] for row in rows if row[2] in forbidden and row[3]] == []
    maintained = {row[1] for row in rows if row[2] == "MAINTAIN" and row[3]}
    assert maintained == {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}


@pytest.mark.parametrize("cls", ["gateway", "runner"])
def test_login_is_denied_ddl_and_identity_changes(
    authority_postgres: AuthorityCluster, cls: str
) -> None:
    denials = (
        "CREATE TABLE denied_probe (x int)",
        "ALTER TABLE agents ADD COLUMN denied_probe int",
        "SET ROLE ava_gateway",
        "SET ROLE ava_runner",
        f"SET ROLE {OWNER}",
        f"SET SESSION AUTHORIZATION {OWNER}",
        "CREATE SCHEMA denied_probe",
    )
    with authority_postgres.connect_class(cls, autocommit=True) as conn:  # type: ignore[arg-type]
        for statement in denials:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)  # type: ignore[arg-type]
        row = conn.execute("SELECT session_user = current_user").fetchone()
        assert row == (True,)


def _fence_generation_zero(cluster: AuthorityCluster) -> OperationAuthority:
    authority = OperationAuthority(operation=uuid4(), direction="candidate")
    with cluster.admin() as conn:
        revoke(conn, cluster.home, authority)
        close_revoked(conn, cluster.home, authority)
    return authority


class _CrashError(Exception):
    pass


def _crash_after(target: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        target(*args, **kwargs)
        raise _CrashError

    return wrapped


def _crash_before(*_args: Any, **_kwargs: Any) -> Any:
    raise _CrashError


_BOUNDARIES = ["secret-published", "pending-recorded", "roles-created", "verified"]


def _inject(monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    if boundary == "secret-published":
        original = ledger_module.write_private_bytes

        def fail_pending(path: Path, data: bytes) -> None:
            if b'"pending": {' in data:
                raise _CrashError
            original(path, data)

        monkeypatch.setattr(ledger_module, "write_private_bytes", fail_pending)
    elif boundary == "pending-recorded":
        monkeypatch.setattr(roles_module, "_create_missing", _crash_before)
    elif boundary == "roles-created":
        monkeypatch.setattr(
            roles_module, "_create_missing", _crash_after(roles_module._create_missing)
        )
    else:
        monkeypatch.setattr(
            roles_module, "verify_generation", _crash_after(roles_module.verify_generation)
        )


@pytest.mark.parametrize("boundary", _BOUNDARIES)
def test_mint_retry_after_crash_reconciles_the_same_generation(
    authority_postgres: AuthorityCluster, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    cluster = authority_postgres
    authority = _fence_generation_zero(cluster)
    with monkeypatch.context() as patched:
        _inject(patched, boundary)
        with cluster.admin() as conn, pytest.raises(_CrashError):
            mint_generation(conn, cluster.home, authority)
    ledger = require_ledger(cluster.home)
    assert ledger.counter == (0 if boundary == "secret-published" else 1)
    with cluster.admin() as conn:
        verified = mint_generation(conn, cluster.home, authority)
        again = mint_generation(conn, cluster.home, authority)
        assert again == verified and verified.number == 1
        activate(cluster.home, authority, verified)
        assert mint_generation(conn, cluster.home, authority) == verified
        assert _generation_roles(conn) == [
            "ava_g0_gateway",
            "ava_g0_runner",
            "ava_g1_gateway",
            "ava_g1_runner",
        ]
        check_invariant(conn, cluster.home, database=cluster.database)
    ledger = require_ledger(cluster.home)
    assert ledger.counter == 1 and ledger.active is not None and ledger.active.number == 1
    assert sorted(p.name for p in (cluster.home / "db-authority" / "generations").iterdir()) == [
        "1.json"
    ]
    with cluster.connect_class("gateway") as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("ava_g1_gateway",)


def test_retry_holds_when_the_stored_verifier_differs(
    authority_postgres: AuthorityCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = authority_postgres
    authority = _fence_generation_zero(cluster)
    with monkeypatch.context() as patched:
        _inject(patched, "roles-created")
        with cluster.admin() as conn, pytest.raises(_CrashError):
            mint_generation(conn, cluster.home, authority)
    with cluster.admin() as conn:
        conn.execute("ALTER ROLE ava_g1_gateway PASSWORD 'foreign-credential-value'")
        with pytest.raises(CatalogRefusedError, match="stored verifier differs"):
            mint_generation(conn, cluster.home, authority)
        assert _generation_roles(conn) == [
            "ava_g0_gateway",
            "ava_g0_runner",
            "ava_g1_gateway",
            "ava_g1_runner",
        ]
    ledger = require_ledger(cluster.home)
    assert ledger.counter == 1 and ledger.pending is not None and ledger.active is None


def test_mint_holds_when_the_next_names_already_exist(authority_postgres: AuthorityCluster) -> None:
    cluster = authority_postgres
    authority = _fence_generation_zero(cluster)
    with cluster.admin() as conn:
        conn.execute("CREATE ROLE ava_g1_runner NOLOGIN")
        with pytest.raises(CatalogRefusedError, match="already exist before their mint"):
            mint_generation(conn, cluster.home, authority)
    ledger = require_ledger(cluster.home)
    assert ledger.counter == 0 and ledger.pending is None
    assert not (cluster.home / "db-authority" / "generations" / "1.json").exists()


def test_mint_holds_while_another_login_holds_group_membership(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    authority = _fence_generation_zero(cluster)
    with cluster.admin() as conn:
        conn.execute("CREATE ROLE stray_writer LOGIN PASSWORD 'stray-writer-password'")
        conn.execute("GRANT ava_runner TO stray_writer")
        with pytest.raises(CatalogRefusedError, match="stray_writer"):
            mint_generation(conn, cluster.home, authority)
    assert require_ledger(cluster.home).pending is None


def test_another_authority_cannot_adopt_or_activate_a_pending_generation(
    authority_postgres: AuthorityCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = authority_postgres
    authority = _fence_generation_zero(cluster)
    with monkeypatch.context() as patched:
        _inject(patched, "verified")
        with cluster.admin() as conn, pytest.raises(_CrashError):
            mint_generation(conn, cluster.home, authority)
    other = OperationAuthority(operation=uuid4(), direction="previous")
    with cluster.admin() as conn:
        with pytest.raises(LedgerRefusedError, match="another authority"):
            mint_generation(conn, cluster.home, other)
        verified = mint_generation(conn, cluster.home, authority)
    with pytest.raises(LedgerRefusedError, match="exact pending generation"):
        activate(cluster.home, other, verified)


def test_mint_refuses_a_non_admin_session(authority_postgres: AuthorityCluster) -> None:
    cluster = authority_postgres
    authority = OperationAuthority(operation=uuid4(), direction="candidate")
    with (
        cluster.connect_class("gateway", autocommit=True) as conn,
        pytest.raises(Exception, match="superuser session"),
    ):
        mint_generation(conn, cluster.home, authority)
    with cluster.admin() as conn:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(OWNER)))
        with pytest.raises(Exception, match="superuser session"):
            mint_generation(conn, cluster.home, authority)
    with cluster.instance.admin(cluster.database) as conn:
        conn.autocommit = False
        with pytest.raises(Exception, match="autocommit"):
            mint_generation(conn, cluster.home, authority)


def test_a_pending_generation_without_roles_is_revoked_and_closed(
    authority_postgres: AuthorityCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mint that crashed before creating roles can be abandoned by the fence;
    the next mint allocates a new number and never reuses the abandoned one."""
    cluster = authority_postgres
    authority = _fence_generation_zero(cluster)
    with monkeypatch.context() as patched:
        _inject(patched, "pending-recorded")
        with cluster.admin() as conn, pytest.raises(_CrashError):
            mint_generation(conn, cluster.home, authority)
    recovery = OperationAuthority(operation=uuid4(), direction="previous")
    with cluster.admin() as conn:
        assert "ava_g1_gateway" not in _generation_roles(conn)
        revoke(conn, cluster.home, recovery)
        close_revoked(conn, cluster.home, recovery)
        verified = mint_generation(conn, cluster.home, recovery)
        activate(cluster.home, recovery, verified)
        assert verified.number == 2
        check_invariant(conn, cluster.home, database=cluster.database)
    ledger = require_ledger(cluster.home)
    assert [(e.number, e.state) for e in ledger.revoked] == [(0, "closed"), (1, "closed")]
