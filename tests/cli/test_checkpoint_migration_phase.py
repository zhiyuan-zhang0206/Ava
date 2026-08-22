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


@pytest.fixture
def migration_phase(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import shared.db
    import shared.migrations
    from shared import cluster

    calls: list[str] = []
    conn = object()

    @contextmanager
    def fake_connect(**kwargs: object) -> Generator[object, None, None]:
        calls.append(f"connect:{kwargs}")
        yield conn

    def fake_dependency_gate() -> None:
        calls.append("dependency")

    def fake_ava_migrations(got: object) -> list[str]:
        assert got is conn
        calls.append("ava")
        return ["20260823T000000_example"]

    def fake_checkpoint_assertion(url: str) -> None:
        calls.append(f"checkpoint:{url}")

    monkeypatch.setattr(shared.db, "connect", fake_connect)
    monkeypatch.setattr(shared.db, "direct_db_url", lambda: "postgresql://direct/ava")
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
    """All capabilities share this read-only post-migration checkpoint gate."""
    from cli.commands.migrations import cmd_migrations_apply

    applied = cmd_migrations_apply()

    assert migration_phase == [
        "dependency",
        "connect:{'direct': True, 'unbounded': True}",
        "ava",
        "checkpoint:postgresql://direct/ava",
    ]
    assert applied == ["20260823T000000_example"]


def test_real_start_phase_converges_ava_then_is_idempotent(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG proves both migration domains are exact on repeated starts."""
    import shared.db
    from cli.commands.migrations import cmd_migrations_apply
    from shared.config import settings
    from shared.migrations import required_migration_set

    db_conn.execute("DELETE FROM machine_units")
    _seed_checkpoint_versions(db_conn)
    monkeypatch.setattr(shared.db, "direct_db_url", lambda: settings.data_plane.db_url)

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

    import shared.db
    from cli.commands.migrations import cmd_migrations_apply
    from shared.cluster.provision import CheckpointDependencyDriftError
    from shared.config import settings

    db_conn.execute("DELETE FROM machine_units")
    _seed_checkpoint_versions(db_conn)
    monkeypatch.setattr(shared.db, "direct_db_url", lambda: settings.data_plane.db_url)
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
