"""`shared.migrations` unit tests — the timestamp-id + applied-set runner.

The session test DB (conftest's `ava_test_<...>`) is bootstrapped from
`db/schema.sql`, which since the 2026-07-19 re-baseline creates a
`schema_migrations(name, applied_at)` table and stamps the single baseline
sentinel row (see the bottom of db/schema.sql). So every test here starts from
the "baselined, no post-baseline delta" state; the autouse fixture rebuilds that
state before/after each test (cutover tests swap the table to the legacy shape).

Coverage of the three cutover paths the design requires:
- legacy `version INT` at exactly {1..81} -> converted to the baseline
  (`test_cutover_converts_legacy_baseline`)
- fresh DB bootstrapped from schema.sql -> baseline stamped, apply is a no-op
  (`test_fresh_schema_sql_bootstrap_is_baselined`)
- legacy behind the baseline -> refused with a stepping-stone message
  (`test_cutover_refuses_legacy_behind_baseline`)
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from shared.config import settings
from shared.migrations import (
    _BASELINE_NAME,
    _LEGACY_BASELINE_MAX,
    _MIGRATION_LOCK_KEY,
    CodeBehindSchema,
    CutoverRefused,
    CutoverRequired,
    MigrationFailed,
    MigrationLayoutError,
    RollbackBelowFloor,
    SchemaVersionMismatch,
    _down_path,
    _list_migration_files,
    _schema_mutation_lock,
    applied_migration_names,
    apply_down,
    apply_pending_migrations,
    check_schema_version,
    required_migration_set,
    rollback_to,
    validate_migration_layout,
    validate_migrations_at_ref,
)

_SCHEMA_SQL = Path(__file__).resolve().parents[2] / "db" / "schema.sql"

# A syntactically-valid synthetic post-baseline migration name (far-future
# timestamp so it never clashes with a real one).
_SYN = "29991231T235959_synthetic"
_SYN2 = "29991231T235960_synthetic-two"  # sorts after _SYN by name
# Synthetic orphan name (NOT in _V010_PRE_RESET_SET) — simulates a
# post-v0.1.0 re-baseline: an applied name whose file no longer exists.
SYN_ORPHAN = "20260815T000001_synthetic-orphan"


def _set_table_to(
    conn: psycopg.Connection, shape: str, rows: Iterable[str] | Iterable[int] | None = None
) -> None:
    """Rebuild schema_migrations in `shape` ('set' or 'legacy') with `rows`."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS schema_migrations")
        if shape == "set":
            cur.execute(
                "CREATE TABLE schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            for name in rows or ():
                cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
        else:  # legacy
            cur.execute(
                "CREATE TABLE schema_migrations "
                "(version INT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            for v in rows or ():
                cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (v,))
    conn.commit()


@pytest.fixture(autouse=True)
def _reset_schema_migrations_state() -> Iterator[None]:
    """Rebuild schema_migrations to the canonical baselined state before/after each
    test. Rebuilds the table itself (not just TRUNCATE) so a cutover test that
    swapped it to the legacy shape cannot leak into the next test or module.

    conftest's `db_conn` TRUNCATE list does not include schema_migrations (it is
    not business data), so this module self-manages it — the same pattern the
    pre-cutover suite used.
    """

    def _reseed() -> None:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            _set_table_to(conn, "set", [_BASELINE_NAME])

    _reseed()
    yield
    _reseed()


# ─── required / applied set ───────────────────────────────────────────────────


def test_required_set_is_just_baseline_with_empty_migrations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With an empty migrations/ (no delta over the baseline) the required set is
    exactly the baseline sentinel. Isolated to a tmp dir so a real post-baseline
    migration on disk does not change what this contract tests."""
    _init_repo(tmp_path)  # the loader's git-tracking gate needs a real checkout
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    assert required_migration_set() == {_BASELINE_NAME}


def test_list_migration_files_empty_is_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty migrations/ (no delta over the baseline) is valid — the loader
    returns [] instead of raising."""
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    assert _list_migration_files() == []


def test_applied_migration_names_reads_the_set(db_conn: psycopg.Connection) -> None:
    """applied_migration_names returns the DB's applied name set (baseline only in
    the seeded state)."""
    assert applied_migration_names(db_conn) == {_BASELINE_NAME}


# ─── check_schema_version (set equality, both directions) ─────────────────────


def test_check_passes_when_aligned(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """applied {baseline} == required {baseline}: no raise. Isolated to an empty
    migrations/ so the seeded baseline-only DB is aligned regardless of real
    post-baseline migrations on disk."""
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
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


def test_check_raises_cutover_required_on_legacy(db_conn: psycopg.Connection) -> None:
    """A read path (check) that meets a legacy integer table raises
    CutoverRequired rather than silently converting."""
    _set_table_to(db_conn, "legacy", range(1, _LEGACY_BASELINE_MAX + 1))
    with pytest.raises(CutoverRequired):
        check_schema_version(db_conn)


# ─── cutover: legacy -> baseline (the three required paths) ───────────────────


def test_cutover_converts_legacy_baseline(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A legacy table at exactly {1..81} is converted in place to the applied-set
    baseline by apply_pending_migrations. Isolated to an empty migrations/ so the
    assertion is about the conversion alone, not any real post-baseline delta."""
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    _set_table_to(db_conn, "legacy", range(1, _LEGACY_BASELINE_MAX + 1))
    with psycopg.connect(settings.data_plane.db_url) as fresh:
        applied = apply_pending_migrations(fresh)
    assert applied == []
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='schema_migrations'"
        )
        cols = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT name FROM schema_migrations")
        names = {r[0] for r in cur.fetchall()}
    assert "name" in cols and "version" not in cols  # converted to the new shape
    assert names == {_BASELINE_NAME}


def test_cutover_refuses_legacy_behind_baseline(db_conn: psycopg.Connection) -> None:
    """A legacy table behind {1..81} (missing the tail) is refused with a
    stepping-stone message; the table is left untouched."""
    _set_table_to(db_conn, "legacy", range(1, _LEGACY_BASELINE_MAX))  # 1..80, missing 81
    with pytest.raises(CutoverRefused) as ei, psycopg.connect(settings.data_plane.db_url) as fresh:
        apply_pending_migrations(fresh)
    assert str(_LEGACY_BASELINE_MAX) in str(ei.value)
    # untouched: still legacy shape
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='schema_migrations' AND column_name='version'"
        )
        assert cur.fetchone() is not None


def test_fresh_schema_sql_bootstrap_is_baselined() -> None:
    """The real fresh-DB bootstrap: apply db/schema.sql to an empty database and
    assert it lands in the baselined applied-set state (new shape + baseline row,
    presets seeded), and apply_pending is then a no-op. Isolated throwaway DB, so
    this never touches the shared session DB."""
    base_url, _ = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"ava_test_mig_{os.getpid()}_{int(time.time() * 1_000_000)}"
    url = f"{base_url}/{name}"
    schema = _SCHEMA_SQL.read_text()

    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(schema)  # type: ignore[arg-type]  # trusted multi-statement schema
            row = conn.execute("SELECT name FROM schema_migrations").fetchall()
            assert row == [(_BASELINE_NAME,)]
            presets = conn.execute("SELECT name FROM agent_presets").fetchall()
            assert {r[0] for r in presets} == {
                "coder",
                "reviewer",
                "researcher",
                "orchestrator",
                "explorer",
            }
        # apply on the baselined DB: the real post-baseline migrations replay
        # cleanly on the fresh schema (fresh-replay), then a second apply is a
        # no-op. This is what guards against a migration that fails on a fresh DB.
        with psycopg.connect(url) as conn:
            assert set(apply_pending_migrations(conn)) == required_migration_set() - {
                _BASELINE_NAME
            }
        with psycopg.connect(url) as conn:
            assert apply_pending_migrations(conn) == []
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


# ─── apply_pending: post-baseline delta ───────────────────────────────────────


def test_apply_pending_nothing_when_baselined(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Baselined DB, empty migrations/ -> nothing to apply."""
    _ = db_conn  # fixture reseeds the baseline
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with psycopg.connect(settings.data_plane.db_url) as fresh:
        assert apply_pending_migrations(fresh) == []


def test_apply_pending_applies_post_baseline(db_conn, monkeypatch, tmp_path) -> None:
    """A synthetic post-baseline migration in migrations/ is applied and stamped by
    name; a second apply is idempotent."""
    _ = db_conn
    (tmp_path / f"{_SYN}.sql").write_text("CREATE TABLE syn_t (id int);")
    (tmp_path / f"{_SYN}.down.sql").write_text("DROP TABLE syn_t;")
    _init_repo(tmp_path)  # applied only if git-tracked (#998)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
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


# ─── git-tracking gate (#998: untracked migrations must never apply) ──────────
#
# 2026-08-07 incident: a migration file written into the running checkout's
# migrations/ WITHOUT being committed to git was auto-applied by the watchdog's
# self-heal (`apply_pending_migrations` scanned the directory, not the index),
# wedging the cluster. These tests pin the fix: only git-tracked files are
# applied or counted as required; untracked ones are warned about and skipped.


def test_untracked_migration_is_skipped_and_warned(
    db_conn, monkeypatch, tmp_path, loguru_records
) -> None:
    """The regression itself: a rogue migration sitting untracked in migrations/
    must NOT be applied. The tracked migration applies normally; the untracked
    one is skipped with a warning and never reaches the schema."""
    _ = db_conn
    (tmp_path / f"{_SYN}.sql").write_text("CREATE TABLE syn_ok_t (id int);")
    (tmp_path / f"{_SYN}.down.sql").write_text("DROP TABLE syn_ok_t;")
    _init_repo(tmp_path)  # _SYN is now tracked
    rogue = "29991231T235958_synthetic-rogue"
    (tmp_path / f"{rogue}.sql").write_text("CREATE TABLE syn_rogue_t (id int);")
    (tmp_path / f"{rogue}.down.sql").write_text("DROP TABLE syn_rogue_t;")
    # NOT git-added: the rogue sits untracked, exactly like the incident file.
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
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
    (tmp_path / f"{_SYN}.down.sql").write_text("-- noop")
    _init_repo(tmp_path)  # _SYN tracked
    (tmp_path / f"{_SYN2}.sql").write_text("-- noop")  # untracked
    (tmp_path / f"{_SYN2}.down.sql").write_text("-- noop")
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    assert required_migration_set() == {_BASELINE_NAME, _SYN}


def test_untracked_malformed_name_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Layout validation applies to what git tracks; an untracked file with a
    malformed name is skipped like any other untracked file, not fatal."""
    (tmp_path / f"{_SYN}.sql").write_text("-- noop")
    (tmp_path / f"{_SYN}.down.sql").write_text("-- noop")
    _init_repo(tmp_path)
    (tmp_path / "0001_legacy.sql").write_text("-- noop")  # malformed AND untracked
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    assert [n for n, _ in _list_migration_files()] == [_SYN]


def test_non_git_dir_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A migrations dir whose repo root is not a git worktree is refused: the
    loader must not apply files whose git-tracking status it cannot verify."""
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with pytest.raises(MigrationLayoutError, match="git worktree"):
        _list_migration_files()


# ─── layout validation ────────────────────────────────────────────────────────


class TestLayoutValidation:
    def test_dir_missing_raises(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path / "nope")
        with pytest.raises(MigrationLayoutError, match="does not exist"):
            _list_migration_files()

    def test_bad_filename_raises(self, tmp_path, monkeypatch) -> None:
        (tmp_path / f"{_SYN}.sql").write_text("-- noop")
        (tmp_path / "0001_legacy.sql").write_text("-- noop")  # old integer format
        _init_repo(tmp_path)  # tracked: layout validation applies to git-tracked files
        monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
        with pytest.raises(MigrationLayoutError, match="does not match"):
            _list_migration_files()

    def test_readme_and_down_and_dotfiles_skipped(self, tmp_path, monkeypatch) -> None:
        (tmp_path / f"{_SYN}.sql").write_text("-- noop")
        (tmp_path / f"{_SYN}.down.sql").write_text("-- noop")
        (tmp_path / "README.md").write_text("docs")
        (tmp_path / ".DS_Store").write_text("junk")
        _init_repo(tmp_path)
        monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
        assert [n for n, _ in _list_migration_files()] == [_SYN]


class TestValidateMigrationLayout:
    def test_good_names_pass(self) -> None:
        validate_migration_layout([f"{_SYN}.sql", f"{_SYN2}.sql"])

    def test_empty_is_valid(self) -> None:
        validate_migration_layout([".DS_Store", "README.md", f"{_SYN}.down.sql"])

    def test_duplicate_raises(self) -> None:
        with pytest.raises(MigrationLayoutError, match="duplicate migration name"):
            validate_migration_layout([f"{_SYN}.sql", f"{_SYN}.sql"])

    def test_bad_filename_raises(self) -> None:
        with pytest.raises(MigrationLayoutError, match="does not match"):
            validate_migration_layout([f"{_SYN}.sql", "0049_event_log.sql"])


def _git(repo, *args) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603 — test git fixture, fixed argv


def _init_repo(repo: Path) -> None:
    """Make `repo` a git worktree with an initial commit. The loader's
    git-tracking gate (#998) applies only what git tracks, so any test that
    monkeypatches MIGRATIONS_DIR must model a real checkout. Call AFTER writing
    the files that should be tracked (they land in the initial commit); files
    written afterwards are untracked by construction."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")


class TestValidateMigrationsAtRef:
    def _init_repo(self, repo, names: list[str]) -> None:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        (repo / "migrations").mkdir()
        for name in names:
            (repo / "migrations" / name).write_text("-- noop\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")

    def test_good_ref_passes(self, tmp_path) -> None:
        self._init_repo(tmp_path, [f"{_SYN}.sql", f"{_SYN2}.sql"])
        validate_migrations_at_ref("HEAD", repo_root=tmp_path)

    def test_duplicate_in_ref_raises(self, tmp_path) -> None:
        self._init_repo(tmp_path, [f"{_SYN}.sql", "sub_dir_placeholder.sql"])
        # a malformed name at the ref is refused before any service is stopped
        with pytest.raises(MigrationLayoutError, match="does not match"):
            validate_migrations_at_ref("HEAD", repo_root=tmp_path)

    def test_unreadable_ref_raises(self, tmp_path) -> None:
        self._init_repo(tmp_path, [f"{_SYN}.sql"])
        with pytest.raises(MigrationLayoutError, match="cannot read migrations/"):
            validate_migrations_at_ref("no-such-ref", repo_root=tmp_path)


def test_apply_multi_statement_migration_over_prepared_conn(monkeypatch, tmp_path) -> None:
    """A MULTI-statement migration body applies over a `prepare_threshold=0`
    connection — the posture `shared.db.connect()` uses (unconditionally, for
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
    (tmp_path / f"{_SYN}.down.sql").write_text("DROP TABLE mstest_t;")
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)

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


# ─── apply_down / rollback_to (set-based) ─────────────────────────────────────


def test_apply_down_round_trip(db_conn, monkeypatch, tmp_path) -> None:
    (tmp_path / f"{_SYN}.down.sql").write_text("DROP TABLE dtest_t;")
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with db_conn.cursor() as cur:
        cur.execute("CREATE TABLE dtest_t (id int)")
        cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (_SYN,))
    db_conn.commit()

    apply_down(db_conn, _SYN)

    with db_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('dtest_t')")
        assert cur.fetchone()[0] is None  # table dropped
        cur.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (_SYN,))
        assert cur.fetchone() is None  # row removed


def test_apply_down_missing_down_raises(db_conn, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)  # no .down.sql
    with pytest.raises(MigrationLayoutError):
        _down_path(_SYN)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (_SYN,))
    db_conn.commit()
    with pytest.raises(MigrationLayoutError):
        apply_down(db_conn, _SYN)


def test_apply_down_atomic_on_failure(db_conn, monkeypatch, tmp_path) -> None:
    """A failing down SQL must NOT delete the schema_migrations row — one txn."""
    (tmp_path / f"{_SYN}.down.sql").write_text("DROP TABLE does_not_exist;")
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (_SYN,))
    db_conn.commit()

    with pytest.raises(MigrationFailed):
        apply_down(db_conn, _SYN)
    db_conn.rollback()

    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (_SYN,))
        assert cur.fetchone() is not None  # row survived


def test_rollback_to_floor_guard(db_conn: psycopg.Connection) -> None:
    """Rolling back below the baseline (keep set excludes the baseline) is
    refused: the baseline has no down."""
    with pytest.raises(RollbackBelowFloor):
        rollback_to(db_conn, set())  # applied {baseline}; to_roll would include baseline
    db_conn.rollback()


def test_rollback_to_descends(db_conn, monkeypatch, tmp_path) -> None:
    """rollback_to reverses the applied names NOT in `keep`, in reverse-name order,
    keeping the baseline."""
    for stem in (_SYN, _SYN2):
        (tmp_path / f"{stem}.down.sql").write_text(f"DROP TABLE t_{stem[-1]};")
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with db_conn.cursor() as cur:
        for stem in (_SYN, _SYN2):
            cur.execute(f"CREATE TABLE t_{stem[-1]} (id int)")
            cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (stem,))
    db_conn.commit()

    rolled = rollback_to(db_conn, {_BASELINE_NAME})

    assert rolled == [_SYN2, _SYN]  # reverse-name (descending) order
    with db_conn.cursor() as cur:
        cur.execute("SELECT name FROM schema_migrations ORDER BY name")
        assert cur.fetchall() == [(_BASELINE_NAME,)]


# ─── migration-apply advisory lock (serializes concurrent appliers) ───────────


def _try_lock_from_other_conn() -> bool:
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
        got = cur.fetchone()[0]  # type: ignore[index]
        if got:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
        return bool(got)


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


def test_rollback_to_holds_the_lock(db_conn: psycopg.Connection, monkeypatch, tmp_path) -> None:
    """rollback_to runs under the schema-mutation lock (a recovery rollback must
    not race a bootstrap forward apply)."""
    (tmp_path / f"{_SYN}.down.sql").write_text("DROP TABLE lock_t;")
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with db_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS lock_t")
        cur.execute("CREATE TABLE lock_t (id int)")
        cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (_SYN,))
    db_conn.commit()

    held: list[bool] = []
    from shared.migrations import apply_down as real_apply_down

    def _spy(conn: psycopg.Connection, name: str) -> None:
        held.append(_try_lock_from_other_conn() is False)
        real_apply_down(conn, name)

    monkeypatch.setattr("shared.migrations.apply_down", _spy)
    rolled = rollback_to(db_conn, {_BASELINE_NAME})
    assert rolled == [_SYN]
    assert held == [True]


# ─── per-migration data check: seed-preset skill-index removal ───────────────
#
# Migrations are normally covered structurally (the fresh-replay in
# test_fresh_schema_sql_bootstrap_is_baselined). This one gets data tests
# because it inverts a meaning rather than moving a column: the seed presets'
# skills_to_inject_into_system_prompt lists were ADDITIONS on top of a 5-name
# cluster default, and under the `["*"]` default the identical value NARROWS an
# agent's index instead. Three consequences these tests pin, all about the
# migration touching only what it shipped:
#   - it matches on VALUE, never on name alone, so an operator who edited a
#     seed-named preset and chose a narrowing keeps it (nothing could restore it);
#   - it reaches agents_meta.config_overlay too, because a spawn COPIES the
#     preset's config into the agent and drops the preset name, so a live agent
#     would otherwise carry the inverted list for the rest of its life;
#   - it reaches agents_meta.birth_config as well, because 20260731T071400
#     stamped the same 5-name list onto every non-terminated agent — and
#     birth_config outranks the code default, so stripping only the overlay would
#     unmask the stamp and leave the agent equally narrowed one layer down.
#
# The preset cases DRIVE THE REAL MIGRATION CHAIN rather than restating the
# migration's own literals. A test that hand-copies the lists it is checking can
# only catch a drift that hits one copy — the failure mode here is a consistent
# edit that misses reality, which is exactly what happened when 042431 rewrote
# the presets to dash and this migration kept matching underscore. So the tests
# seed the historical underscore state ONCE (_LEGACY_SEED_LISTS below, frozen
# forever: it is what db/schema.sql shipped before the rename), then run

_KEY = "skills_to_inject_into_system_prompt"


# ─── re-baseline convergence (v0.1.0 schema reset, 2026-08-14) ───────────────
#
# The pre-release migration history was squashed into db/schema.sql at the
# v0.1.0 public release; migrations/ holds only the empty v010 anchor. A
# cluster upgrading across the reset has applied-set rows whose files no
# longer exist — apply_pending_migrations must converge them away (the
# convergence gate proves db/schema.sql is their net effect).


def test_schema_sql_has_birth_config_column() -> None:
    """Terminal-state pin (the pre-reset birth-config backfill contract, now
    folded into the baseline): agents_meta carries birth_config, and the
    CHECK-level comment in schema.sql documents the overlay-vs-default
    precedence the migration used to enforce by hand."""
    schema = _SCHEMA_SQL.read_text()
    assert "birth_config               JSONB" in schema, (
        "birth_config column missing from agents_meta"
    )


def test_schema_sql_has_r1_deploy_state_tables() -> None:
    """Terminal-state pin (the pre-reset r1 deploy-state backfill contract):
    the baseline carries both deploy-state tables the rollout machinery reads."""
    schema = _SCHEMA_SQL.read_text()
    for table in ("host_deploy_state",):
        assert f"CREATE TABLE {table}" in schema, f"{table} missing from baseline"


def test_schema_sql_seeds_presets_without_a_skill_index() -> None:
    """The baseline must agree with the migrated state: a fresh DB's seed presets
    carry no skills_to_inject_into_system_prompt, so fresh and upgraded clusters
    do not disagree about what a `coder` is."""
    seed_block = _SCHEMA_SQL.read_text().split("INSERT INTO agent_presets")[1]
    seed_block = seed_block.split("ON CONFLICT")[0]
    assert _KEY not in seed_block


def test_apply_pending_squashes_orphaned_applied_names(db_conn, monkeypatch, tmp_path) -> None:
    """The v0.1.0 reset scenario: the DB's applied set holds pre-reset migration
    names whose files no longer exist in migrations/. apply converges them away
    (schema unchanged — the baseline already carries their effect) and then
    applies what is pending; a second apply is a no-op."""
    _ = db_conn  # fixture reseeds the baseline
    orphan = "20260815T000001_synthetic-orphan"
    _init_repo(tmp_path)  # migrations/ = tmp_path, git-tracked anchor applies
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (orphan,))
    try:
        with psycopg.connect(settings.data_plane.db_url) as fresh:
            assert apply_pending_migrations(fresh) == []
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(_BASELINE_NAME,)]
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            c.execute("DELETE FROM schema_migrations WHERE name = %s", (orphan,))


def test_apply_pending_squash_then_apply_pending(db_conn, monkeypatch, tmp_path) -> None:
    """Squash and forward-apply compose in one call: an orphaned pre-reset name
    is converged away AND a pending migration (the v010 anchor, say) applies
    in the same run."""
    _ = db_conn
    orphan = "20260815T000001_synthetic-orphan"
    (tmp_path / f"{_SYN}.sql").write_text("CREATE TABLE syn_squash_t (id int);")
    (tmp_path / f"{_SYN}.down.sql").write_text("DROP TABLE syn_squash_t;")
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (orphan,))
    try:
        with psycopg.connect(settings.data_plane.db_url) as fresh:
            assert apply_pending_migrations(fresh) == [_SYN]
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(_BASELINE_NAME,), (_SYN,)]
            cur.execute("SELECT to_regclass('syn_squash_t')")
            row = cur.fetchone()
            assert row is not None and row[0] is not None
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS syn_squash_t")
            c.execute("DELETE FROM schema_migrations WHERE name = %s", (orphan,))


def test_squash_does_not_touch_baseline_or_pending(db_conn, monkeypatch, tmp_path) -> None:
    """Convergence is precise: the baseline sentinel is never deleted, and a name
    that is BOTH applied and present as a file is not touched (its row stays,
    nothing to squash)."""
    _ = db_conn
    (tmp_path / f"{_SYN}.sql").write_text("CREATE TABLE syn_keep_t (id int);")
    (tmp_path / f"{_SYN}.down.sql").write_text("DROP TABLE syn_keep_t;")
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (_SYN,))
    try:
        with psycopg.connect(settings.data_plane.db_url) as fresh:
            assert apply_pending_migrations(fresh) == []  # _SYN applied; nothing pending
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(_BASELINE_NAME,), (_SYN,)]
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS syn_keep_t")
            c.execute("DELETE FROM schema_migrations WHERE name = %s", (_SYN,))


def test_squash_authority_checked_even_without_pending(db_conn, monkeypatch, tmp_path) -> None:
    """A squash is a mutation: a non-gateway checkout that would only ever
    trigger the squash path (no pending files) must still be refused."""
    _ = db_conn
    orphan = "20260815T000001_synthetic-orphan"
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (orphan,))
        # Give the DB a gateway identity so _assert_migration_authority has
        # something to refuse: without any machine_units rows the authority
        # check passes vacuously (a fresh, unclaimed DB).
        c.execute(
            "INSERT INTO machine_units (machine_name, home, serve_gateway) "
            "VALUES ('real-gateway', '/real/home', true)"
        )
    try:
        # Point the checkout identity somewhere that is NOT the DB's gateway
        # unit; _assert_migration_authority must fire even though pending == [].
        import shared.migrations as _m

        monkeypatch.setattr(
            _m, "checkout_anchored_home", lambda: (Path("/nonexistent/home"), False)
        )
        from shared.migrations import MigrationAuthorityMismatch

        with psycopg.connect(settings.data_plane.db_url) as fresh:
            try:
                apply_pending_migrations(fresh)
            except MigrationAuthorityMismatch:
                pass  # expected: squash refused for non-gateway
            else:
                raise AssertionError("squash without authority must be refused")
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            c.execute("DELETE FROM schema_migrations WHERE name = %s", (orphan,))
            c.execute("DELETE FROM machine_units WHERE machine_name = 'real-gateway'")


def test_squash_logs_the_converged_names(db_conn, monkeypatch, tmp_path, loguru_records) -> None:
    """The convergence is loud: the log names every orphaned applied name so an
    operator can audit what the reset folded away."""
    _ = db_conn
    orphan = "20260815T000001_synthetic-orphan"
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (orphan,))
    try:
        with psycopg.connect(settings.data_plane.db_url) as fresh:
            apply_pending_migrations(fresh)
        assert any("squash" in r["message"] and orphan in r["message"] for r in loguru_records), (
            "the squash must be loud"
        )
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            c.execute("DELETE FROM schema_migrations WHERE name = %s", (orphan,))


def test_squash_refuses_partial_pre_reset_history(db_conn, monkeypatch, tmp_path) -> None:
    """P1 guard: a DB holding only PART of the pre-v0.1.0 history must be
    refused, never silently converged — deleting its tracking rows would
    certify a schema that never ran the missing migrations."""
    from shared.migrations import _V010_PRE_RESET_SET, MigrationHistoryGap

    assert len(_V010_PRE_RESET_SET) == 59, "frozen set drifted"
    partial = sorted(_V010_PRE_RESET_SET)[:24]  # the 8/1-cluster shape
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
        for name in partial:
            c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
    try:
        with (
            psycopg.connect(settings.data_plane.db_url) as fresh,
            pytest.raises(MigrationHistoryGap),
        ):
            apply_pending_migrations(fresh)
        # nothing was deleted by the refusal
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            assert cur.fetchone()[0] == 1 + len(partial)
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            for name in partial:
                c.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))


def test_squash_converges_full_pre_reset_history(db_conn, monkeypatch, tmp_path) -> None:
    """A DB that ran the COMPLETE pre-reset history converges cleanly: the
    baseline carries the net effect of all 47, so deleting their tracking rows
    is safe and leaves applied == required."""
    from shared.migrations import _V010_PRE_RESET_SET

    all_names = sorted(_V010_PRE_RESET_SET)
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
        for name in all_names:
            c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
    try:
        with psycopg.connect(settings.data_plane.db_url) as fresh:
            assert apply_pending_migrations(fresh) == []
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(_BASELINE_NAME,)]
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            for name in all_names:
                c.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))


def test_squash_ignores_pre_reset_names_outside_the_frozen_set(
    db_conn, monkeypatch, tmp_path
) -> None:
    """A pre-reset-era name NOT in the frozen set is an unknown: the guard
    ignores it (it is not part of the squashed history), and convergence still
    deletes it like any other orphan — the frozen set only gates the 47."""
    _ = db_conn
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (SYN_ORPHAN,))
    try:
        with psycopg.connect(settings.data_plane.db_url) as fresh:
            assert apply_pending_migrations(fresh) == []
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(_BASELINE_NAME,)]
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            c.execute("DELETE FROM schema_migrations WHERE name = %s", (SYN_ORPHAN,))
