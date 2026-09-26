"""Real PostgreSQL 17 for the database write-generation authority.

One throwaway instance per test module (``_authority_instance``) carries a
template database built exactly as provisioning builds it: ``db/schema.sql``,
the LangGraph checkpoint tables and every migration applied by the superuser
acting as a NOLOGIN schema owner, so every object is owner-owned. The
instance then switches to the always-authenticated ``pg_hba``:

- the OS user reaches the bootstrap superuser only through ``peer`` on the
  owner-only socket (an ident map names it, since the throwaway superuser is
  ``ava`` rather than the OS user);
- every other role authenticates with SCRAM over the socket and TCP;
- one harness-only line keeps the throwaway stall watchdog's passwordless
  ``ava@127.0.0.1/postgres`` probe alive. It admits no application role.

``authority_postgres`` gives each test a fresh database copied from the
template plus a private home, born through the real authority functions
(groups, ledger, generation 0). ``authority_unborn`` is the same database
before birth. Teardown rolls back leftover prepared transactions, drops the
database and every role the test created, and resets the shared owner.
``max_prepared_transactions`` is non-zero so the closure's prepared-
transaction guard can be exercised.
"""

from __future__ import annotations

import getpass
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql

from shared.cluster.authority import (
    GATEWAY_GROUP,
    RUNNER_GROUP,
    BirthAuthority,
    GenerationSecret,
    Groups,
    activate,
    create_ledger,
    ensure_groups,
    mint_generation,
    read_secret,
    require_ledger,
)
from shared.migrations import apply_pending_migrations
from shared.pg_tools import pg_start_env, pg_tool, throwaway_postgres

OWNER = "ava_test_owner"
_TEMPLATE = "ava_test_tmpl"
_SUPERUSER = "ava"
_SCHEMA = Path(__file__).resolve().parents[3] / "db" / "schema.sql"

_HBA = """\
# TYPE  DATABASE  USER  ADDRESS       METHOD
local   all       ava                 peer map=ava_admin
host    postgres  ava   127.0.0.1/32  trust
local   all       all                 scram-sha-256
host    all       all   127.0.0.1/32  scram-sha-256
host    all       all   ::1/128       scram-sha-256
"""
_HBA_RULES = [
    ("local", ["all"], ["ava"], "peer"),
    ("host", ["postgres"], ["ava"], "trust"),
    ("local", ["all"], ["all"], "scram-sha-256"),
    ("host", ["all"], ["all"], "scram-sha-256"),
    ("host", ["all"], ["all"], "scram-sha-256"),
]


@dataclass(frozen=True)
class Instance:
    port: int
    socket_dir: str

    def admin(self, database: str) -> psycopg.Connection[Any]:
        """The bootstrap superuser over the owner-only socket (peer)."""
        return psycopg.connect(
            host=self.socket_dir, port=self.port, user=_SUPERUSER, dbname=database, autocommit=True
        )


def _restart(data_dir: Path) -> None:
    subprocess.run(  # noqa: S603 — resolved pg_ctl path + static flags
        [
            str(pg_tool("pg_ctl")),
            "-D",
            str(data_dir),
            "-l",
            str(data_dir.parent / "pg.log"),
            "-w",
            "-t",
            "60",
            "-m",
            "fast",
            "restart",
        ],
        check=True,
        capture_output=True,
        env=pg_start_env(),
    )


def _build_template(tcp: str) -> None:
    with psycopg.connect(f"{tcp}/{_TEMPLATE}", autocommit=True) as conn:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(OWNER)))
        conn.execute(_SCHEMA.read_text())  # type: ignore[arg-type]
    as_owner = f"{tcp}/{_TEMPLATE}?options=-c%20role%3D{OWNER}"
    with PostgresSaver.from_conn_string(as_owner) as saver:
        saver.setup()
    with psycopg.connect(as_owner) as conn:
        apply_pending_migrations(conn)


def _authenticate(instance: Instance, tcp: str, data_dir: Path) -> None:
    (data_dir / "pg_ident.conf").write_text(f"ava_admin {getpass.getuser()} {_SUPERUSER}\n")
    (data_dir / "pg_hba.conf").write_text(_HBA)
    with psycopg.connect(f"{tcp}/postgres", autocommit=True) as conn:
        conn.execute("SELECT pg_reload_conf()")
    with instance.admin("postgres") as conn:
        rules = conn.execute(
            "SELECT type, database, user_name, auth_method, error FROM pg_hba_file_rules"
            " ORDER BY rule_number"
        ).fetchall()
    assert [(*rule[:4],) for rule in rules] == _HBA_RULES, rules
    assert all(rule[4] is None for rule in rules), rules


@pytest.fixture(scope="module")
def _authority_instance() -> Iterator[Instance]:
    with throwaway_postgres() as url:
        port = urlsplit(url).port
        assert port is not None
        tcp = f"postgresql://{_SUPERUSER}@127.0.0.1:{port}"
        with psycopg.connect(f"{tcp}/postgres", autocommit=True) as conn:
            data_row = conn.execute("SHOW data_directory").fetchone()
            socket_row = conn.execute("SHOW unix_socket_directories").fetchone()
            assert data_row is not None and socket_row is not None
            conn.execute("ALTER SYSTEM SET max_prepared_transactions = 4")
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(OWNER)))
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(_TEMPLATE), sql.Identifier(OWNER)
                )
            )
        data_dir = Path(data_row[0])
        _restart(data_dir)
        _build_template(tcp)
        with psycopg.connect(f"{tcp}/postgres", autocommit=True) as conn:
            conn.execute("DROP DATABASE ava_citest")
            conn.execute("DROP ROLE ava_citest")
        instance = Instance(port=port, socket_dir=socket_row[0])
        _authenticate(instance, tcp, data_dir)
        yield instance


@dataclass(frozen=True)
class AuthorityCluster:
    instance: Instance
    home: Path
    database: str
    owner: str
    groups: Groups

    def admin(self) -> psycopg.Connection[Any]:
        return self.instance.admin(self.database)

    def login(
        self, role: str, password: str, *, via: Literal["tcp", "socket"] = "tcp", **kwargs: Any
    ) -> psycopg.Connection[Any]:
        host = "127.0.0.1" if via == "tcp" else self.instance.socket_dir
        return psycopg.connect(
            host=host,
            port=self.instance.port,
            user=role,
            password=password,
            dbname=self.database,
            connect_timeout=5,
            **kwargs,
        )

    def active_secret(self) -> GenerationSecret:
        ledger = require_ledger(self.home)
        assert ledger.active is not None
        return read_secret(self.home, ledger.active)

    def connect_class(
        self,
        cls: Literal["gateway", "runner"],
        *,
        via: Literal["tcp", "socket"] = "tcp",
        **kwargs: Any,
    ) -> psycopg.Connection[Any]:
        role = self.active_secret().roles.of(cls)
        return self.login(role.name, role.password, via=via, **kwargs)


def birth(cluster: AuthorityCluster) -> None:
    """First-start birth through the real library: groups, ledger, generation 0."""
    authority = BirthAuthority()
    with cluster.admin() as conn:
        ensure_groups(conn, owner=cluster.owner, database=cluster.database, groups=cluster.groups)
        create_ledger(cluster.home, owner=cluster.owner, groups=cluster.groups, authority=authority)
        verified = mint_generation(conn, cluster.home, authority)
    activate(cluster.home, authority, verified)


def _reset(instance: Instance, database: str) -> None:
    with instance.admin("postgres") as conn:
        prepared = conn.execute("SELECT gid, database FROM pg_prepared_xacts").fetchall()
        for gid, name in prepared:
            with instance.admin(name) as owner_db:
                owner_db.execute(sql.SQL("ROLLBACK PREPARED {}").format(sql.Literal(gid)))
        conn.execute(
            "SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity"
            " WHERE pid <> pg_backend_pid() AND usesysid IS NOT NULL"
            " AND usename IS DISTINCT FROM %s",
            (_SUPERUSER,),
        )
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
        )
        roles = conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' AND rolname <> ALL(%s)",
            ([_SUPERUSER, OWNER],),
        ).fetchall()
        for (role,) in roles:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL(
                "ALTER ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
                " NOBYPASSRLS PASSWORD NULL"
            ).format(sql.Identifier(OWNER))
        )


@pytest.fixture
def authority_unborn(_authority_instance: Instance, tmp_path: Path) -> Iterator[AuthorityCluster]:
    """A fresh owner-owned cluster database and private home, before birth."""
    database = f"ava_test_auth_{uuid.uuid4().hex[:12]}"
    with _authority_instance.admin("postgres") as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {} OWNER {}").format(
                sql.Identifier(database), sql.Identifier(_TEMPLATE), sql.Identifier(OWNER)
            )
        )
    home = tmp_path.resolve() / "home"
    home.mkdir()
    try:
        yield AuthorityCluster(
            instance=_authority_instance,
            home=home,
            database=database,
            owner=OWNER,
            groups=Groups(gateway=GATEWAY_GROUP, runner=RUNNER_GROUP),
        )
    finally:
        _reset(_authority_instance, database)


@pytest.fixture
def authority_postgres(authority_unborn: AuthorityCluster) -> AuthorityCluster:
    """The same cluster born through the real authority functions (generation 0)."""
    birth(authority_unborn)
    return authority_unborn
