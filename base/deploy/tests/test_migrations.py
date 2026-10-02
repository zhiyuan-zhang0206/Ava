"""Applied-set runner, transactions, and locks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.config import settings
from base.deploy.schema.migrations import (
    _BASELINE_NAME,
    CodeBehindSchema,
    MigrationLayoutError,
    SchemaVersionMismatch,
    _list_migration_files,
    _schema_mutation_lock,
    applied_migration_names,
    apply_pending_migrations,
    check_schema_version,
    required_migration_set,
)
from base.deploy.tests.migration_support import (
    _SYN,
    _SYN2,
    _init_repo,
    _set_table_to,
    _try_lock_from_other_conn,
)
from base.deploy.tests.migration_support import (
    _reset_schema_migrations_state as _reset_schema_migrations_state,
)


def test_required_set_is_just_baseline_with_empty_migrations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With an empty migrations/ (no delta over the baseline) the required set is
    exactly the baseline sentinel. Isolated to a tmp dir so a real post-baseline
    migration on disk does not change what this contract tests."""
    _init_repo(tmp_path)  # the loader's git-tracking gate needs a real checkout
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)
    assert required_migration_set() == {_BASELINE_NAME}


def test_list_migration_files_empty_is_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty migrations/ (no delta over the baseline) is valid — the loader
    returns [] instead of raising."""
    _init_repo(tmp_path)
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)
    assert _list_migration_files() == []


def test_applied_migration_names_reads_the_set(db_conn: psycopg.Connection) -> None:
    """applied_migration_names returns the DB's applied name set (baseline only in
    the seeded state)."""
    assert applied_migration_names(db_conn) == {_BASELINE_NAME}


def test_check_passes_when_aligned(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """applied {baseline} == required {baseline}: no raise. Isolated to an empty
    migrations/ so the seeded baseline-only DB is aligned regardless of real
    post-baseline migrations on disk."""
    _init_repo(tmp_path)
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)
    check_schema_version(db_conn)


def test_check_raises_when_db_behind(db_conn: psycopg.Connection) -> None:
    """Drop the baseline row -> applied {} is missing the baseline -> behind code."""
    _set_table_to(db_conn, "set", [])  # empty, no baseline
    with pytest.raises(SchemaVersionMismatch) as ei:
        check_schema_version(db_conn)
    assert _BASELINE_NAME in str(ei.value)


def test_check_raises_when_code_behind(db_conn: psycopg.Connection) -> None:
    """An applied name the code does not carry -> DB ahead -> CodeBehindSchema."""
    _set_table_to(db_conn, "set", [_BASELINE_NAME, _SYN])
    with pytest.raises(CodeBehindSchema) as ei:
        check_schema_version(db_conn)
    assert _SYN in str(ei.value)


def test_apply_pending_nothing_when_baselined(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Baselined DB, empty migrations/ -> nothing to apply."""
    _ = db_conn  # fixture reseeds the baseline
    _init_repo(tmp_path)
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)
    with psycopg.connect(settings.data_plane.db_url) as fresh:
        assert apply_pending_migrations(fresh) == []


def test_apply_pending_applies_post_baseline(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A synthetic post-baseline migration in migrations/ is applied and stamped by
    name; a second apply is idempotent."""
    _ = db_conn
    (tmp_path / f"{_SYN}.sql").write_text("CREATE TABLE syn_t (id int);")
    _init_repo(tmp_path)  # applied only if git-tracked (#998)
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)
    try:
        with psycopg.connect(settings.data_plane.db_url) as fresh:
            assert apply_pending_migrations(fresh) == [_SYN]
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(_BASELINE_NAME,), (_SYN,)]
            cur.execute("SELECT to_regclass('syn_t')")
            row = cur.fetchone()
            assert row is not None and row[0] is not None
        with psycopg.connect(settings.data_plane.db_url) as again:
            assert apply_pending_migrations(again) == []  # idempotent
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS syn_t")


def test_untracked_migration_is_skipped_and_warned(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loguru_records: list[dict[str, Any]],
) -> None:
    """The regression itself: a rogue migration sitting untracked in migrations/
    must NOT be applied. The tracked migration applies normally; the untracked
    one is skipped with a warning and never reaches the schema."""
    _ = db_conn
    (tmp_path / f"{_SYN}.sql").write_text("CREATE TABLE syn_ok_t (id int);")
    _init_repo(tmp_path)  # _SYN is now tracked
    rogue = "29991231T235958_synthetic-rogue"
    (tmp_path / f"{rogue}.sql").write_text("CREATE TABLE syn_rogue_t (id int);")
    # NOT git-added: the rogue sits untracked, exactly like the incident file.
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)
    try:
        with psycopg.connect(settings.data_plane.db_url) as fresh:
            assert apply_pending_migrations(fresh) == [_SYN]
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            row = c.execute("SELECT to_regclass('syn_rogue_t')").fetchone()
            assert row is None or row[0] is None  # the rogue never ran
            row = c.execute("SELECT to_regclass('syn_ok_t')").fetchone()
            assert row is not None and row[0] is not None  # the tracked one did
        assert any("untracked" in r["message"] and rogue in r["message"] for r in loguru_records), (
            "the skip must be loud"
        )
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS syn_ok_t")
            c.execute("DROP TABLE IF EXISTS syn_rogue_t")


def test_untracked_file_excluded_from_required_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The startup sanity check must not demand an untracked migration — it is
    not part of this checkout's code, so a DB without it is not "behind". (The
    false "schema behind code" signal is what kept the incident wedge in place
    after the watchdog's rollback.)"""
    (tmp_path / f"{_SYN}.sql").write_text("-- noop")
    _init_repo(tmp_path)  # _SYN tracked
    (tmp_path / f"{_SYN2}.sql").write_text("-- noop")  # untracked
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)
    assert required_migration_set() == {_BASELINE_NAME, _SYN}


def test_untracked_malformed_name_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Layout validation applies to what git tracks; an untracked file with a
    malformed name is skipped like any other untracked file, not fatal."""
    (tmp_path / f"{_SYN}.sql").write_text("-- noop")
    _init_repo(tmp_path)
    (tmp_path / "0001_legacy.sql").write_text("-- noop")  # malformed AND untracked
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)
    assert [n for n, _ in _list_migration_files()] == [_SYN]


def test_non_git_dir_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A migrations dir whose repo root is not a git worktree is refused: the
    loader must not apply files whose git-tracking status it cannot verify."""
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)
    with pytest.raises(MigrationLayoutError, match="git worktree"):
        _list_migration_files()


def test_apply_multi_statement_migration_over_prepared_conn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A MULTI-statement migration body applies over a `prepare_threshold=0`
    connection — the posture `base.db.connect()` uses (unconditionally, for
    PgBouncer transaction-pool safety). Regression for the main-CI breakage:
    prepare_threshold=0 forces the extended (prepared-statement) protocol on the
    first execute, and Postgres rejects a prepared statement that carries multiple
    commands ("cannot insert multiple commands into a prepared statement"). The
    applier now runs bodies with prepare=False (simple protocol), so a multi-
    statement migration is not tied to the caller's prepare posture. Without the
    fix this raises MigrationFailed."""
    (tmp_path / f"{_SYN}.sql").write_text(
        "CREATE TABLE mstest_t (id int, note text);\nINSERT INTO mstest_t (id, note) VALUES (1, 'ok');"
    )
    _init_repo(tmp_path)
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)

    # A dedicated connection carrying the prepare posture that broke apply. The
    # applier needs a non-autocommit conn (it manages per-migration transactions).
    with psycopg.connect(settings.data_plane.db_url, prepare_threshold=0) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS mstest_t")
        conn.commit()
        applied = apply_pending_migrations(conn)
        assert _SYN in applied
        with conn.cursor() as cur:
            cur.execute("SELECT id, note FROM mstest_t")
            assert cur.fetchone() == (1, "ok")  # both statements of the body ran
        # Clean up the table + the applied row (the autouse fixture reseeds
        # schema_migrations, but the table lives outside its purview).
        with conn.cursor() as cur:
            cur.execute("DROP TABLE mstest_t")
        conn.commit()


def test_schema_mutation_lock_is_exclusive(db_conn: psycopg.Connection) -> None:
    assert _try_lock_from_other_conn() is True
    with _schema_mutation_lock(db_conn):
        assert _try_lock_from_other_conn() is False
    assert _try_lock_from_other_conn() is True
    db_conn.rollback()


def test_schema_mutation_lock_released_on_exception(db_conn: psycopg.Connection) -> None:
    with pytest.raises(RuntimeError, match="boom"), _schema_mutation_lock(db_conn):
        raise RuntimeError("boom")
    assert _try_lock_from_other_conn() is True
    db_conn.rollback()
