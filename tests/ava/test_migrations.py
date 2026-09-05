"""`shared.migrations` unit tests — the timestamp-id + applied-set runner.

The session test DB (conftest's `ava_test_<...>`) is bootstrapped from
`db/schema.sql`, which since the 2026-07-19 re-baseline creates a
`schema_migrations(name, applied_at)` table and stamps the baseline sentinel
plus migration names already folded into that current schema (see the bottom of
db/schema.sql). The autouse fixture starts each migration-runner test with only
the baseline sentinel, so it can model its own post-baseline applied set without
production seed markers leaking into synthetic migration layouts; teardown
restores the full schema.sql seed for tests outside this module.

Coverage of the three cutover paths the design requires:
- legacy `version INT` at exactly {1..81} -> converted to the baseline
  (`test_cutover_converts_legacy_baseline`)
- fresh DB bootstrapped from schema.sql -> baseline stamped, apply is a no-op
  (`test_fresh_schema_sql_bootstrap_is_baselined`)
- legacy behind the baseline -> refused with a stepping-stone message
  (`test_cutover_refuses_legacy_behind_baseline`)
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any, LiteralString, cast

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


def _schema_sql_stamped_migration_names() -> list[str]:
    """The applied-set stamps db/schema.sql seeds, in file order.

    A folded strict migration keeps its stamp row so a fresh DB does not replay
    it. Reading the stamps from the file (rather than hard-coding a
    baseline+folded list) keeps this exact; the next folded migration cannot
    silently strand the fixture.
    """
    import re

    return re.findall(
        r"INSERT INTO schema_migrations \(name\) VALUES \('([^']+)'\)",
        _SCHEMA_SQL.read_text(),
    )


_FORCE_FENCE_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "20260821T104519_add-force-terminate-inbound-fence.sql"
)
_LAST_CLAIM_LOOP_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "20260901T065353_add-last-claim-loop-at.sql"
)
_LAST_CLAIM_LOOP_MIGRATION_NAME = _LAST_CLAIM_LOOP_MIGRATION.stem
_DEFAULT_MODEL_VISION_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "20260903T044332_default-model-deepseek-v4-flash-vision-exp.sql"
)
_SNAPSHOT_RETIREMENT_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "20260901T141933_drop-expired-backfill-snapshots.sql"
)
_SNAPSHOT_RETIREMENT_MIGRATION_NAME = _SNAPSHOT_RETIREMENT_MIGRATION.stem
_FAILURE_FEEDBACK_UP = (
    Path(__file__).resolve().parents[2] / "migrations" / "20260905T121043_failure-feedback.sql"
)
_FAILURE_FEEDBACK_DOWN = (
    Path(__file__).resolve().parents[2] / "migrations" / "20260905T121043_failure-feedback.down.sql"
)
_RETIRED_SNAPSHOT_TABLES = (
    "fork_lineage_fix_backfill_agents_meta",
    "fork_lineage_fix_backfill_events",
    "ledger_unpriced_backfill_20260824",
)

# A syntactically-valid synthetic post-baseline migration name (far-future
# timestamp so it never clashes with a real one).
_SYN = "29991231T235959_synthetic"
_SYN2 = "29991231T235960_synthetic-two"  # sorts after _SYN by name
# Synthetic orphan name (NOT in _V010_PRE_RESET_SET) — simulates a
# post-v0.1.0 re-baseline: an applied name whose file no longer exists.
SYN_ORPHAN = "20260815T000001_synthetic-orphan"


@contextmanager
def _throwaway_database(prefix: str) -> Generator[str, None, None]:
    base_url, _ = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"ava_test_{prefix}_{os.getpid()}_{int(time.time() * 1_000_000)}"
    url = f"{base_url}/{name}"
    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


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

    def _reseed(names: list[str]) -> None:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            _set_table_to(conn, "set", names)

    # Captured at setup, not at teardown: a test's monkeypatch of MIGRATIONS_DIR
    # is still active when this autouse fixture tears down, and the after-state
    # must be the real checkout's canonical applied set.
    _canonical_applied = sorted(required_migration_set())
    _reseed([_BASELINE_NAME])
    yield
    # Restore the canonical fully-applied state (baseline + every migration file),
    # NOT a hard-coded prefix list: a freshly folded strict migration must stay
    # in the canonical applied set, or a later cmd_migrations_apply in the same
    # worker re-runs it against an already-current schema and fails loudly
    # (DuplicateColumn — the class that hit PR #1587's migration).
    _reseed(_canonical_applied)


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
            expected_stamped = {(name,) for name in _schema_sql_stamped_migration_names()}
            row = conn.execute("SELECT name FROM schema_migrations").fetchall()
            assert set(row) == expected_stamped
            presets = conn.execute("SELECT name FROM agent_presets").fetchall()
            assert {r[0] for r in presets} == {
                "coder",
                "reviewer",
                "researcher",
                "orchestrator",
                "explorer",
            }
            default_model = conn.execute(
                "SELECT llm_model FROM cluster_defaults WHERE id = 1"
            ).fetchone()
            assert default_model == ("deepseek-v4-flash-vision-exp",)
        # Apply on the baselined DB: the folded migration marker makes the strict
        # ALTER skip a fresh schema that already carries the column; all other
        # post-baseline migrations replay cleanly, then a second apply is a no-op.
        with psycopg.connect(url) as conn:
            assert set(apply_pending_migrations(conn)) == required_migration_set() - set(
                _schema_sql_stamped_migration_names()
            )
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


def test_default_model_vision_migration_updates_only_prior_seed(
    db_conn: psycopg.Connection, cluster_defaults_unset: None
) -> None:
    """Upgrade the previous migration-owned flash default, never an API choice."""
    up = _DEFAULT_MODEL_VISION_MIGRATION.read_text()
    down = _DEFAULT_MODEL_VISION_MIGRATION.with_suffix(".down.sql").read_text()

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE cluster_defaults SET llm_model = 'deepseek-v4-flash', updated_by = 'migration' "
            "WHERE id = 1"
        )
        cur.execute(sql.SQL(cast(LiteralString, up)), prepare=False)
        cur.execute("SELECT llm_model, updated_by FROM cluster_defaults WHERE id = 1")
        assert cur.fetchone() == ("deepseek-v4-flash-vision-exp", "migration")

        cur.execute(sql.SQL(cast(LiteralString, down)), prepare=False)
        cur.execute("SELECT llm_model, updated_by FROM cluster_defaults WHERE id = 1")
        assert cur.fetchone() == ("deepseek-v4-flash", "migration")

        cur.execute(
            "UPDATE cluster_defaults SET llm_model = 'deepseek-v4-flash', updated_by = 'api' "
            "WHERE id = 1"
        )
        cur.execute(sql.SQL(cast(LiteralString, up)), prepare=False)
        cur.execute("SELECT llm_model, updated_by FROM cluster_defaults WHERE id = 1")
        assert cur.fetchone() == ("deepseek-v4-flash", "api")

        cur.execute(
            "UPDATE cluster_defaults SET llm_model = 'deepseek-v4-flash', updated_by = NULL "
            "WHERE id = 1"
        )
        cur.execute(sql.SQL(cast(LiteralString, up)), prepare=False)
        cur.execute("SELECT llm_model, updated_by FROM cluster_defaults WHERE id = 1")
        assert cur.fetchone() == ("deepseek-v4-flash-vision-exp", "migration")
    db_conn.commit()


def test_last_claim_loop_migration_fails_on_unrecorded_drift(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An existing DB with the column but no applied marker is a loud drift."""
    _set_table_to(db_conn, "set", [_BASELINE_NAME])
    up = tmp_path / _LAST_CLAIM_LOOP_MIGRATION.name
    down = tmp_path / _LAST_CLAIM_LOOP_MIGRATION.with_suffix(".down.sql").name
    up.write_text(_LAST_CLAIM_LOOP_MIGRATION.read_text())
    down.write_text(_LAST_CLAIM_LOOP_MIGRATION.with_suffix(".down.sql").read_text())
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)

    with psycopg.connect(settings.data_plane.db_url) as conn:
        with pytest.raises(MigrationFailed) as exc_info:
            apply_pending_migrations(conn)
        assert isinstance(exc_info.value.__cause__, psycopg.errors.DuplicateColumn)
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = %s",
            (_LAST_CLAIM_LOOP_MIGRATION_NAME,),
        ).fetchone()
        assert row is None


def _stage_snapshot_retirement_migration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Configure the real retirement migration as the only pending delta."""
    up = tmp_path / _SNAPSHOT_RETIREMENT_MIGRATION.name
    down = tmp_path / _SNAPSHOT_RETIREMENT_MIGRATION.with_suffix(".down.sql").name
    up.write_text(_SNAPSHOT_RETIREMENT_MIGRATION.read_text())
    down.write_text(_SNAPSHOT_RETIREMENT_MIGRATION.with_suffix(".down.sql").read_text())
    _init_repo(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)


def _create_retired_snapshot_tables(db_conn: psycopg.Connection) -> None:
    """Create the historical snapshot shapes required by the retirement migration."""
    db_conn.execute(
        "CREATE TABLE fork_lineage_fix_backfill_agents_meta (id BIGINT, spawner TEXT);"
        "CREATE TABLE fork_lineage_fix_backfill_events (id BIGINT, target_agent_id BIGINT);"
        "CREATE TABLE ledger_unpriced_backfill_20260824 (agent_id BIGINT);"
    )
    db_conn.commit()


def _drop_retired_snapshot_tables(db_conn: psycopg.Connection) -> None:
    """Remove test-only snapshot tables preserved by a failed retirement."""
    db_conn.execute(
        "DROP TABLE IF EXISTS public.fork_lineage_fix_backfill_agents_meta;"
        "DROP TABLE IF EXISTS public.fork_lineage_fix_backfill_events;"
        "DROP TABLE IF EXISTS public.ledger_unpriced_backfill_20260824;"
    )
    db_conn.commit()


def test_snapshot_retirement_migration_drops_empty_snapshots(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty rollback snapshots may be retired and the migration records its apply."""
    _set_table_to(db_conn, "set", [_BASELINE_NAME])
    _stage_snapshot_retirement_migration(monkeypatch, tmp_path)
    _create_retired_snapshot_tables(db_conn)

    with psycopg.connect(settings.data_plane.db_url) as conn:
        assert apply_pending_migrations(conn) == [_SNAPSHOT_RETIREMENT_MIGRATION_NAME]
        for table in _RETIRED_SNAPSHOT_TABLES:
            assert conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone() == (
                None,
            )


def test_snapshot_retirement_migration_rejects_nonempty_snapshots(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A populated correction snapshot must fail before any snapshot is dropped."""
    _set_table_to(db_conn, "set", [_BASELINE_NAME])
    _stage_snapshot_retirement_migration(monkeypatch, tmp_path)
    _create_retired_snapshot_tables(db_conn)
    db_conn.execute("INSERT INTO ledger_unpriced_backfill_20260824 (agent_id) VALUES (1)")
    db_conn.commit()

    try:
        with psycopg.connect(settings.data_plane.db_url) as conn:
            with pytest.raises(MigrationFailed) as exc_info:
                apply_pending_migrations(conn)
            assert isinstance(exc_info.value.__cause__, psycopg.errors.RaiseException)
            assert "must be archived before retirement" in str(exc_info.value.__cause__)
            for table in _RETIRED_SNAPSHOT_TABLES:
                assert conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone() == (
                    f"{table}",
                )
            row = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = %s",
                (_SNAPSHOT_RETIREMENT_MIGRATION_NAME,),
            ).fetchone()
            assert row is None
    finally:
        _drop_retired_snapshot_tables(db_conn)


def test_snapshot_retirement_migration_checks_public_snapshots_with_shadowed_search_path(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A shadow schema cannot hide a populated public correction snapshot."""
    _set_table_to(db_conn, "set", [_BASELINE_NAME])
    _stage_snapshot_retirement_migration(monkeypatch, tmp_path)
    _create_retired_snapshot_tables(db_conn)
    db_conn.execute("CREATE SCHEMA snapshot_shadow")
    db_conn.execute(
        "CREATE TABLE snapshot_shadow.fork_lineage_fix_backfill_agents_meta "
        "(id BIGINT, spawner TEXT);"
        "CREATE TABLE snapshot_shadow.fork_lineage_fix_backfill_events "
        "(id BIGINT, target_agent_id BIGINT);"
        "CREATE TABLE snapshot_shadow.ledger_unpriced_backfill_20260824 (agent_id BIGINT);"
        "INSERT INTO public.ledger_unpriced_backfill_20260824 (agent_id) VALUES (1);"
    )
    db_conn.commit()

    try:
        with psycopg.connect(settings.data_plane.db_url) as conn:
            conn.execute("SET search_path TO snapshot_shadow, public")
            with pytest.raises(MigrationFailed) as exc_info:
                apply_pending_migrations(conn)
            assert isinstance(exc_info.value.__cause__, psycopg.errors.RaiseException)
            assert "ledger_unpriced_backfill_20260824 must be archived before retirement" in str(
                exc_info.value.__cause__
            )
    finally:
        db_conn.execute("DROP SCHEMA snapshot_shadow CASCADE")
        db_conn.commit()
        _drop_retired_snapshot_tables(db_conn)


def test_snapshot_retirement_migration_rechecks_after_a_concurrent_snapshot_write(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A writer committed during retirement makes the migration fail without a drop."""
    _set_table_to(db_conn, "set", [_BASELINE_NAME])
    _stage_snapshot_retirement_migration(monkeypatch, tmp_path)
    _create_retired_snapshot_tables(db_conn)
    relation = db_conn.execute(
        "SELECT 'public.ledger_unpriced_backfill_20260824'::regclass::oid"
    ).fetchone()
    assert relation is not None

    writer = psycopg.connect(settings.data_plane.db_url)
    results: Queue[object] = Queue()

    def _apply() -> None:
        with psycopg.connect(settings.data_plane.db_url) as conn:
            try:
                results.put(apply_pending_migrations(conn))
            except BaseException as exc:  # pass the migration outcome to the test thread
                results.put(exc)

    try:
        writer.execute("INSERT INTO public.ledger_unpriced_backfill_20260824 (agent_id) VALUES (1)")
        worker = Thread(target=_apply)
        worker.start()
        try:
            for _ in range(100):
                waiting = db_conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE relation = %s AND NOT granted)",
                    (relation[0],),
                ).fetchone()
                assert waiting is not None
                if waiting[0]:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("migration did not wait for the concurrent snapshot writer")
        finally:
            writer.commit()
            writer.close()

        worker.join(timeout=10)
        assert not worker.is_alive()
        result = results.get_nowait()
        assert isinstance(result, MigrationFailed)
        assert isinstance(result.__cause__, psycopg.errors.RaiseException)
        assert db_conn.execute(
            "SELECT to_regclass('public.ledger_unpriced_backfill_20260824')"
        ).fetchone() == ("ledger_unpriced_backfill_20260824",)
        assert (
            db_conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = %s",
                (_SNAPSHOT_RETIREMENT_MIGRATION_NAME,),
            ).fetchone()
            is None
        )
    finally:
        _drop_retired_snapshot_tables(db_conn)


def test_force_terminate_fence_migration_backfills_current_death_intent() -> None:
    """Upgrade preserves only current-death force evidence.

    A marker after the current death is the exact fence; without one, every
    inbound already present is conservatively fenced. Historical markers from a
    prior death and rows outside user-terminated state must not be mistaken for
    current force intent.
    """
    with (
        _throwaway_database("force_fence") as url,
        psycopg.connect(url, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql.SQL(cast(LiteralString, _SCHEMA_SQL.read_text())), prepare=False)
        cur.execute("ALTER TABLE agents_meta DROP COLUMN last_force_terminate_inbound_id")
        cur.execute("INSERT INTO agents (id) SELECT generate_series(1, 6)")
        cur.execute(
            "INSERT INTO agents_meta "
            "(id, status, termination_source, status_changed_at) VALUES "
            "(1, 'terminated', 'user', '2026-08-21 10:00:00+00'), "
            "(2, 'terminated', 'user', '2026-08-21 10:00:00+00'), "
            "(3, 'terminated', 'user', '2026-08-21 10:00:00+00'), "
            "(4, 'terminated', 'reaper', '2026-08-21 10:00:00+00'), "
            "(5, 'idling', 'user', '2026-08-21 10:00:00+00'), "
            "(6, 'terminated', 'user', '2026-08-21 10:00:00+00')"
        )

        def _inbound(agent_id: int, kind: str, created_at: str) -> int:
            cur.execute(
                "INSERT INTO inbound_messages "
                "(agent_id, content, kind, source, created_at) "
                "VALUES (%s, '', %s, 'user', %s) RETURNING id",
                (agent_id, kind, created_at),
            )
            row = cur.fetchone()
            assert row is not None
            return row[0]

        current_marker = _inbound(1, "terminate", "2026-08-21 10:01:00+00")
        post_force_chat = _inbound(1, "chat", "2026-08-21 10:02:00+00")
        historical_marker = _inbound(2, "terminate", "2026-08-21 09:00:00+00")
        no_current_marker_chat = _inbound(2, "chat", "2026-08-21 10:01:00+00")
        no_marker_chat = _inbound(3, "chat", "2026-08-21 10:01:00+00")
        _inbound(4, "terminate", "2026-08-21 10:01:00+00")
        _inbound(5, "terminate", "2026-08-21 10:01:00+00")
        pre_force_chat = _inbound(6, "chat", "2026-08-21 10:01:00+00")
        current_marker_after_chat = _inbound(6, "terminate", "2026-08-21 10:02:00+00")

        cur.execute(
            sql.SQL(cast(LiteralString, _FORCE_FENCE_MIGRATION.read_text())),
            prepare=False,
        )
        cur.execute("SELECT id, last_force_terminate_inbound_id FROM agents_meta ORDER BY id")
        fences = dict(cur.fetchall())

    assert fences == {
        1: current_marker,
        2: no_current_marker_chat,
        3: no_marker_chat,
        4: None,
        5: None,
        6: current_marker_after_chat,
    }
    assert post_force_chat > current_marker
    assert no_current_marker_chat > historical_marker
    assert pre_force_chat < current_marker_after_chat


_SKILL_MATCH_CLEANUP_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "20260827T165000_drop-skill-match-config-keys.sql"
)
_SKILL_MATCH_KEYS = (
    "skill_match_enabled",
    "skill_match_top_k",
    "skill_match_min_score",
    "skill_match_budget_ms",
)


def test_skill_match_config_cleanup_migration_strips_residue() -> None:
    """The deleted skill matcher's config keys are removed from every persisted
    agent-configuration home; unrelated keys and NULL rows survive.

    The matcher was removed per user ruling 2026-08-27 but a later merge
    accidentally restored it, so stored overlays on deployed clusters still
    carry the keys. The overlay resolver rejects unknown keys, so leftover
    keys would break agent boot once the fields are unregistered — this
    migration deletes them (user report 2026-08-28).
    """
    residue = {
        "llm_model": "claude-opus-4-8",
        "skill_match_enabled": True,
        "skill_match_top_k": 3,
        "skill_match_min_score": 0.35,
        "skill_match_budget_ms": 300,
    }
    with (
        _throwaway_database("skill_match_cleanup") as url,
        psycopg.connect(url, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql.SQL(cast(LiteralString, _SCHEMA_SQL.read_text())), prepare=False)
        cur.execute("INSERT INTO agents (id) SELECT generate_series(1, 4)")
        cur.execute(
            "INSERT INTO agents_meta (id, status, config_overlay, birth_config) VALUES "
            "(1, 'idling', %s::jsonb, NULL), "  # every residue key + an unrelated key
            "(2, 'idling', %s::jsonb, NULL), "  # no residue key — untouched
            "(3, 'idling', NULL, NULL), "  # both columns NULL — stay NULL
            "(4, 'idling', NULL, %s::jsonb)",  # residue key in birth_config
            (
                json.dumps(residue),
                json.dumps({"llm_model": "claude-opus-4-8"}),
                json.dumps({"syntax_fix_ruff_format": True, "skill_match_enabled": True}),
            ),
        )
        cur.execute(
            "INSERT INTO agent_presets (name, label, config) VALUES (%s, %s, %s::jsonb)",
            ("residue", "Residue", json.dumps(residue)),
        )

        cur.execute(
            sql.SQL(cast(LiteralString, _SKILL_MATCH_CLEANUP_MIGRATION.read_text())),
            prepare=False,
        )

        cur.execute("SELECT id, config_overlay, birth_config FROM agents_meta ORDER BY id")
        overlays: list[Any] = []
        births: list[Any] = []
        for row in cur.fetchall():
            overlays.append(row[1])
            births.append(row[2])
        cur.execute("SELECT config FROM agent_presets WHERE name = 'residue'")
        row = cur.fetchone()
        assert row is not None
        preset = row[0]
        cur.execute("SELECT config_overlay FROM agents_meta WHERE id = 1")
        row = cur.fetchone()
        assert row is not None
        cleaned = row[0]

    assert overlays[0] == {"llm_model": "claude-opus-4-8"}
    assert all(k not in overlays[0] for k in _SKILL_MATCH_KEYS)
    assert overlays[1] == {"llm_model": "claude-opus-4-8"}
    assert overlays[2] is None
    assert births[0] is None
    assert births[1] is None
    assert births[2] is None
    assert births[3] == {"syntax_fix_ruff_format": True}
    assert preset == {"llm_model": "claude-opus-4-8"}
    assert cleaned == {"llm_model": "claude-opus-4-8"}


def test_skill_match_config_cleanup_migration_is_idempotent() -> None:
    """A second application changes nothing (the WHERE clauses already skipped
    the cleaned rows), and the no-op down runs without error."""
    with (
        _throwaway_database("skill_match_cleanup2") as url,
        psycopg.connect(url, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql.SQL(cast(LiteralString, _SCHEMA_SQL.read_text())), prepare=False)
        cur.execute("INSERT INTO agents (id) VALUES (1)")
        cur.execute(
            "INSERT INTO agents_meta (id, status, config_overlay) VALUES (1, 'idling', %s::jsonb)",
            (json.dumps({"skill_match_enabled": True, "llm_model": "x"}),),
        )
        migration = _SKILL_MATCH_CLEANUP_MIGRATION.read_text()
        cur.execute(sql.SQL(cast(LiteralString, migration)), prepare=False)
        cur.execute(sql.SQL(cast(LiteralString, migration)), prepare=False)
        down = _SKILL_MATCH_CLEANUP_MIGRATION.with_suffix(".down.sql").read_text()
        cur.execute(sql.SQL(cast(LiteralString, down)), prepare=False)
        cur.execute("SELECT config_overlay FROM agents_meta WHERE id = 1")
        row = cur.fetchone()
        assert row is not None
        overlay = row[0]

    assert overlay == {"llm_model": "x"}


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


def test_apply_pending_applies_post_baseline(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    def test_dir_missing_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path / "nope")
        with pytest.raises(MigrationLayoutError, match="does not exist"):
            _list_migration_files()

    def test_bad_filename_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / f"{_SYN}.sql").write_text("-- noop")
        (tmp_path / "0001_legacy.sql").write_text("-- noop")  # old integer format
        _init_repo(tmp_path)  # tracked: layout validation applies to git-tracked files
        monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
        with pytest.raises(MigrationLayoutError, match="does not match"):
            _list_migration_files()

    def test_readme_and_down_and_dotfiles_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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


def _git(repo: Path, *args: str) -> None:
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
    def _init_repo(self, repo: Path, names: list[str]) -> None:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        (repo / "migrations").mkdir()
        for name in names:
            (repo / "migrations" / name).write_text("-- noop\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")

    def test_good_ref_passes(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path, [f"{_SYN}.sql", f"{_SYN2}.sql"])
        validate_migrations_at_ref("HEAD", repo_root=tmp_path)

    def test_duplicate_in_ref_raises(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path, [f"{_SYN}.sql", "sub_dir_placeholder.sql"])
        # a malformed name at the ref is refused before any service is stopped
        with pytest.raises(MigrationLayoutError, match="does not match"):
            validate_migrations_at_ref("HEAD", repo_root=tmp_path)

    def test_unreadable_ref_raises(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path, [f"{_SYN}.sql"])
        with pytest.raises(MigrationLayoutError, match="cannot read migrations/"):
            validate_migrations_at_ref("no-such-ref", repo_root=tmp_path)


def test_apply_multi_statement_migration_over_prepared_conn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_apply_down_round_trip(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / f"{_SYN}.down.sql").write_text("DROP TABLE dtest_t;")
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with db_conn.cursor() as cur:
        cur.execute("CREATE TABLE dtest_t (id int)")
        cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (_SYN,))
    db_conn.commit()

    apply_down(db_conn, _SYN)

    with db_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('dtest_t')")
        dropped = cur.fetchone()
        assert dropped is not None and dropped[0] is None  # table dropped
        cur.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (_SYN,))
        assert cur.fetchone() is None  # row removed


def test_apply_down_missing_down_raises(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)  # no .down.sql
    with pytest.raises(MigrationLayoutError):
        _down_path(_SYN)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (_SYN,))
    db_conn.commit()
    with pytest.raises(MigrationLayoutError):
        apply_down(db_conn, _SYN)


def test_apply_down_atomic_on_failure(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_rollback_to_descends(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_rollback_to_aborts_all_downs_on_failure(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failing down leaves every migration row and schema object unchanged."""
    (tmp_path / f"{_SYN2}.down.sql").write_text("DROP TABLE rollback_atomic_second_t;")
    (tmp_path / f"{_SYN}.down.sql").write_text("DROP TABLE definitely_missing_rollback_atomic_t;")
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with db_conn.cursor() as cur:
        cur.execute("CREATE TABLE rollback_atomic_first_t (id int)")
        cur.execute("CREATE TABLE rollback_atomic_second_t (id int)")
        for stem in (_SYN, _SYN2):
            cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (stem,))
    db_conn.commit()

    try:
        with pytest.raises(MigrationFailed):
            rollback_to(db_conn, {_BASELINE_NAME})

        with db_conn.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(_BASELINE_NAME,), (_SYN,), (_SYN2,)]
            cur.execute("SELECT to_regclass('rollback_atomic_first_t')")
            assert cur.fetchone() == ("rollback_atomic_first_t",)
            cur.execute("SELECT to_regclass('rollback_atomic_second_t')")
            assert cur.fetchone() == ("rollback_atomic_second_t",)
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS rollback_atomic_first_t")
            cur.execute("DROP TABLE IF EXISTS rollback_atomic_second_t")
            cur.execute("DELETE FROM schema_migrations WHERE name IN (%s, %s)", (_SYN, _SYN2))
        db_conn.commit()


def test_rollback_to_can_raise_after_the_batch_commits(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A post-yield advisory-unlock failure arrives after the rollback batch
    commits. Callers must treat an unexpected exception as schema-ambiguous,
    never as proof that the schema stayed unchanged."""
    (tmp_path / f"{_SYN}.down.sql").write_text("DROP TABLE rollback_committed_t;")
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
    with db_conn.cursor() as cur:
        cur.execute("CREATE TABLE rollback_committed_t (id int)")
        cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (_SYN,))
    db_conn.commit()

    @contextmanager
    def _fail_unlock_after_yield(_conn: psycopg.Connection) -> Generator[None, None, None]:
        yield
        raise RuntimeError("advisory unlock failed after commit")

    monkeypatch.setattr("shared.migrations._schema_mutation_lock", _fail_unlock_after_yield)
    try:
        with pytest.raises(RuntimeError, match="unlock failed after commit"):
            rollback_to(db_conn, {_BASELINE_NAME})

        with db_conn.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations WHERE name = %s", (_SYN,))
            assert cur.fetchone() is None
            cur.execute("SELECT to_regclass('rollback_committed_t')")
            assert cur.fetchone() == (None,)
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS rollback_committed_t")
            cur.execute("DELETE FROM schema_migrations WHERE name = %s", (_SYN,))
        db_conn.commit()


# ─── migration-apply advisory lock (serializes concurrent appliers) ───────────


def _try_lock_from_other_conn() -> bool:
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
        row = cur.fetchone()
        assert row is not None
        got = row[0]
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


def test_rollback_to_holds_the_lock(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_apply_pending_squashes_orphaned_applied_names(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_apply_pending_squash_then_apply_pending(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_squash_does_not_touch_baseline_or_pending(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_squash_authority_checked_even_without_pending(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_squash_logs_the_converged_names(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loguru_records: list[dict[str, Any]],
) -> None:
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


def test_squash_refuses_partial_pre_reset_history(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
            surviving = cur.fetchone()
            assert surviving is not None and surviving[0] == 1 + len(partial)
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            for name in partial:
                c.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))


def test_squash_converges_full_pre_reset_history(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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


# ─── root-task-ongoing migration: legacy-schema execution ────────────────────


_ROOT_ONGOING_UP = (
    Path(__file__).resolve().parents[2] / "migrations" / "20260827T021440_root-task-ongoing.sql"
)
_ROOT_ONGOING_DOWN = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "20260827T021440_root-task-ongoing.down.sql"
)

# The pre-ruling agent_tasks shape: 4-value status CHECK (auto-named
# agent_tasks_status_check) and the root seeded as 'in_progress'. Minimal but
# faithful — the migration only touches agent_tasks, so the scratch DB needs no
# other tables (owner FK references agents, created inline).
_LEGACY_AGENT_TASKS = """
CREATE TABLE agents (id BIGSERIAL PRIMARY KEY, label TEXT);
CREATE TABLE agent_tasks (
    id          BIGSERIAL PRIMARY KEY,
    parent_id   BIGINT REFERENCES agent_tasks(id),
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    results     TEXT,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'in_progress', 'done', 'cancelled')),
    priority    TEXT NOT NULL DEFAULT 'P2'
                CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    owner       BIGINT REFERENCES agents(id),
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_root     BOOLEAN NOT NULL DEFAULT FALSE,
    remind_interval_seconds INTEGER DEFAULT 1800,
    last_reminded_at TIMESTAMPTZ,
    reminder_count INTEGER NOT NULL DEFAULT 0
);
INSERT INTO agents (id, label) VALUES (1, 'agent');
INSERT INTO agent_tasks (id, title, description, status, created_by, is_root, owner)
VALUES (1, 'Root', 'root', 'in_progress', 'system', TRUE, NULL),
       (2, 'regular', 'regular task', 'in_progress', '1', FALSE, 1);
"""


def _run_sql(url: str, sql_body: str) -> None:
    """Execute a multi-statement SQL body (a migration file) in one transaction."""
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL(cast(LiteralString, sql_body)), prepare=False)
        conn.commit()


def _agent_tasks_shape(url: str) -> tuple[str, str, str | None]:
    """Return (root_status, status_constraint_sql, root_pin_sql) from the scratch DB."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM agent_tasks WHERE is_root")
        root_row = cur.fetchone()
        assert root_row is not None
        cur.execute(
            """SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conrelid = 'agent_tasks'::regclass
                 AND conname = 'agent_tasks_status_check'"""
        )
        status_row = cur.fetchone()
        assert status_row is not None
        cur.execute(
            """SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conrelid = 'agent_tasks'::regclass
                 AND conname = 'agent_tasks_root_status_ongoing'"""
        )
        root_pin = cur.fetchone()
    return root_row[0], status_row[0], root_pin[0] if root_pin else None


def test_root_task_ongoing_migration_upgrades_legacy_schema() -> None:
    """The migration runs on a PRE-ruling cluster: old 4-value CHECK + root
    'in_progress'. Regression for the adversarial-review finding (PR #746) that
    the original body UPDATE'd the root before dropping the old CHECK, aborting
    `ava cluster update` on every existing cluster (migration smoke only
    replays on a fresh schema.sql, so CI could not see it).

    Asserts the full legacy -> new shape transition, including the
    bidirectional root pin (a direct UPDATE on a non-root row is rejected by
    the DB, not only by API guards)."""
    with _throwaway_database("root-ongoing-up") as url:
        _run_sql(url, _LEGACY_AGENT_TASKS)
        # Precondition: legacy shape.
        root_status, status_check, root_pin = _agent_tasks_shape(url)
        assert root_status == "in_progress"
        assert "cancelled" in status_check and "ongoing" not in status_check
        assert root_pin is None

        # Apply the UP migration — must succeed on the legacy shape.
        _run_sql(url, _ROOT_ONGOING_UP.read_text())

        # Root moved to 'ongoing'; CHECK widened; bidirectional pin in place.
        root_status, status_check, root_pin = _agent_tasks_shape(url)
        assert root_status == "ongoing"
        assert "ongoing" in status_check
        # Bidirectional pin: root must be ongoing AND non-root must not be.
        assert root_pin is not None
        assert "is_root" in root_pin and "status = 'ongoing'" in root_pin
        assert "NOT is_root" in root_pin and "status <> 'ongoing'" in root_pin

        # Bidirectional pin: a non-root row cannot be direct-UPDATE'd to
        # 'ongoing' (DB-level, no API involved).
        with (
            psycopg.connect(url) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg.errors.CheckViolation),
        ):
            cur.execute(
                """UPDATE agent_tasks SET status = 'ongoing'
                       WHERE id = 2"""
            )

        # The root itself cannot be moved off 'ongoing' either.
        with (
            psycopg.connect(url) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg.errors.CheckViolation),
        ):
            cur.execute(
                """UPDATE agent_tasks SET status = 'done'
                       WHERE id = 1"""
            )


def test_root_task_ongoing_migration_down_restores_legacy_schema() -> None:
    """The DOWN migration restores the pre-ruling shape on a cluster that ran
    the UP: root back to 'in_progress', 4-value CHECK back, root pin gone.
    Regression for the finding that the original down ADD'd the 4-value CHECK
    while the root was still 'ongoing' — ADD CONSTRAINT validates existing rows
    and aborted the rollback."""
    with _throwaway_database("root-ongoing-down") as url:
        _run_sql(url, _LEGACY_AGENT_TASKS)
        _run_sql(url, _ROOT_ONGOING_UP.read_text())
        # Sanity: we are on the new shape before rolling back.
        root_status, _, _ = _agent_tasks_shape(url)
        assert root_status == "ongoing"

        _run_sql(url, _ROOT_ONGOING_DOWN.read_text())

        root_status, status_check, root_pin = _agent_tasks_shape(url)
        assert root_status == "in_progress"
        assert "cancelled" in status_check and "ongoing" not in status_check
        assert root_pin is None

        # 'ongoing' is no longer a legal value at all.
        with (
            psycopg.connect(url) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg.errors.CheckViolation),
        ):
            cur.execute(
                """UPDATE agent_tasks SET status = 'ongoing'
                       WHERE id = 2"""
            )


# ─── drop-task-open-status migration: legacy-schema execution ────────────────


_DROP_OPEN_UP = (
    Path(__file__).resolve().parents[2] / "migrations" / "20260829T090700_drop-task-open-status.sql"
)
_DROP_OPEN_DOWN = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "20260829T090700_drop-task-open-status.down.sql"
)

# The pre-ruling agent_tasks shape: 5-value status CHECK (auto-named
# agent_tasks_status_check), DEFAULT 'open', the root pinned 'ongoing', and the
# old partial unique title index. Minimal but faithful — the migration only
# touches agent_tasks, so the scratch DB needs no other tables (owner FK
# references agents, created inline). One 'open' row is seeded so the data
# migration is exercised.
_LEGACY_OPEN_AGENT_TASKS = """
CREATE TABLE agents (id BIGSERIAL PRIMARY KEY, label TEXT);
CREATE TABLE agent_tasks (
    id          BIGSERIAL PRIMARY KEY,
    parent_id   BIGINT REFERENCES agent_tasks(id),
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    results     TEXT,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'in_progress', 'done', 'cancelled', 'ongoing')),
    priority    TEXT NOT NULL DEFAULT 'P2',
    owner       BIGINT REFERENCES agents(id),
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_root     BOOLEAN NOT NULL DEFAULT FALSE,
    remind_interval_seconds INTEGER DEFAULT 1800,
    last_reminded_at TIMESTAMPTZ,
    reminder_count INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE agent_tasks ADD CONSTRAINT agent_tasks_root_status_ongoing
    CHECK ((is_root AND status = 'ongoing') OR (NOT is_root AND status <> 'ongoing'));
CREATE UNIQUE INDEX agent_tasks_title_unique_open
    ON agent_tasks (title) WHERE status IN ('open', 'in_progress');
INSERT INTO agents (id, label) VALUES (1, 'agent');
INSERT INTO agent_tasks (id, title, description, status, created_by, is_root, owner)
VALUES (1, 'Root', 'root', 'ongoing', 'system', TRUE, NULL),
       (2, 'open task', 'open', 'open', '1', FALSE, 1),
       (3, 'active task', 'active', 'in_progress', '1', FALSE, 1),
       (4, 'done task', 'done', 'done', '1', FALSE, 1);
-- The explicit ids above do not advance the BIGSERIAL sequence; the tests
-- insert without ids afterwards, so the sequence must be set to the max.
SELECT setval(pg_get_serial_sequence('agent_tasks', 'id'),
              (SELECT max(id) FROM agent_tasks));
"""


def _drop_open_shape(url: str) -> tuple[str, str, str, set[str]]:
    """Return (status_default, status_check_sql, root_status, index_names)."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'agent_tasks' AND column_name = 'status'"
        )
        default_row = cur.fetchone()
        assert default_row is not None
        cur.execute(
            """SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conrelid = 'agent_tasks'::regclass
                 AND conname = 'agent_tasks_status_check'"""
        )
        status_row = cur.fetchone()
        assert status_row is not None
        cur.execute("SELECT status FROM agent_tasks WHERE is_root")
        root_row = cur.fetchone()
        assert root_row is not None
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'agent_tasks' "
            "AND indexname IN ('agent_tasks_title_unique_open', "
            "'agent_tasks_title_unique_in_progress')"
        )
        index_rows = cur.fetchall()
    return (
        default_row[0],
        status_row[0],
        root_row[0],
        {r[0] for r in index_rows},
    )


def test_drop_task_open_status_migration_upgrades_legacy_schema() -> None:
    """The migration runs on a pre-ruling cluster: 5-value CHECK + DEFAULT
    'open' + old unique index + live 'open' rows. Asserts the full legacy ->
    new shape transition: rows migrated to 'in_progress', default flipped,
    CHECK narrowed, unique index recreated under the new name, root untouched."""
    with _throwaway_database("drop-open-up") as url:
        _run_sql(url, _LEGACY_OPEN_AGENT_TASKS)
        # Precondition: legacy shape.
        default, status_check, root_status, indexes = _drop_open_shape(url)
        assert default == "'open'::text"
        assert "'open'" in status_check and "'ongoing'" in status_check
        assert root_status == "ongoing"
        assert indexes == {"agent_tasks_title_unique_open"}

        # Apply the UP migration — must succeed on the legacy shape.
        _run_sql(url, _DROP_OPEN_UP.read_text())

        default, status_check, root_status, indexes = _drop_open_shape(url)
        assert default == "'in_progress'::text"
        assert "'open'" not in status_check and "'ongoing'" in status_check
        assert root_status == "ongoing"  # the root pin is untouched
        assert indexes == {"agent_tasks_title_unique_in_progress"}

        # Every former 'open' row now reads 'in_progress'; non-open rows keep
        # their statuses.
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, status FROM agent_tasks ORDER BY id")
            rows = cur.fetchall()
        assert rows == [
            (1, "ongoing"),
            (2, "in_progress"),
            (3, "in_progress"),
            (4, "done"),
        ]

        # 'open' is no longer a legal value at all.
        with (
            psycopg.connect(url) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg.errors.CheckViolation),
        ):
            cur.execute(
                """INSERT INTO agent_tasks (title, description, status, created_by, owner)
                       VALUES ('new', 'd', 'open', '1', 1)"""
            )

        # The narrowed unique index still backstops duplicate in_progress titles.
        with (
            psycopg.connect(url) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg.errors.UniqueViolation),
        ):
            cur.execute(
                """INSERT INTO agent_tasks (title, description, status, created_by, owner)
                       VALUES ('active task', 'd', 'in_progress', '1', 1)"""
            )


def test_drop_task_open_status_migration_down_restores_legacy_schema() -> None:
    """The DOWN migration restores the pre-ruling shape on a cluster that ran
    the UP: CHECK widened back, DEFAULT 'open', old unique index back. Migrated
    rows stay 'in_progress' — nothing distinguishes them from tasks born
    'in_progress', so the down restores the vocabulary, not the data."""
    with _throwaway_database("drop-open-down") as url:
        _run_sql(url, _LEGACY_OPEN_AGENT_TASKS)
        _run_sql(url, _DROP_OPEN_UP.read_text())
        # Sanity: we are on the new shape before rolling back.
        default, _, _, indexes = _drop_open_shape(url)
        assert default == "'in_progress'::text"
        assert indexes == {"agent_tasks_title_unique_in_progress"}

        _run_sql(url, _DROP_OPEN_DOWN.read_text())

        default, status_check, root_status, indexes = _drop_open_shape(url)
        assert default == "'open'::text"
        assert "'open'" in status_check and "'ongoing'" in status_check
        assert root_status == "ongoing"
        assert indexes == {"agent_tasks_title_unique_open"}

        # 'open' is legal again, and the old index backstops it too.
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_tasks (title, description, status, created_by, owner)
                       VALUES ('fresh', 'd', 'open', '1', 1)"""
            )
        with (
            psycopg.connect(url) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg.errors.UniqueViolation),
        ):
            cur.execute(
                """INSERT INTO agent_tasks (title, description, status, created_by, owner)
                       VALUES ('fresh', 'd', 'in_progress', '1', 1)"""
            )


# ─── allow-non-root-ongoing migration: current-schema execution ─────────────


_ALLOW_NON_ROOT_ONGOING_UP = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "20260901T181810_allow-non-root-ongoing.sql"
)
_ALLOW_NON_ROOT_ONGOING_DOWN = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "20260901T181810_allow-non-root-ongoing.down.sql"
)

_BIDIRECTIONAL_ONGOING_AGENT_TASKS = """
CREATE TABLE agent_tasks (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'in_progress'
        CHECK (status IN ('in_progress', 'done', 'cancelled', 'ongoing')),
    is_root BOOLEAN NOT NULL DEFAULT FALSE
);
ALTER TABLE agent_tasks ADD CONSTRAINT agent_tasks_root_status_ongoing
    CHECK ((is_root AND status = 'ongoing') OR (NOT is_root AND status <> 'ongoing'));
INSERT INTO agent_tasks (id, status, is_root) VALUES
    (1, 'ongoing', TRUE),
    (2, 'in_progress', FALSE);
"""


def test_allow_non_root_ongoing_migration_permits_regular_ongoing_tasks() -> None:
    """The new root pin permits ongoing regular tasks but keeps the root pinned."""
    with _throwaway_database("allow-non-root-ongoing-up") as url:
        _run_sql(url, _BIDIRECTIONAL_ONGOING_AGENT_TASKS)
        _run_sql(url, _ALLOW_NON_ROOT_ONGOING_UP.read_text())

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("UPDATE agent_tasks SET status = 'ongoing' WHERE id = 2")
            cur.execute("SELECT status FROM agent_tasks WHERE id = 2")
            assert cur.fetchone() == ("ongoing",)

        with (
            psycopg.connect(url) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg.errors.CheckViolation),
        ):
            cur.execute("UPDATE agent_tasks SET status = 'done' WHERE id = 1")


def test_allow_non_root_ongoing_migration_down_restores_bidirectional_pin() -> None:
    """Rollback resets regular ongoing tasks before restoring the old CHECK."""
    with _throwaway_database("allow-non-root-ongoing-down") as url:
        _run_sql(url, _BIDIRECTIONAL_ONGOING_AGENT_TASKS)
        _run_sql(url, _ALLOW_NON_ROOT_ONGOING_UP.read_text())
        _run_sql(url, "UPDATE agent_tasks SET status = 'ongoing' WHERE id = 2")
        _run_sql(url, _ALLOW_NON_ROOT_ONGOING_DOWN.read_text())

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM agent_tasks WHERE id = 2")
            assert cur.fetchone() == ("in_progress",)

        with (
            psycopg.connect(url) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg.errors.CheckViolation),
        ):
            cur.execute("UPDATE agent_tasks SET status = 'ongoing' WHERE id = 2")


# ─── failure-feedback migration: additive up/down ────────────────────────────


def test_failure_feedback_migration_adds_trust_facts_and_event_store() -> None:
    with _throwaway_database("failure-feedback-up") as url:
        _run_sql(url, "CREATE TABLE inbound_messages (id BIGSERIAL PRIMARY KEY)")
        _run_sql(url, _FAILURE_FEEDBACK_UP.read_text())

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'inbound_messages' AND column_name LIKE 'source_%' "
                "OR table_name = 'inbound_messages' AND column_name = 'content_hash'"
            )
            assert {row[0] for row in cur.fetchall()} == {
                "source_assertion_match",
                "source_transport",
                "source_verified_by",
                "content_hash",
            }
            cur.execute(
                "INSERT INTO work_failed_events "
                "(repo, ref, commit_sha, stage, summary, author_agent_id, dedup_key) "
                "VALUES ('Ava', 'main', 'abc', 'qa', 'failed', 1, 'same')"
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    "INSERT INTO work_failed_events "
                    "(repo, ref, commit_sha, stage, summary, author_agent_id, dedup_key) "
                    "VALUES ('Ava', 'main', 'abc', 'qa', 'failed', 1, 'same')"
                )


def test_failure_feedback_migration_down_removes_additive_schema() -> None:
    with _throwaway_database("failure-feedback-down") as url:
        _run_sql(url, "CREATE TABLE inbound_messages (id BIGSERIAL PRIMARY KEY)")
        _run_sql(url, _FAILURE_FEEDBACK_UP.read_text())
        _run_sql(url, _FAILURE_FEEDBACK_DOWN.read_text())

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('work_failed_events')")
            assert cur.fetchone() == (None,)
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'inbound_messages' "
                "AND column_name IN ('source_verified_by', 'source_transport', "
                "'content_hash', 'source_assertion_match')"
            )
            assert cur.fetchall() == []
