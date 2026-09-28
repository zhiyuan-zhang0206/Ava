"""Checkpoint schema verification belongs to the start migration phase.

Only fresh install may call ``PostgresSaver.setup()``. Existing clusters move
checkpoint schema through paired Ava migrations, so the existing rollback path
can reverse them. Every capability verifies the full upstream applied set after
Ava migrations; a dependency bump without that explicit bridge fails before
any database work.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest


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
    """The admin socket as `owner_session` sees it: the startup role is honored."""

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
    import shared.db
    import shared.migrations
    from shared import cluster, pg_admin
    from shared.cluster import ownership

    calls: list[str] = []
    conn = object()
    admin_conn = _FakeAdminConnection("ava")
    authority = pg_admin.OwnerAuthority(
        admin_url="postgresql://admin@/postgres?host=/tmp/ava-pg-test&port=5999",
        database="ava",
        owner="ava",
        data_dir=Path("/home/pg"),
    )

    @contextmanager
    def fake_connect(**kwargs: object) -> Generator[object, None, None]:
        calls.append(f"connect:{kwargs}")
        yield conn

    def fake_admin_connect(url: str, **kwargs: object) -> _FakeAdminConnection:
        assert url == authority.conninfo
        calls.append(f"admin:{kwargs}")
        return admin_conn

    def fake_dependency_gate() -> None:
        calls.append("dependency")

    def fake_ava_migrations(got: object) -> list[str]:
        assert got in (conn, admin_conn)
        calls.append("ava")
        return ["20260823T000000_example"]

    def fake_checkpoint_assertion(url: str, **kwargs: object) -> None:
        calls.append(f"checkpoint:{url}:{kwargs}")

    def record_ownership(_conn: object, data: Path) -> None:
        assert data == authority.data_dir
        calls.append("ownership")

    monkeypatch.setattr(ownership, "require_postgres_connection", record_ownership)
    monkeypatch.setattr(shared.db, "connect", fake_connect)
    monkeypatch.setattr(shared.db, "direct_db_url", lambda: "postgresql://direct/ava")
    monkeypatch.setattr(pg_admin, "local_owner_authority", lambda: authority)
    monkeypatch.setattr(pg_admin.psycopg, "connect", fake_admin_connect)
    monkeypatch.setattr(shared.migrations, "apply_pending_migrations", fake_ava_migrations)
    monkeypatch.setattr(
        cluster, "assert_checkpoint_dependency_pinned", fake_dependency_gate, raising=False
    )
    monkeypatch.setattr(
        cluster, "assert_checkpoint_schema_current", fake_checkpoint_assertion, raising=False
    )
    return calls


def test_start_phase_verifies_checkpoint_schema_after_ava_migrations(
    migration_phase: list[str],
) -> None:
    """A locally owned plane migrates as the admin acting as the owner, then
    every capability shares the read-only post-migration checkpoint gate."""
    from cli.commands.migrations import cmd_migrations_apply
    from shared import pg_admin

    authority = pg_admin.local_owner_authority()
    applied = cmd_migrations_apply()

    assert migration_phase == [
        "dependency",
        "admin:{}",
        "ownership",
        "ava",
        f"checkpoint:{authority.conninfo}:{{'expected_data_dir': {authority.data_dir!r}}}",
    ]
    assert applied == ["20260823T000000_example"]


def test_foreign_connected_postgres_refuses_before_migration_ddl(
    migration_phase: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.commands.migrations import cmd_migrations_apply
    from shared.cluster import ownership

    def refuse(_conn: object, _data: Path) -> None:
        raise RuntimeError("foreign connected backend")

    monkeypatch.setattr(ownership, "require_postgres_connection", refuse)
    with pytest.raises(RuntimeError, match="foreign connected backend"):
        cmd_migrations_apply()
    assert "ava" not in migration_phase


def test_admin_session_without_owner_role_refuses_before_migration_ddl(
    migration_phase: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dial that dropped the startup role (a pooler) would create
    superuser-owned objects; the owner check refuses before any DDL."""
    from cli.commands.migrations import cmd_migrations_apply
    from shared import pg_admin

    def superuser_dial(_url: str, **_kwargs: object) -> _FakeAdminConnection:
        return _FakeAdminConnection("postgres")

    monkeypatch.setattr(pg_admin.psycopg, "connect", superuser_dial)
    with pytest.raises(RuntimeError, match="did not assume schema owner"):
        cmd_migrations_apply()
    assert "ava" not in migration_phase


def test_remote_managed_migration_preserves_explicit_provider_authority(
    migration_phase: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.commands.migrations import cmd_migrations_apply
    from shared import pg_admin
    from shared.config import settings

    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://owner@db.example/ava")
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://cache.example/0")
    monkeypatch.setattr(
        pg_admin, "local_owner_authority", lambda: pytest.fail("remote plane has no admin")
    )
    assert cmd_migrations_apply() == ["20260823T000000_example"]
    assert migration_phase == [
        "dependency",
        "connect:{'direct': True, 'unbounded': True}",
        "ava",
        "checkpoint:postgresql://direct/ava:{}",
    ]


def _bind_private_database(conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the owner authority at the session database's own postmaster.

    The session Postgres is a throwaway without a home launch receipt, so the
    native custody proof is replaced by a same-instance check: the admin dial
    must reach the data directory the authority names.
    """
    from shared import pg_admin
    from shared.cluster import ownership
    from shared.config import settings

    row = conn.execute(
        "SELECT current_setting('data_directory'), current_database(), current_user"
    ).fetchone()
    assert row is not None
    directory, database, owner = Path(row[0]), row[1], row[2]

    def guard(actual: psycopg.Connection, data: Path) -> None:
        reached = actual.execute("SELECT current_setting('data_directory')").fetchone()
        assert data == directory and reached == (str(directory),)

    authority = pg_admin.OwnerAuthority(
        admin_url=settings.data_plane.db_url, database=database, owner=owner, data_dir=directory
    )
    monkeypatch.setattr(ownership, "require_postgres_connection", guard)
    monkeypatch.setattr(pg_admin, "local_owner_authority", lambda: authority)


def test_real_start_phase_converges_ava_then_is_idempotent(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG proves both migration domains are exact on repeated starts."""
    from cli.commands.migrations import cmd_migrations_apply
    from shared.migrations import required_migration_set

    _bind_private_database(db_conn, monkeypatch)
    db_conn.execute("DELETE FROM machine_units")
    _seed_checkpoint_versions(db_conn)

    cmd_migrations_apply()

    ava_rows = db_conn.execute("SELECT name FROM schema_migrations").fetchall()
    assert {row[0] for row in ava_rows} == required_migration_set()
    checkpoint_rows = db_conn.execute("SELECT v FROM checkpoint_migrations").fetchall()
    assert {row[0] for row in checkpoint_rows} == set(range(10))
    assert cmd_migrations_apply() == []


def test_dependency_drift_fails_before_any_database_change(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unmirrored upstream v10 cannot strand update recovery on new schema.

    When a real v10 arrives, this red gate may only move after a paired Ava
    migration exists and real-PG tests cover BOTH existing-v9 update/down and
    fresh-v10-birth first-start registration/down. The mirrored up must be
    idempotent against schema + checkpoint_migrations effects already created
    by fresh-install setup while still recording its Ava migration name.
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    from cli.commands.migrations import cmd_migrations_apply
    from shared.cluster.provision import CheckpointDependencyDriftError

    _bind_private_database(db_conn, monkeypatch)
    db_conn.execute("DELETE FROM machine_units")
    _seed_checkpoint_versions(db_conn)
    cmd_migrations_apply()
    ava_before = db_conn.execute("SELECT name FROM schema_migrations").fetchall()
    checkpoint_before = db_conn.execute("SELECT v FROM checkpoint_migrations").fetchall()

    monkeypatch.setattr(PostgresSaver, "MIGRATIONS", [*PostgresSaver.MIGRATIONS, "SELECT 1"])

    with pytest.raises(CheckpointDependencyDriftError, match="paired Ava timestamp migration"):
        cmd_migrations_apply()

    assert db_conn.execute("SELECT name FROM schema_migrations").fetchall() == ava_before
    assert db_conn.execute("SELECT v FROM checkpoint_migrations").fetchall() == checkpoint_before


def test_checkpoint_schema_upstream_baseline_is_frozen() -> None:
    """Future versions extend the paired-migration manifest, never baseline."""
    from shared.cluster.provision import (
        CHECKPOINT_SCHEMA_AVA_MIGRATIONS,
        CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION,
    )

    assert CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION == 9
    assert CHECKPOINT_SCHEMA_AVA_MIGRATIONS == {}


def test_checkpoint_migration_manifest_must_be_contiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.cluster import provision

    monkeypatch.setattr(provision, "CHECKPOINT_SCHEMA_AVA_MIGRATIONS", {11: "future"})

    with pytest.raises(provision.CheckpointDependencyDriftError, match="contiguous"):
        provision.assert_checkpoint_dependency_pinned()


def test_checkpoint_migration_manifest_follows_upstream_version_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ava applies filenames forward/down in reverse, so mapping order is semantic."""
    from shared.cluster import provision

    monkeypatch.setattr(
        provision,
        "CHECKPOINT_SCHEMA_AVA_MIGRATIONS",
        {
            10: "20990102T000000_checkpoint-v10",
            11: "20990101T000000_checkpoint-v11",
        },
    )

    with pytest.raises(provision.CheckpointDependencyDriftError, match="version order"):
        provision.assert_checkpoint_dependency_pinned()


@pytest.mark.parametrize(
    ("tracked", "write_up", "write_down"),
    [(False, True, True), (True, False, False), (True, True, False)],
    ids=["untracked", "missing-up", "missing-down"],
)
def test_checkpoint_migration_manifest_requires_tracked_up_and_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tracked: bool,
    write_up: bool,
    write_down: bool,
) -> None:
    from shared import migrations
    from shared.cluster import provision

    name = "20990101T000000_checkpoint-v10"
    if write_up:
        (tmp_path / f"{name}.sql").write_text("SELECT 1;\n")
    if write_down:
        (tmp_path / f"{name}.down.sql").write_text("SELECT 1;\n")
    monkeypatch.setattr(provision, "CHECKPOINT_SCHEMA_AVA_MIGRATIONS", {10: name})
    monkeypatch.setattr(migrations, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setattr(migrations, "required_migration_set", lambda: {name} if tracked else set())

    with pytest.raises(provision.CheckpointDependencyDriftError, match="git-tracked paired"):
        provision.assert_checkpoint_dependency_pinned()
