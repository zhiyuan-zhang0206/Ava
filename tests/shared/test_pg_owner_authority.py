"""The admin-as-owner authority on a real home-owned Postgres.

Every schema-creating path (baseline, checkpoint setup, migrations, the
pgvector memory table) dials the home's owner-only socket as the OS-user
administrator acting as the schema owner. These tests prove, against a real
per-cluster instance with native custody:

- objects come out owner-owned, never admin-owned;
- the result is privilege-identical to the pre-authority procedure that logged
  in as the owner (the application roles' surface does not move);
- re-running provisioning is a no-op;
- the owner can lose LOGIN and migrations, provisioning, `pg_dump` and a
  restore of that dump keep working — the precondition for demoting it.
"""

from __future__ import annotations

import getpass
import os
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from cli.commands import _cluster_instance as ci
from cli.commands._data_plane import prepare_memory_vectors
from cli.commands.migrations import cmd_migrations_apply
from services.memory_indexer.backends.pgvector import prepare_table
from services.memory_indexer.embeddings.factory import get_provider
from shared import cluster
from shared.cluster import (
    ensure_checkpoint_schema,
    ensure_pgvector_extension,
    ensure_runner_role,
    provision_database,
)
from shared.config import settings
from shared.migrations import apply_pending_migrations, required_migration_set
from shared.pg_admin import OwnerAuthority, local_owner_authority, owner_conninfo
from shared.pg_tools import pg_tool

_OWNER_PASSWORD = "owner-login-fixture"  # noqa: S105 — test fixture, not a real credential
_RUNNER_PASSWORD = "runner-login-fixture"  # noqa: S105 — test fixture, not a real credential

# Every catalog fact an application role's privileges depend on. A NULL ACL
# reads as its built-in default, so an explicit owner-only ACL and an untouched
# one compare equal (pg_dump restores the former as the latter).
_SNAPSHOT: dict[str, sql.SQL] = {
    "relations": sql.SQL(
        "SELECT c.relkind::text, c.relname::text, pg_get_userbyid(c.relowner)::text,"
        " coalesce(c.relacl, acldefault((CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END)::\"char\","
        " c.relowner))::text FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'"
        " ORDER BY 1, 2"
    ),
    "functions": sql.SQL(
        "SELECT p.oid::regprocedure::text, pg_get_userbyid(p.proowner)::text,"
        " coalesce(p.proacl, acldefault('f', p.proowner))::text, p.prosecdef::text"
        " FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'"
        " ORDER BY 1"
    ),
    "types": sql.SQL(
        "SELECT t.typname::text, pg_get_userbyid(t.typowner)::text,"
        " coalesce(t.typacl, acldefault('T', t.typowner))::text"
        " FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
        " WHERE n.nspname = 'public' ORDER BY 1"
    ),
    "default_privileges": sql.SQL(
        "SELECT pg_get_userbyid(d.defaclrole)::text, d.defaclobjtype::text,"
        " coalesce(d.defaclnamespace::regnamespace::text, ''), d.defaclacl::text"
        " FROM pg_default_acl d ORDER BY 1, 2, 3"
    ),
    "schema": sql.SQL(
        "SELECT pg_get_userbyid(nspowner)::text, coalesce(nspacl, acldefault('n', nspowner))::text"
        " FROM pg_namespace WHERE nspname = 'public'"
    ),
    "database": sql.SQL(
        "SELECT pg_get_userbyid(datdba)::text, coalesce(datacl, acldefault('d', datdba))::text"
        " FROM pg_database WHERE datname = current_database()"
    ),
    "runner_effective": sql.SQL(
        "SELECT c.relname::text, p.privilege FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',"
        " 'REFERENCES', 'TRIGGER']) AS p(privilege)"
        " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')"
        " AND has_table_privilege('ava_runner', c.oid, p.privilege) ORDER BY 1, 2"
    ),
}

# Public objects that are not extension members and not owned by `owner`.
_FOREIGN_OWNED = sql.SQL(
    "SELECT kind, name, owner FROM ("
    " SELECT 'relation' AS kind, c.relname::text AS name,"
    "  pg_get_userbyid(c.relowner)::text AS owner, 'pg_class'::regclass AS cls, c.oid"
    "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'"
    " UNION ALL"
    " SELECT 'function', p.oid::regprocedure::text, pg_get_userbyid(p.proowner)::text,"
    "  'pg_proc'::regclass, p.oid"
    "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'"
    " UNION ALL"
    " SELECT 'type', t.typname::text, pg_get_userbyid(t.typowner)::text, 'pg_type'::regclass,"
    "  t.oid FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
    "  WHERE n.nspname = 'public'"
    ") o WHERE owner <> %s AND NOT EXISTS ("
    " SELECT 1 FROM pg_depend d WHERE d.classid = o.cls AND d.objid = o.oid"
    " AND d.deptype = 'e') ORDER BY 1, 2"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def owned_pg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """A home-owned Postgres (native launch receipt, owner-only socket) whose
    registry record `local_owner_authority` resolves; yields its port."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(settings.general, "ava_home", str(home))
    monkeypatch.setattr(settings.general, "cluster_registry", str(tmp_path / "clusters.json"))
    pg_port = _free_port()
    record = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", {"postgres": pg_port}),
        gateway_home=str(home),
        created_at="test",
    )

    def _record(_home: Path) -> cluster.ClusterRecord:
        return record

    monkeypatch.setattr(cluster, "get_record", _record)
    try:
        assert ci._start_pg(pg_port, "") == 0
        yield pg_port
    finally:
        # Re-pin the temp home: teardown must never resolve the operator's home.
        monkeypatch.setattr(settings.general, "ava_home", str(home))
        subprocess.run(  # noqa: S603 — private test-owned postgres
            [ci._pg_bin("pg_ctl"), "-D", str(home / "pg"), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )


def _data_dir() -> Path:
    return Path(settings.general.ava_home) / "pg"


def _admin_on(pg_port: int, database: str) -> str:
    return make_conninfo(ci.pg_admin_url(pg_port), dbname=database)


def _bind_cluster_url(monkeypatch: pytest.MonkeyPatch, pg_port: int, identity: str) -> None:
    """The cluster URL names the owner and database as data, as a born `.env` does."""
    monkeypatch.setattr(
        settings.data_plane, "db_url", f"postgresql://{identity}@127.0.0.1:{pg_port}/{identity}"
    )


def _vector_available(pg_port: int) -> bool:
    with psycopg.connect(_admin_on(pg_port, "postgres")) as conn:
        row = conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        return row.fetchone() is not None


def _snapshot(conninfo: str, owner: str) -> dict[str, list[tuple[str, ...]]]:
    """Every privilege-bearing catalog fact, with the owner's name normalized."""
    with psycopg.connect(conninfo) as conn:
        return {
            name: [
                tuple(str(value).replace(owner, "<owner>") for value in row)
                for row in conn.execute(query).fetchall()
            ]
            for name, query in _SNAPSHOT.items()
        }


def _foreign_owned(conninfo: str, owner: str) -> list[tuple[str, str, str]]:
    with psycopg.connect(conninfo) as conn:
        return [(str(a), str(b), str(c)) for a, b, c in conn.execute(_FOREIGN_OWNED, (owner,))]


def _prepared_state(conninfo: str) -> tuple[set[str], bool]:
    """(applied migration names, whether the pgvector memory table exists)."""
    with psycopg.connect(conninfo) as conn:
        applied = {str(row[0]) for row in conn.execute("SELECT name FROM schema_migrations")}
        memory = conn.execute("SELECT to_regclass('public.memory_embeddings')").fetchone()
    return applied, memory == ("memory_embeddings",)


def _provision_by_owner_login(pg_port: int, identity: str, dim: int) -> None:
    """The procedure before the admin authority: DDL by logging in as the owner.

    Role, database, extension and runner grants were already admin work; the
    schema baseline, checkpoint setup, migrations and the indexer's memory
    table ran over the owner's own login.
    """
    admin = ci.pg_admin_url(pg_port)
    cluster.ensure_cluster_role(identity, base_admin_url=admin, db_admin_password=_OWNER_PASSWORD)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(identity), sql.Identifier(identity)
            )
        )
    login = f"postgresql://{identity}:{_OWNER_PASSWORD}@127.0.0.1:{pg_port}/{identity}"
    schema = (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text()
    with psycopg.connect(login, autocommit=True) as conn:
        conn.execute(schema)  # type: ignore[arg-type]
    ensure_pgvector_extension(identity, base_admin_url=admin)
    with PostgresSaver.from_conn_string(login) as saver:
        saver.setup()
    with psycopg.connect(login) as conn:
        apply_pending_migrations(conn)
    ensure_runner_role(identity, base_admin_url=admin, runner_password=_RUNNER_PASSWORD)
    with psycopg.connect(login) as conn:
        prepare_table(conn, dim)


def _provision_by_authority(pg_port: int, identity: str) -> bool:
    """Today's start order through the admin authority; returns database creation."""
    admin = ci.pg_admin_url(pg_port)
    created = provision_database(
        identity,
        base_admin_url=admin,
        db_admin_password=_OWNER_PASSWORD,
        expected_data_dir=_data_dir(),
    )
    _prepare_by_authority(pg_port, identity, database_created=created)
    return created


def _prepare_by_authority(pg_port: int, identity: str, *, database_created: bool) -> None:
    admin = ci.pg_admin_url(pg_port)
    ensure_pgvector_extension(identity, base_admin_url=admin, expected_data_dir=_data_dir())
    ensure_checkpoint_schema(
        identity,
        base_admin_url=admin,
        database_created=database_created,
        expected_data_dir=_data_dir(),
    )
    cmd_migrations_apply()
    ensure_pgvector_extension(identity, base_admin_url=admin, expected_data_dir=_data_dir())
    prepare_memory_vectors()
    ensure_runner_role(
        identity,
        base_admin_url=admin,
        runner_password=_RUNNER_PASSWORD,
        expected_data_dir=_data_dir(),
    )


def test_authority_matches_owner_login_privileges_and_is_idempotent(
    owned_pg: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same catalog, same application privileges, only the dial changed."""
    monkeypatch.setattr(settings.services, "memory_search_backend", "pgvector")
    legacy, current = "ava_lgcy", "ava_auth"
    _provision_by_owner_login(owned_pg, legacy, get_provider().dim)
    _bind_cluster_url(monkeypatch, owned_pg, current)

    assert _provision_by_authority(owned_pg, current) is True

    after_birth = _snapshot(_admin_on(owned_pg, current), current)
    assert after_birth == _snapshot(_admin_on(owned_pg, legacy), legacy)
    assert _foreign_owned(_admin_on(owned_pg, current), current) == []
    assert _prepared_state(_admin_on(owned_pg, current)) == (
        required_migration_set(),
        _vector_available(owned_pg),
    )

    # A second start over the same home changes nothing.
    assert _provision_by_authority(owned_pg, current) is False
    assert cmd_migrations_apply() == []
    assert _snapshot(_admin_on(owned_pg, current), current) == after_birth


def test_owner_without_login_keeps_every_admin_path(
    owned_pg: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Slice-4 precondition: a NOLOGIN owner still migrates, provisions and dumps."""
    monkeypatch.setattr(settings.services, "memory_search_backend", "pgvector")
    identity = "ava_nolg"
    _bind_cluster_url(monkeypatch, owned_pg, identity)
    admin = ci.pg_admin_url(owned_pg)
    created = provision_database(
        identity,
        base_admin_url=admin,
        db_admin_password=_OWNER_PASSWORD,
        expected_data_dir=_data_dir(),
    )
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(identity)))
    with pytest.raises(psycopg.OperationalError, match="not permitted to log in"):
        psycopg.connect(
            f"postgresql://{identity}:{_OWNER_PASSWORD}@127.0.0.1:{owned_pg}/{identity}"
        )

    _prepare_by_authority(owned_pg, identity, database_created=created)

    authority = local_owner_authority()
    assert authority == OwnerAuthority(
        admin_url=admin, database=identity, owner=identity, data_dir=_data_dir()
    )
    with authority.session() as conn:
        facts = conn.execute(
            "SELECT session_user, current_user, current_setting('statement_timeout')"
        ).fetchone()
    assert facts == (getpass.getuser(), identity, "0")
    assert _foreign_owned(authority.conninfo, identity) == []
    source = _snapshot(_admin_on(owned_pg, identity), identity)

    # pg_dump acts as the owner with no password in argv or the environment.
    dump = tmp_path / "owner.dump"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    subprocess.run(  # noqa: S603 — fixed tool path, private test database
        [
            str(pg_tool("pg_dump")),
            "--format=custom",
            "--file",
            str(dump),
            "--dbname",
            authority.conninfo,
        ],
        check=True,
        capture_output=True,
        env=environment,
    )
    restored = f"{identity}_rst"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(restored), sql.Identifier(identity)
            )
        )
    subprocess.run(  # noqa: S603 — fixed tool path, private test database
        [
            str(pg_tool("pg_restore")),
            "--clean",
            "--if-exists",
            "--exit-on-error",
            "--dbname",
            _admin_on(owned_pg, restored),
            str(dump),
        ],
        check=True,
        capture_output=True,
        env=environment,
    )
    assert _foreign_owned(_admin_on(owned_pg, restored), identity) == []
    copy = _snapshot(_admin_on(owned_pg, restored), identity)
    assert {name: rows for name, rows in copy.items() if name != "database"} == {
        name: rows for name, rows in source.items() if name != "database"
    }


def test_owner_conninfo_refuses_what_startup_options_cannot_carry() -> None:
    """The owner travels as `-c role=<owner>`; anything else is refused."""
    admin = "postgresql://admin@/postgres?host=/sockets/ava-pg-x&port=5433"
    with pytest.raises(ValueError, match="plain role identifier"):
        owner_conninfo(admin, database="ava", owner="ava owner")
    with pytest.raises(ValueError, match="startup options"):
        owner_conninfo(f"{admin}&options=-c%20role%3Dother", database="ava", owner="ava")
    assert conninfo_to_dict(owner_conninfo(admin, database="ava_main", owner="ava_main")) == {
        "user": "admin",
        "dbname": "ava_main",
        "host": "/sockets/ava-pg-x",
        "port": "5433",
        "options": "-c role=ava_main",
    }


def test_local_owner_authority_reads_record_and_url_as_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Socket port from the registry record; owner and database from the URL."""
    home = tmp_path / "home"
    monkeypatch.setattr(settings.general, "ava_home", str(home))
    record = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", {"postgres": 5999, "pgbouncer": 6999}),
        gateway_home=str(home),
        created_at="test",
    )

    def _record(_home: Path) -> cluster.ClusterRecord:
        return record

    monkeypatch.setattr(cluster, "get_record", _record)
    monkeypatch.setattr(
        settings.data_plane, "db_url", "postgresql://ava_main:pw@127.0.0.1:6999/ava_db"
    )

    authority = local_owner_authority()

    assert (authority.database, authority.owner) == ("ava_db", "ava_main")
    assert authority.data_dir == home / "pg"
    assert conninfo_to_dict(authority.admin_url)["port"] == "5999"
    assert "pw" not in authority.conninfo


def test_remote_plane_has_no_local_owner_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://owner@db.example/ava")
    with pytest.raises(RuntimeError, match="remote-managed"):
        local_owner_authority()
