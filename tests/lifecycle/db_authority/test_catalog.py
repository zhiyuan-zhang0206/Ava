"""Group grants and the fail-closed invariant on real PostgreSQL 17,
MAINTAIN/VACUUM, and cutover. The runner matrix itself is exercised through a
group login in tests/shared/test_runner_role.py."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from shared.cluster.authority import (
    CatalogRefusedError,
    CutoverAuthority,
    VacuumSkippedError,
    activate,
    check_invariant,
    create_ledger,
    ensure_groups,
    ensure_monitor,
    mint_generation,
    prove_closure,
    retire_legacy_logins,
    vacuum_or_fail,
)
from tests.lifecycle.db_authority.conftest import OWNER, AuthorityCluster

# (mutation run by the admin, expected violation fragment)
_UNKNOWN_EFFECTS = [
    ("ALTER ROLE ava_g0_gateway CREATEDB", "ava_g0_gateway holds CREATEDB"),
    (f"GRANT {OWNER} TO ava_g0_runner", "schema-owner membership"),
    ("ALTER ROLE ava_g0_gateway SET work_mem = '8MB'", "ava_g0_gateway carries role settings"),
    ("GRANT SELECT ON agents TO ava_g0_gateway", "ava_g0_gateway owns objects or holds direct"),
    ("CREATE TABLE stray_owned (x int); ALTER TABLE stray_owned OWNER TO ava_g0_runner", "owns"),
    (
        "REVOKE ava_gateway FROM ava_g0_gateway;"
        " GRANT ava_gateway TO ava_g0_gateway WITH INHERIT TRUE, SET TRUE, ADMIN FALSE",
        "differ from INHERIT-only",
    ),
    ("ALTER ROLE ava_g0_gateway PASSWORD 'foreign-credential'", "stored verifier differs"),
    ("ALTER ROLE ava_gateway LOGIN", "group ava_gateway can log in"),
    (f"ALTER ROLE {OWNER} LOGIN", "schema owner ava_test_owner can log in"),
    ("CREATE ROLE stray LOGIN PASSWORD 'stray-password'; GRANT ava_runner TO stray", "stray"),
    ("CREATE ROLE aux LOGIN; GRANT INSERT ON agents TO aux", "grants INSERT to aux"),
    ("CREATE ROLE aux LOGIN; GRANT pg_write_all_data TO aux", "login aux holds write privileges"),
    ("CREATE ROLE su LOGIN SUPERUSER", "login su is an unexpected superuser"),
    ("GRANT CONNECT ON DATABASE {db} TO PUBLIC", "PUBLIC may connect"),
    (
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} GRANT INSERT ON TABLES TO PUBLIC",
        "default privileges grant INSERT to PUBLIC",
    ),
    ("DROP ROLE ava_g0_runner", "ava_g0_runner does not exist"),
]


@pytest.mark.parametrize(("mutation", "violation"), _UNKNOWN_EFFECTS)
def test_invariant_refuses_every_unknown_effect(
    authority_postgres: AuthorityCluster, mutation: str, violation: str
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        check_invariant(conn, cluster.home, database=cluster.database)
        conn.execute(mutation.format(db=cluster.database))  # type: ignore[arg-type]
        with pytest.raises(CatalogRefusedError) as refused:
            check_invariant(conn, cluster.home, database=cluster.database)
    assert any(violation in item for item in refused.value.violations), refused.value.violations


# (drift of the converged monitoring login, expected violation fragment)
_MONITOR_DRIFT = [
    ("ALTER ROLE ava_monitor PASSWORD 'monitor-password'", "ava_monitor has a password"),
    ("ALTER ROLE ava_monitor CREATEROLE", "ava_monitor holds attributes"),
    ("ALTER ROLE ava_monitor SET work_mem = '8MB'", "ava_monitor holds attributes"),
    ("GRANT pg_monitor TO ava_monitor", "ava_monitor memberships"),
    ("CREATE ROLE aux LOGIN; GRANT ava_monitor TO aux", "ava_monitor has members"),
    ("GRANT SELECT ON agents TO ava_monitor", "grants SELECT to ava_monitor"),
    ("GRANT CONNECT ON DATABASE postgres TO ava_monitor", "holds grants beyond CONNECT"),
    (
        "CREATE TABLE monitor_owned (x int); ALTER TABLE monitor_owned OWNER TO ava_monitor",
        "ava_monitor owns objects or holds grants",
    ),
]


@pytest.mark.parametrize(("mutation", "violation"), _MONITOR_DRIFT)
def test_invariant_holds_the_monitoring_login_to_its_shape(
    authority_postgres: AuthorityCluster, mutation: str, violation: str
) -> None:
    """The converged monitor (password-less LOGIN, INHERIT-only pg_read_all_stats,
    CONNECT on the cluster database) passes the invariant; any drift is a
    violation, and converging again refuses instead of repairing it."""
    cluster = authority_postgres
    with cluster.admin() as conn:
        ensure_monitor(conn, database=cluster.database)
        ensure_monitor(conn, database=cluster.database)
        facts = conn.execute(
            "SELECT rolcanlogin, rolpassword IS NULL, rolsuper FROM pg_authid"
            " WHERE rolname = 'ava_monitor'"
        ).fetchone()
        assert facts == (True, True, False)
        check_invariant(conn, cluster.home, database=cluster.database)
        conn.execute(mutation)  # type: ignore[arg-type]
        with pytest.raises(CatalogRefusedError) as refused:
            check_invariant(conn, cluster.home, database=cluster.database)
        assert any(violation in item for item in refused.value.violations), refused.value.violations
        with pytest.raises(CatalogRefusedError):
            ensure_monitor(conn, database=cluster.database)


def test_invariant_admits_only_allowlisted_read_only_grantees(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        conn.execute("CREATE ROLE grafana_ro LOGIN PASSWORD 'read-only-password'")
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO grafana_ro").format(
                sql.Identifier(cluster.database)
            )
        )
        conn.execute("GRANT SELECT ON agents TO grafana_ro")
        with pytest.raises(CatalogRefusedError, match="grants SELECT to grafana_ro"):
            check_invariant(conn, cluster.home, database=cluster.database)
        verified = check_invariant(
            conn, cluster.home, database=cluster.database, readonly_grantees=("grafana_ro",)
        )
        assert verified is not None and verified.number == 0
        conn.execute("GRANT UPDATE ON agents TO grafana_ro")
        with pytest.raises(CatalogRefusedError, match="grants UPDATE to grafana_ro"):
            check_invariant(
                conn, cluster.home, database=cluster.database, readonly_grantees=("grafana_ro",)
            )


def test_default_privileges_cover_tables_created_after_birth(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(OWNER)))
        conn.execute("CREATE TABLE later_migration (id bigserial PRIMARY KEY, note text)")
        conn.execute("RESET ROLE")
        check_invariant(conn, cluster.home, database=cluster.database)
    with cluster.connect_class("gateway", autocommit=True) as gateway:
        gateway.execute("INSERT INTO later_migration (note) VALUES ('gateway')")
        gateway.execute("UPDATE later_migration SET note = 'updated'")
    with cluster.connect_class("runner", autocommit=True) as runner:
        assert runner.execute("SELECT note FROM later_migration").fetchall() == [("updated",)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runner.execute("INSERT INTO later_migration (note) VALUES ('runner')")


def test_ensure_groups_is_idempotent_and_never_changes_login(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        for _ in range(2):
            ensure_groups(
                conn, owner=cluster.owner, database=cluster.database, groups=cluster.groups
            )
        check_invariant(conn, cluster.home, database=cluster.database)
        conn.execute("ALTER ROLE ava_runner LOGIN")
        with pytest.raises(CatalogRefusedError, match="converts through the cutover"):
            ensure_groups(
                conn, owner=cluster.owner, database=cluster.database, groups=cluster.groups
            )
        row = conn.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'ava_runner'"
        ).fetchone()
        assert row == (True,)


def test_gateway_vacuum_requires_maintain_and_a_skip_is_a_failure(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    with cluster.connect_class("gateway", autocommit=True) as gateway:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            vacuum_or_fail(gateway, table)
        with cluster.admin() as conn:
            conn.execute("REVOKE MAINTAIN ON checkpoints FROM ava_gateway")
        notices: list[str] = []
        gateway.add_notice_handler(lambda diag: notices.append(diag.message_primary or ""))
        gateway.execute("VACUUM (ANALYZE) checkpoints")  # plain VACUUM only warns
        assert any("skipping" in notice for notice in notices)
        with pytest.raises(VacuumSkippedError, match="permission denied to vacuum"):
            vacuum_or_fail(gateway, "checkpoints")


def test_cutover_retires_legacy_logins_closes_their_sessions_and_mints(
    authority_unborn: AuthorityCluster,
) -> None:
    cluster = authority_unborn
    authority = CutoverAuthority()
    with cluster.admin() as conn:
        conn.execute(
            sql.SQL("ALTER ROLE {} LOGIN PASSWORD 'legacy-owner-password'").format(
                sql.Identifier(OWNER)
            )
        )
        conn.execute("CREATE ROLE ava_runner LOGIN PASSWORD 'legacy-runner-password'")
        conn.execute("GRANT SELECT ON agents TO ava_runner")
    legacy_owner = cluster.login(OWNER, "legacy-owner-password")
    legacy_runner = cluster.login("ava_runner", "legacy-runner-password", via="socket")
    legacy_runner.execute("SELECT count(*) FROM agents")
    try:
        with cluster.admin() as conn:
            with pytest.raises(CatalogRefusedError, match="converts through the cutover"):
                ensure_groups(
                    conn, owner=cluster.owner, database=cluster.database, groups=cluster.groups
                )
            with conn.transaction():
                demoted = retire_legacy_logins(
                    conn, owner=cluster.owner, groups=cluster.groups, authority=authority
                )
                ensure_groups(
                    conn, owner=cluster.owner, database=cluster.database, groups=cluster.groups
                )
            assert demoted == ("ava_runner", OWNER)
            evidence = prove_closure(conn, (cluster.owner, "ava_gateway", "ava_runner"))
            assert evidence.terminated == 2
            create_ledger(
                cluster.home, owner=cluster.owner, groups=cluster.groups, authority=authority
            )
            verified = mint_generation(conn, cluster.home, authority)
            activate(cluster.home, authority, verified)
            assert check_invariant(conn, cluster.home, database=cluster.database) == verified
        for session in (legacy_owner, legacy_runner):
            with pytest.raises(psycopg.OperationalError):
                session.execute("SELECT 1")
        for role, password in (
            (OWNER, "legacy-owner-password"),
            ("ava_runner", "legacy-runner-password"),
        ):
            with pytest.raises(psycopg.OperationalError):
                cluster.login(role, password)
        with cluster.connect_class("runner") as runner:
            assert runner.execute("SELECT count(*) FROM agents").fetchone() == (0,)
    finally:
        legacy_owner.close()
        legacy_runner.close()


def test_retire_refuses_a_superuser_owner(authority_unborn: AuthorityCluster) -> None:
    cluster = authority_unborn
    with cluster.admin() as conn, pytest.raises(CatalogRefusedError, match="superuser"):
        retire_legacy_logins(conn, owner="ava", groups=cluster.groups, authority=CutoverAuthority())
