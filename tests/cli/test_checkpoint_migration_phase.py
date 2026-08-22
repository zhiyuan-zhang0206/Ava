"""LangGraph checkpoint DDL belongs to the gateway migration phase.

Runtime checkpoint readers can run under ``ava_runner`` and therefore may not
call ``PostgresSaver.setup()``.  Existing clusters still need LangGraph's own
versioned migrations when the pinned dependency grows its schema, so
``ava start`` step 2.5 runs that setup once, on the gateway, beside Ava's SQL
migrations.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import psycopg
import pytest


@pytest.fixture
def migration_phase(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[object]]:
    import shared.db
    import shared.migrations
    from shared import cluster

    calls: list[str] = []
    conn = object()

    @contextmanager
    def fake_connect(**kwargs: object) -> Generator[object, None, None]:
        calls.append(f"connect:{kwargs}")
        yield conn

    @contextmanager
    def fake_lock(got: object) -> Generator[None, None, None]:
        assert got is conn
        calls.append("lock")
        yield

    def fake_authority(got: object) -> None:
        assert got is conn
        calls.append("authority")

    def fake_ava_migrations(got: object) -> list[str]:
        assert got is conn
        calls.append("ava")
        return ["20260823T000000_example"]

    def fake_checkpoint_migrations(url: str) -> list[int]:
        calls.append(f"checkpoint:{url}")
        return [10, 11]

    monkeypatch.setattr(shared.db, "connect", fake_connect)
    monkeypatch.setattr(shared.db, "direct_db_url", lambda: "postgresql://direct/ava")
    monkeypatch.setattr(shared.migrations, "schema_mutation_lock", fake_lock, raising=False)
    monkeypatch.setattr(
        shared.migrations, "assert_migration_authority", fake_authority, raising=False
    )
    monkeypatch.setattr(shared.migrations, "apply_pending_migrations", fake_ava_migrations)
    monkeypatch.setattr(
        cluster, "migrate_checkpoint_schema", fake_checkpoint_migrations, raising=False
    )
    return calls, [conn]


def test_gateway_migration_phase_owns_checkpoint_setup(
    monkeypatch: pytest.MonkeyPatch, migration_phase: tuple[list[str], list[object]]
) -> None:
    """Gateway authority + the shared mutation lock cover both schema domains."""
    import shared.machine
    from cli.commands.migrations import cmd_migrations_apply

    calls, _ = migration_phase
    monkeypatch.setattr(shared.machine, "is_gateway", lambda: True)

    applied = cmd_migrations_apply()

    assert calls == [
        "connect:{'direct': True, 'unbounded': True}",
        "lock",
        "authority",
        "ava",
        "checkpoint:postgresql://direct/ava",
    ]
    assert applied == [
        "20260823T000000_example",
        "langgraph-checkpoint-v10",
        "langgraph-checkpoint-v11",
    ]


def test_runner_migration_phase_never_attempts_checkpoint_ddl(
    monkeypatch: pytest.MonkeyPatch, migration_phase: tuple[list[str], list[object]]
) -> None:
    """A runner may check/apply no-op Ava migrations but never calls setup."""
    import shared.machine
    from cli.commands.migrations import cmd_migrations_apply

    calls, _ = migration_phase
    monkeypatch.setattr(shared.machine, "is_gateway", lambda: False)

    applied = cmd_migrations_apply()

    assert calls == ["connect:{'direct': True, 'unbounded': True}", "ava"]
    assert applied == ["20260823T000000_example"]


def test_real_gateway_phase_serializes_and_applies_missing_checkpoint_version(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG proves the outer advisory lock and saver autocommit coexist."""
    from langgraph.checkpoint.postgres import PostgresSaver

    import shared.db
    import shared.machine
    from cli.commands.migrations import cmd_migrations_apply
    from shared.config import settings
    from shared.migrations import required_migration_set

    latest = len(PostgresSaver.MIGRATIONS) - 1
    db_conn.execute("DELETE FROM machine_units")
    db_conn.commit()

    monkeypatch.setattr(shared.machine, "is_gateway", lambda: True)
    monkeypatch.setattr(shared.db, "direct_db_url", lambda: settings.data_plane.db_url)

    # Establish both migration domains explicitly.  The session DB is shared
    # across tests and earlier migration tests intentionally rewrite its
    # bookkeeping, so this test must not inherit whichever state happened to
    # run before it.
    cmd_migrations_apply()
    db_conn.execute("DELETE FROM checkpoint_migrations WHERE v = %s", (latest,))
    db_conn.commit()

    applied = cmd_migrations_apply()

    assert applied == [f"langgraph-checkpoint-v{latest}"]
    row = db_conn.execute("SELECT max(v) FROM checkpoint_migrations").fetchone()
    assert row == (latest,)
    ava_rows = db_conn.execute("SELECT name FROM schema_migrations").fetchall()
    assert {row[0] for row in ava_rows} == required_migration_set()

    assert cmd_migrations_apply() == []
