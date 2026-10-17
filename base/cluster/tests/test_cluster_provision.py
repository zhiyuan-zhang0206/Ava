from __future__ import annotations

import os
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg import sql

from base.cluster import drop_database, provision_database
from base.cluster.dataplane.pg_admin import owner_conninfo
from base.cluster.derive import swap_db
from base.config import settings


def _admin_url() -> str:
    base, _ = settings.data_plane.db_url.rsplit("/", 1)
    return f"{base}/postgres"


def _fresh_identity() -> str:
    return f"meiva_provtest_{os.getpid()}_{int(time.time() * 1_000_000)}"


def test_provision_database_creates_db_and_applies_schema(db_conn: object) -> None:
    identity = _fresh_identity()
    expected_db = identity
    admin_url = _admin_url()
    base_url, _ = settings.data_plane.db_url.rsplit("/", 1)
    db_url = f"{base_url}/{expected_db}"

    try:
        assert provision_database(identity, base_admin_url=admin_url) is True

        with psycopg.connect(admin_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (expected_db,)
            ).fetchone()
        assert row is not None, f"database {expected_db!r} was not created"

        with psycopg.connect(db_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.tables"
                "  WHERE table_schema = 'public' AND table_name = 'agents'"
                ")"
            ).fetchone()
        assert row is not None and row[0] is True, "table 'agents' not found — schema not applied"

    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = %s AND pid <> pg_backend_pid()",
                (expected_db,),
            )
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(expected_db)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(expected_db)))


def test_drop_database_removes_db_idempotent(db_conn: object) -> None:
    identity = _fresh_identity()
    expected_db = identity
    admin_url = _admin_url()

    provision_database(identity, base_admin_url=admin_url)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        assert (
            conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (expected_db,)).fetchone()
            is not None
        )

    drop_database(identity, base_admin_url=admin_url)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        assert (
            conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (expected_db,)).fetchone()
            is None
        ), f"database {expected_db!r} still present after drop_database"

    drop_database(identity, base_admin_url=admin_url)


def test_provision_database_idempotent(db_conn: object) -> None:
    identity = _fresh_identity()
    expected_db = identity
    admin_url = _admin_url()

    try:
        assert provision_database(identity, base_admin_url=admin_url) is True
        assert provision_database(identity, base_admin_url=admin_url) is False

        with psycopg.connect(admin_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (expected_db,)
            ).fetchone()
        assert row is not None

    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = %s AND pid <> pg_backend_pid()",
                (expected_db,),
            )
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(expected_db)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(expected_db)))


def test_provisioned_role_is_nosuperuser_and_owns_db(db_conn: object) -> None:
    role = expected_db = _fresh_identity()
    admin_url = _admin_url()
    try:
        provision_database(role, base_admin_url=admin_url)
        with psycopg.connect(admin_url, autocommit=True) as conn:
            attrs = conn.execute(
                "SELECT rolsuper, rolcanlogin, rolpassword IS NULL FROM pg_authid"
                " WHERE rolname = %s",
                (role,),
            ).fetchone()
            assert attrs == (False, False, True), "role must be NOLOGIN NOSUPERUSER, no password"
            owner = conn.execute(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (expected_db,)
            ).fetchone()
            assert owner is not None and owner[0] == role, "role must own its own database"
    finally:
        _drop_db_and_role(admin_url, expected_db)


def test_role_cannot_read_another_clusters_database(db_conn: object) -> None:
    base = _fresh_identity()
    a, b = f"{base}a", f"{base}b"
    admin_url = _admin_url()
    db_a, role_b = a, b
    try:
        provision_database(a, base_admin_url=admin_url)
        provision_database(b, base_admin_url=admin_url)
        url_a_as_b = owner_conninfo(admin_url, database=db_a, owner=role_b)
        with (
            psycopg.connect(url_a_as_b, autocommit=True) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute("SELECT * FROM agents")
    finally:
        _drop_db_and_role(admin_url, a)
        _drop_db_and_role(admin_url, b)


def _drop_db_and_role(admin_url: str, name: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


class _FakePgConn:
    def __init__(self, url: str, *, first_row: tuple[object, ...] | None) -> None:
        self.url = url
        self.first_row = first_row
        self.executed: list[str] = []

    def __enter__(self) -> _FakePgConn:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> _FakePgConn:
        self.executed.append(sql)
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        row, self.first_row = self.first_row, None
        return row


def _patch_pg_connect(
    monkeypatch: pytest.MonkeyPatch, rows: list[tuple[object, ...] | None]
) -> tuple[list[str], list[_FakePgConn]]:
    urls: list[str] = []
    conns: list[_FakePgConn] = []
    owner = threading.current_thread()

    def fake_connect(url: str, **_: object) -> _FakePgConn:
        if threading.current_thread() is not owner:
            return _FakePgConn(url, first_row=None)
        urls.append(url)
        conn = _FakePgConn(url, first_row=rows.pop(0))
        conns.append(conn)
        return conn

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    return urls, conns


def test_ensure_pgvector_extension_noop_when_binaries_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.cluster.provision import ensure_pgvector_extension

    urls, conns = _patch_pg_connect(monkeypatch, [None])
    ensure_pgvector_extension("meiva_ident", base_admin_url="postgresql://admin@/postgres")
    assert urls == ["postgresql://admin@/postgres"]
    assert conns[0].executed == ["SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"]


def test_ensure_pgvector_extension_precreates_in_cluster_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.cluster.provision import ensure_pgvector_extension

    urls, conns = _patch_pg_connect(monkeypatch, [(1,), None])
    ensure_pgvector_extension(
        "meiva_ident", base_admin_url="postgresql://admin@/postgres?host=/tmp/x"
    )
    assert len(urls) == 2
    assert urlsplit(urls[1]).path == "/meiva_ident"
    assert conns[1].executed == ["CREATE EXTENSION IF NOT EXISTS vector"]


def test_ensure_pgvector_extension_noop_on_connect_failure(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    from base.cluster.provision import ensure_pgvector_extension

    owner = threading.current_thread()

    def boom(url: str, **_kw: object) -> _FakePgConn:
        if threading.current_thread() is not owner:
            return _FakePgConn(url, first_row=None)
        raise psycopg.OperationalError("connection is bad")

    monkeypatch.setattr(psycopg, "connect", boom)
    ensure_pgvector_extension("meiva_ident", base_admin_url="postgresql://admin@/postgres")
    [warning] = [r for r in loguru_records if "[pgvector] pre-create skipped" in r["message"]]
    assert "(connection is bad)" in warning["message"]


def test_interrupted_empty_database_requires_initialization_authority(db_conn: object) -> None:
    identity = _fresh_identity()
    admin_url = _admin_url()
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(identity)))
    try:
        with pytest.raises(RuntimeError, match="without initialization authority"):
            provision_database(identity, base_admin_url=admin_url)
        assert (
            provision_database(
                identity,
                base_admin_url=admin_url,
                resume_initialization=True,
            )
            is False
        )
        with psycopg.connect(swap_db(admin_url, identity)) as conn:
            assert conn.execute("SELECT to_regclass('public.agents')").fetchone() == ("agents",)
    finally:
        _drop_db_and_role(admin_url, identity)


def test_interrupted_database_with_unknown_objects_is_preserved(db_conn: object) -> None:
    identity = _fresh_identity()
    admin_url = _admin_url()
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(identity)))
    try:
        with psycopg.connect(swap_db(admin_url, identity)) as conn:
            conn.execute("CREATE TABLE preserve_me (value text)")
            conn.execute("INSERT INTO preserve_me VALUES ('user data')")
        with pytest.raises(RuntimeError, match="unknown database objects"):
            provision_database(
                identity,
                base_admin_url=admin_url,
                resume_initialization=True,
            )
        with psycopg.connect(swap_db(admin_url, identity)) as conn:
            assert conn.execute("SELECT value FROM preserve_me").fetchall() == [("user data",)]
            assert conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone() == (
                None,
            )
    finally:
        _drop_db_and_role(admin_url, identity)


def test_default_schema_check_never_setup_after_concurrent_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    from base.cluster import provision

    expected = frozenset(range(len(PostgresSaver.MIGRATIONS)))
    observed = iter((expected - {9}, expected))

    def checkpoint_versions(_db_url: str, *, expected_data_dir: object = None) -> frozenset[int]:
        return next(observed)

    def setup_must_not_run(_self: PostgresSaver) -> None:
        raise AssertionError("a successful read-only recheck must return before setup")

    monkeypatch.setattr(provision, "checkpoint_schema_versions", checkpoint_versions)
    monkeypatch.setattr(PostgresSaver, "setup", setup_must_not_run)

    provision.ensure_checkpoint_schema("meiva_repaired", base_admin_url=_admin_url())
    with pytest.raises(StopIteration):
        next(observed)
