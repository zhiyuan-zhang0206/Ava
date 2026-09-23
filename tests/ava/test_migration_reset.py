"""Schema reset convergence and partial-history protection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from shared.config import settings
from shared.migrations import (
    _BASELINE_NAME,
    apply_pending_migrations,
)
from tests.ava.migration_support import (
    _SYN,
    SYN_ORPHAN,
    _init_repo,
)
from tests.ava.migration_support import (
    _reset_schema_migrations_state as _reset_schema_migrations_state,
)


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
    baseline carries the net effect of all 59, so deleting their tracking rows
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
    deletes it like any other orphan — the frozen set only gates the 59."""
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


@pytest.mark.parametrize("applied_count", [0, 1, 100])
def test_current_reset_refuses_missing_history_without_mutation(
    db_conn: psycopg.Connection, applied_count: int
) -> None:
    from shared.migration_history import _PRE_RESET_SET
    from shared.migrations import MigrationHistoryGap, applied_migration_names
    from tests.ava.migration_support import _set_table_to

    assert len(_PRE_RESET_SET) == 101
    before = {_BASELINE_NAME, *sorted(_PRE_RESET_SET)[:applied_count]}
    _set_table_to(db_conn, "set", before)
    with psycopg.connect(settings.data_plane.db_url) as conn:
        with pytest.raises(MigrationHistoryGap, match="2026-09-23"):
            apply_pending_migrations(conn)
        assert applied_migration_names(conn) == before


def test_current_reset_converges_complete_history_and_refuses_cross_floor_rollback(
    db_conn: psycopg.Connection,
) -> None:
    from shared.migration_history import _PRE_RESET_SET, _RESET_ANCHOR
    from shared.migrations import (
        RollbackBelowFloor,
        applied_migration_names,
        apply_down,
        check_schema_version,
        required_migration_set,
        rollback_to,
    )
    from tests.ava.migration_support import _schema_sql_stamped_migration_names, _set_table_to

    # Concurrent upstream deltas remain executable and are already represented
    # in this test DB's schema. Preserve their stamps when modeling the reset.
    predecessor = _PRE_RESET_SET | (set(_schema_sql_stamped_migration_names()) - {_RESET_ANCHOR})
    _set_table_to(db_conn, "set", predecessor)
    with psycopg.connect(settings.data_plane.db_url) as conn:
        applied = apply_pending_migrations(conn)
        assert _RESET_ANCHOR in applied
        check_schema_version(conn)
        expected = required_migration_set()
        assert applied_migration_names(conn) == expected
        assert apply_pending_migrations(conn) == []
        with pytest.raises(RollbackBelowFloor):
            rollback_to(conn, predecessor)
        assert applied_migration_names(conn) == expected
        with pytest.raises(RollbackBelowFloor):
            apply_down(conn, _RESET_ANCHOR)
        assert applied_migration_names(conn) == expected


@pytest.mark.parametrize("apply", [False, True])
def test_integer_history_is_refused_without_conversion(
    db_conn: psycopg.Connection, apply: bool
) -> None:
    from shared.migrations import MigrationLayoutError, check_schema_version
    from tests.ava.migration_support import _set_table_to

    _set_table_to(db_conn, "legacy", range(1, 82))
    with psycopg.connect(settings.data_plane.db_url) as conn:
        with pytest.raises(MigrationLayoutError, match="matching release"):
            (apply_pending_migrations if apply else check_schema_version)(conn)
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in range(1, 82)]


def test_reset_anchor_and_history_deletion_roll_back_together(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared import migrations
    from shared.migration_history import _PRE_RESET_SET, _RESET_ANCHOR
    from tests.ava.migration_support import _set_table_to

    (tmp_path / f"{_RESET_ANCHOR}.sql").write_text("SELECT 1;")
    (tmp_path / f"{_RESET_ANCHOR}.down.sql").write_text("SELECT 1;")
    _init_repo(tmp_path)
    monkeypatch.setattr(migrations, "MIGRATIONS_DIR", tmp_path)
    predecessor = {_BASELINE_NAME, *_PRE_RESET_SET}
    _set_table_to(db_conn, "set", predecessor)
    original = migrations._squash_history

    def interrupted(conn: psycopg.Connection, names: set[str]) -> None:
        original(conn, names)
        raise RuntimeError("interrupted after history deletion")

    monkeypatch.setattr(migrations, "_squash_history", interrupted)
    # Autocommit removes any incidental outer transaction: the reset itself
    # must own its atomicity even when no caller transaction can rescue it.
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        with pytest.raises(RuntimeError, match="interrupted"):
            migrations.apply_pending_migrations(conn)
        assert migrations.applied_migration_names(conn) == predecessor
        monkeypatch.setattr(migrations, "_squash_history", original)
        assert migrations.apply_pending_migrations(conn) == [_RESET_ANCHOR]
        migrations.check_schema_version(conn)


def test_failed_reset_anchor_preserves_history_for_retry(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared import migrations
    from shared.migration_history import _PRE_RESET_SET, _RESET_ANCHOR
    from tests.ava.migration_support import _set_table_to

    anchor = tmp_path / f"{_RESET_ANCHOR}.sql"
    anchor.write_text("SELECT 1 / 0;")
    (tmp_path / f"{_RESET_ANCHOR}.down.sql").write_text("SELECT 1;")
    _init_repo(tmp_path)
    monkeypatch.setattr(migrations, "MIGRATIONS_DIR", tmp_path)
    predecessor = {_BASELINE_NAME, *_PRE_RESET_SET}
    _set_table_to(db_conn, "set", predecessor)
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        with pytest.raises(migrations.MigrationFailed, match="division by zero"):
            migrations.apply_pending_migrations(conn)
        assert migrations.applied_migration_names(conn) == predecessor
        anchor.write_text("SELECT 1;")
        assert migrations.apply_pending_migrations(conn) == [_RESET_ANCHOR]
        migrations.check_schema_version(conn)
