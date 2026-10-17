from __future__ import annotations

import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

import psycopg
import pytest

from base.db.tests.fakes import patch_database
from base.deploy.schema import migration_layout
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database


def _seed_checkpoint_versions(conn: psycopg.Connection) -> None:
    conn.execute(
        "INSERT INTO checkpoint_migrations (v) SELECT generate_series(0, 9) ON CONFLICT DO NOTHING"
    )
    conn.commit()


class _FakeCursor:
    def __init__(self, owner: str) -> None:
        self._owner = owner

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: str) -> _FakeCursor:
        assert query == "SELECT current_user"
        return self

    def fetchone(self) -> tuple[str]:
        return (self._owner,)


class _FakeAdminConnection:
    autocommit = False

    def __init__(self, owner: str) -> None:
        self._owner = owner

    def __enter__(self) -> _FakeAdminConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self, **_kwargs: object) -> _FakeCursor:
        return _FakeCursor(self._owner)

    def commit(self) -> None:
        return None


@pytest.fixture
def migration_phase(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import base.deploy.schema.migrations
    from base import cluster
    from base.cluster.dataplane import pg_admin

    calls: list[str] = []
    owner = threading.current_thread()
    conn = object()
    admin_conn = _FakeAdminConnection("meiva")
    authority = pg_admin.OwnerAuthority(
        admin_url="postgresql://admin@/postgres?host=/tmp/meiva-pg-test&port=5999",
        database="meiva",
        owner="meiva",
        data_dir=Path("/home/pg"),
    )

    @contextmanager
    def fake_connect(**kwargs: object) -> Generator[object, None, None]:
        calls.append(f"connect:{kwargs}")
        yield conn

    def fake_admin_connect(url: str, **kwargs: object) -> _FakeAdminConnection:
        if threading.current_thread() is not owner:
            return _FakeAdminConnection("meiva")
        assert url == authority.conninfo
        calls.append(f"admin:{kwargs}")
        return admin_conn

    def fake_dependency_gate(tracked_migrations: object) -> None:
        assert isinstance(tracked_migrations, set)
        calls.append("dependency")

    def fake_meiva_migrations(got: object, *, machine_name: Callable[[], str]) -> list[str]:
        assert callable(machine_name)
        assert got in (conn, admin_conn)
        calls.append("meiva")
        return ["20260823T000000_example"]

    def fake_checkpoint_assertion(url: str, **kwargs: object) -> None:
        calls.append(f"checkpoint:{url}:{kwargs}")

    def record_ownership(_conn: object, data: Path, _socket_dir: Path) -> None:
        assert data == authority.data_dir
        calls.append("ownership")

    monkeypatch.setattr(pg_admin, "require_postgres_connection", record_ownership)
    patch_database(monkeypatch, connect=fake_connect)

    def owner_authority(_db_url: str, *, remote_plane: bool) -> pg_admin.OwnerAuthority:
        assert not remote_plane
        return authority

    monkeypatch.setattr(pg_admin, "local_owner_authority", owner_authority)
    monkeypatch.setattr(pg_admin.psycopg, "connect", fake_admin_connect)
    monkeypatch.setattr(
        base.deploy.schema.migrations, "apply_pending_migrations", fake_meiva_migrations
    )
    monkeypatch.setattr(
        cluster, "assert_checkpoint_dependency_pinned", fake_dependency_gate, raising=False
    )
    monkeypatch.setattr(
        cluster, "assert_checkpoint_schema_current", fake_checkpoint_assertion, raising=False
    )
    return calls


def test_start_phase_verifies_checkpoint_schema_after_meiva_migrations(
    migration_phase: list[str],
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    from base.cluster.dataplane import pg_admin
    from cli.commands.lifecycle.migrations import cmd_migrations_apply

    authority = pg_admin.local_owner_authority(
        "postgresql://meiva@localhost/meiva", remote_plane=False
    )
    applied = cmd_migrations_apply(
        database_factory=operator_database, machine_name=command_runtime.machine.name
    )

    assert migration_phase == [
        "dependency",
        "admin:{}",
        "ownership",
        "meiva",
        f"checkpoint:{authority.conninfo}:{{'expected_data_dir': {authority.data_dir!r}}}",
    ]
    assert applied == ["20260823T000000_example"]


def test_foreign_connected_postgres_refuses_before_migration_ddl(
    migration_phase: list[str],
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    from base.cluster.dataplane import pg_admin
    from cli.commands.lifecycle.migrations import cmd_migrations_apply

    def refuse(_conn: object, _data: Path, _socket_dir: Path) -> None:
        raise RuntimeError("foreign connected backend")

    monkeypatch.setattr(pg_admin, "require_postgres_connection", refuse)
    with pytest.raises(RuntimeError, match="foreign connected backend"):
        cmd_migrations_apply(
            database_factory=operator_database, machine_name=command_runtime.machine.name
        )
    assert "meiva" not in migration_phase


def test_admin_session_without_owner_role_refuses_before_migration_ddl(
    migration_phase: list[str],
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    from base.cluster.dataplane import pg_admin
    from cli.commands.lifecycle.migrations import cmd_migrations_apply

    owner = threading.current_thread()

    def superuser_dial(_url: str, **_kwargs: object) -> _FakeAdminConnection:
        if threading.current_thread() is not owner:
            return _FakeAdminConnection("meiva")
        return _FakeAdminConnection("postgres")

    monkeypatch.setattr(pg_admin.psycopg, "connect", superuser_dial)
    with pytest.raises(RuntimeError, match="did not assume schema owner"):
        cmd_migrations_apply(
            database_factory=operator_database, machine_name=command_runtime.machine.name
        )
    assert "meiva" not in migration_phase


def test_remote_managed_migration_preserves_explicit_provider_authority(
    migration_phase: list[str],
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    from base.cluster.dataplane import pg_admin
    from base.config import settings
    from cli.commands.lifecycle.migrations import cmd_migrations_apply

    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://owner@db.example/meiva")
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://cache.example/0")

    def refuse_local_owner(_db_url: str, *, remote_plane: bool) -> NoReturn:
        pytest.fail(f"remote plane has no local authority (remote={remote_plane})")

    monkeypatch.setattr(pg_admin, "local_owner_authority", refuse_local_owner)
    assert cmd_migrations_apply(
        database_factory=operator_database, machine_name=command_runtime.machine.name
    ) == ["20260823T000000_example"]
    assert migration_phase == [
        "dependency",
        "connect:{'unbounded': True}",
        "meiva",
        "checkpoint:postgresql://owner@db.example/meiva:{}",
    ]


def _bind_private_database(conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from base.cluster.dataplane import pg_admin
    from base.config import settings

    row = conn.execute(
        "SELECT current_setting('data_directory'), current_database(), current_user"
    ).fetchone()
    assert row is not None
    directory, database, owner = Path(row[0]), row[1], row[2]

    def guard(actual: psycopg.Connection, data: Path, _socket_dir: Path) -> None:
        reached = actual.execute("SELECT current_setting('data_directory')").fetchone()
        assert data == directory and reached == (str(directory),)

    authority = pg_admin.OwnerAuthority(
        admin_url=settings.data_plane.db_url, database=database, owner=owner, data_dir=directory
    )
    monkeypatch.setattr(pg_admin, "require_postgres_connection", guard)

    def owner_authority(_db_url: str, *, remote_plane: bool) -> pg_admin.OwnerAuthority:
        assert not remote_plane
        return authority

    monkeypatch.setattr(pg_admin, "local_owner_authority", owner_authority)


def test_real_start_phase_converges_meiva_then_is_idempotent(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    from base.deploy.schema.migrations import required_migration_set
    from cli.commands.lifecycle.migrations import cmd_migrations_apply

    _bind_private_database(db_conn, monkeypatch)
    db_conn.execute("DELETE FROM machine_units")
    _seed_checkpoint_versions(db_conn)

    cmd_migrations_apply(
        database_factory=operator_database, machine_name=command_runtime.machine.name
    )

    meiva_rows = db_conn.execute("SELECT name FROM schema_migrations").fetchall()
    assert {row[0] for row in meiva_rows} == required_migration_set()
    checkpoint_rows = db_conn.execute("SELECT v FROM checkpoint_migrations").fetchall()
    assert {row[0] for row in checkpoint_rows} == set(range(10))
    assert (
        cmd_migrations_apply(
            database_factory=operator_database, machine_name=command_runtime.machine.name
        )
        == []
    )


def test_dependency_drift_fails_before_any_database_change(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    from base.cluster.provision import CheckpointDependencyDriftError
    from cli.commands.lifecycle.migrations import cmd_migrations_apply

    _bind_private_database(db_conn, monkeypatch)
    db_conn.execute("DELETE FROM machine_units")
    _seed_checkpoint_versions(db_conn)
    cmd_migrations_apply(
        database_factory=operator_database, machine_name=command_runtime.machine.name
    )
    meiva_before = db_conn.execute("SELECT name FROM schema_migrations").fetchall()
    checkpoint_before = db_conn.execute("SELECT v FROM checkpoint_migrations").fetchall()

    monkeypatch.setattr(PostgresSaver, "MIGRATIONS", [*PostgresSaver.MIGRATIONS, "SELECT 1"])

    with pytest.raises(CheckpointDependencyDriftError, match="Meiva timestamp migration"):
        cmd_migrations_apply(
            database_factory=operator_database, machine_name=command_runtime.machine.name
        )

    assert db_conn.execute("SELECT name FROM schema_migrations").fetchall() == meiva_before
    assert db_conn.execute("SELECT v FROM checkpoint_migrations").fetchall() == checkpoint_before


def test_checkpoint_schema_upstream_baseline_is_frozen() -> None:
    from base.cluster.provision import (
        CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS,
        CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION,
    )

    assert CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION == 9
    assert CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS == {}


def test_checkpoint_migration_manifest_must_be_contiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.cluster import provision

    monkeypatch.setattr(provision, "CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS", {11: "future"})

    with pytest.raises(provision.CheckpointDependencyDriftError, match="contiguous"):
        provision.assert_checkpoint_dependency_pinned(set())


def test_checkpoint_migration_manifest_follows_upstream_version_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.cluster import provision

    monkeypatch.setattr(
        provision,
        "CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS",
        {
            10: "20990102T000000_checkpoint-v10",
            11: "20990101T000000_checkpoint-v11",
        },
    )

    with pytest.raises(provision.CheckpointDependencyDriftError, match="version order"):
        provision.assert_checkpoint_dependency_pinned(set())


@pytest.mark.parametrize(
    ("tracked", "write_up"),
    [(False, True), (True, False)],
    ids=["untracked", "missing-up"],
)
def test_checkpoint_migration_manifest_requires_a_tracked_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tracked: bool,
    write_up: bool,
) -> None:
    from base.cluster import provision
    from base.deploy.schema.migration_layout import required_migration_set
    from base.deploy.schema.tests.migration_support import git, init_repo

    init_repo(tmp_path)
    name = "20990101T000000_checkpoint-v10"
    if write_up:
        (tmp_path / f"{name}.sql").write_text("SELECT 1;\n")
    monkeypatch.setattr(provision, "CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS", {10: name})
    monkeypatch.setattr(migration_layout, "MIGRATIONS_DIR", tmp_path)
    if tracked:
        git(tmp_path, "add", "-A")

    with pytest.raises(
        provision.CheckpointDependencyDriftError, match="git-tracked Meiva migration"
    ):
        provision.assert_checkpoint_dependency_pinned(required_migration_set())


def test_checkpoint_migration_manifest_accepts_nested_tracked_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    from base.cluster import provision
    from base.deploy.schema.migration_layout import required_migration_set
    from base.deploy.schema.tests.migration_support import init_repo

    name = "20990101T000000_checkpoint-v10"
    hour = tmp_path / "2099/01/01/00"
    hour.mkdir(parents=True)
    (hour / f"{name}.sql").write_text("SELECT 1;\n")
    init_repo(tmp_path)
    monkeypatch.setattr(provision, "CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS", {10: name})
    monkeypatch.setattr(migration_layout, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setattr(PostgresSaver, "MIGRATIONS", [*PostgresSaver.MIGRATIONS, "SELECT 1;"])

    provision.assert_checkpoint_dependency_pinned(required_migration_set())
