"""Group grants and the fail-closed invariant on real PostgreSQL 17, the
runner-matrix parity with legacy provisioning, MAINTAIN/VACUUM, and cutover."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from shared.cluster import ensure_runner_role
from shared.cluster.authority import (
    CatalogRefusedError,
    CutoverAuthority,
    VacuumSkippedError,
    activate,
    apply_group_grants,
    check_invariant,
    create_ledger,
    ensure_groups,
    mint_generation,
    prove_closure,
    retire_legacy_logins,
    vacuum_or_fail,
)
from shared.cluster.authority.model import Groups
from shared.pg_tools import throwaway_postgres
from tests.lifecycle.db_authority.conftest import OWNER, AuthorityCluster

_LEGACY_RUNNER_PW = "legacy-runner-fixture"

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


_ACL_SNAPSHOT = """
SELECT 'relation ' || c.oid::regclass::text, a.privilege_type
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a
WHERE n.nspname = 'public' AND a.grantee = %(role)s::regrole
UNION ALL
SELECT 'column ' || c.oid::regclass::text || '.' || att.attname, a.privilege_type
FROM pg_attribute att JOIN pg_class c ON c.oid = att.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(att.attacl) a
WHERE n.nspname = 'public' AND a.grantee = %(role)s::regrole
UNION ALL
SELECT 'routine ' || p.oid::regprocedure::text, a.privilege_type
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a
WHERE n.nspname = 'public' AND a.grantee = %(role)s::regrole
UNION ALL
SELECT 'schema public', a.privilege_type
FROM pg_namespace n, aclexplode(n.nspacl) a
WHERE n.nspname = 'public' AND a.grantee = %(role)s::regrole
UNION ALL
SELECT 'database', a.privilege_type
FROM pg_database d, aclexplode(d.datacl) a
WHERE d.datname = current_database() AND a.grantee = %(role)s::regrole
UNION ALL
SELECT 'default ' || d.defaclobjtype::text, a.privilege_type
FROM pg_default_acl d, aclexplode(d.defaclacl) a
WHERE a.grantee = %(role)s::regrole
"""


def _acl(conn: psycopg.Connection[Any], role: str) -> set[tuple[str, str]]:
    return {(row[0], row[1]) for row in conn.execute(_ACL_SNAPSHOT, {"role": role})}


def test_runner_group_matrix_equals_legacy_runner_provisioning() -> None:
    """Until the start wiring retires ``ensure_runner_role``, both definitions of
    the runner matrix must grant the same surface.

    The only intended differences: the group drops the publication-admission
    EXECUTE (deleted with the publication graph) and gains CONNECT and schema
    USAGE (PUBLIC loses CONNECT).
    """
    schema = (Path(__file__).resolve().parents[3] / "db" / "schema.sql").read_text()
    with throwaway_postgres(schema_sql=schema) as url:
        admin = url.rsplit("/", 1)[0] + "/postgres"
        ensure_runner_role("ava_citest", base_admin_url=admin, runner_password=_LEGACY_RUNNER_PW)
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE ROLE ava_runner_parity NOLOGIN")
            conn.execute("CREATE ROLE ava_gateway_parity NOLOGIN")
            apply_group_grants(
                conn,
                owner="ava_citest",
                database="ava_citest",
                groups=Groups(gateway="ava_gateway_parity", runner="ava_runner_parity"),
            )
            legacy, group = _acl(conn, "ava_runner"), _acl(conn, "ava_runner_parity")
    assert legacy - group == {
        ("routine lock_runtime_publication_admission()", "EXECUTE"),
    }
    assert group - legacy == {("schema public", "USAGE"), ("database", "CONNECT")}
