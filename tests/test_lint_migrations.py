"""`scripts/lint_migrations.py` — timestamp-id + applied-set layout lint.

Drives the on-disk checks against a tmp migrations/ + a stub db/schema.sql, so
no test touches the real repo tree.
"""

from __future__ import annotations

import importlib

_TS = "20260719T143000"
_TS2 = "20260719T143001"
_BASELINE_INSERT = "INSERT INTO schema_migrations (name) VALUES ('00000000T000000_baseline');"


def _lint(monkeypatch, tmp_path, *, schema_body: str = _BASELINE_INSERT):
    """Point the lint module at a tmp migrations/ + db/schema.sql; return
    (lint_module, migrations_dir)."""
    lint = importlib.import_module("scripts.lint_migrations")
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    monkeypatch.setattr(lint, "MIGRATIONS_DIR", migrations_dir)
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "schema.sql").write_text(schema_body)
    monkeypatch.setattr(lint, "SCHEMA_SQL", tmp_path / "db" / "schema.sql")
    return lint, migrations_dir


def test_empty_migrations_passes(monkeypatch, tmp_path):
    """An empty migrations/ (no delta over the baseline) is valid."""
    lint, _ = _lint(monkeypatch, tmp_path)
    assert lint.main() == 0


def test_valid_pair_passes(monkeypatch, tmp_path):
    lint, d = _lint(monkeypatch, tmp_path)
    (d / f"{_TS}_add-foo.sql").write_text("SELECT 1;")
    (d / f"{_TS}_add-foo.down.sql").write_text("SELECT 1;")
    assert lint.main() == 0


def test_missing_down_fails(monkeypatch, tmp_path):
    lint, d = _lint(monkeypatch, tmp_path)
    (d / f"{_TS}_add-foo.sql").write_text("SELECT 1;")  # no .down.sql
    assert lint.main() == 1


def test_orphan_down_fails(monkeypatch, tmp_path):
    lint, d = _lint(monkeypatch, tmp_path)
    (d / f"{_TS}_add-foo.down.sql").write_text("SELECT 1;")  # no up
    assert lint.main() == 1


def test_legacy_integer_name_fails(monkeypatch, tmp_path):
    """A leftover sequential-integer name is rejected by the format check."""
    lint, d = _lint(monkeypatch, tmp_path)
    (d / "0049_event_log.sql").write_text("SELECT 1;")
    (d / "0049_event_log.down.sql").write_text("SELECT 1;")
    assert lint.main() == 1


def test_invalid_timestamp_fails(monkeypatch, tmp_path):
    """A well-shaped but impossible datetime (month 13) is rejected."""
    lint, d = _lint(monkeypatch, tmp_path)
    (d / "20261301T143000_x.sql").write_text("SELECT 1;")
    (d / "20261301T143000_x.down.sql").write_text("SELECT 1;")
    assert lint.main() == 1


def test_duplicate_timestamp_prefix_fails(monkeypatch, tmp_path):
    """Two migrations sharing one second-precision timestamp prefix are
    ambiguous — rejected (2026-08-07 ruling: names stay second-precision, so
    the lint guards the prefix)."""
    lint, d = _lint(monkeypatch, tmp_path)
    (d / f"{_TS}_add-foo.sql").write_text("SELECT 1;")
    (d / f"{_TS}_add-foo.down.sql").write_text("SELECT 1;")
    (d / f"{_TS}_add-bar.sql").write_text("SELECT 1;")
    (d / f"{_TS}_add-bar.down.sql").write_text("SELECT 1;")
    assert lint.main() == 1


def test_distinct_prefixes_pass(monkeypatch, tmp_path):
    """Sibling migrations in the same second still pass when their prefixes
    differ — the guard is about the prefix, not the second itself."""
    lint, d = _lint(monkeypatch, tmp_path)
    (d / f"{_TS}_add-foo.sql").write_text("SELECT 1;")
    (d / f"{_TS}_add-foo.down.sql").write_text("SELECT 1;")
    (d / f"{_TS2}_add-bar.sql").write_text("SELECT 1;")
    (d / f"{_TS2}_add-bar.down.sql").write_text("SELECT 1;")
    assert lint.main() == 0


def test_readme_is_ignored(monkeypatch, tmp_path):
    lint, d = _lint(monkeypatch, tmp_path)
    (d / "README.md").write_text("docs")
    assert lint.main() == 0


def test_down_bare_drop_fails(monkeypatch, tmp_path):
    """A top-level DROP without IF EXISTS in a down is rejected — a repeated or
    standalone rollback would blow up on a schema that already lacks the
    object (audit P2)."""
    lint, d = _lint(monkeypatch, tmp_path)
    (d / f"{_TS}_add-foo.sql").write_text("SELECT 1;")
    (d / f"{_TS}_add-foo.down.sql").write_text("DROP TABLE foo;")
    assert lint.main() == 1


def test_down_if_exists_passes(monkeypatch, tmp_path):
    lint, d = _lint(monkeypatch, tmp_path)
    (d / f"{_TS}_add-foo.sql").write_text("SELECT 1;")
    (d / f"{_TS}_add-foo.down.sql").write_text("DROP TABLE IF EXISTS foo;")
    assert lint.main() == 0


def test_down_guarded_do_block_drop_passes(monkeypatch, tmp_path):
    """Drops inside a DO block (guarded by the block's own existence checks) are
    not flagged — e.g. the monthly-partitioning down."""
    lint, d = _lint(monkeypatch, tmp_path)
    (d / f"{_TS}_add-foo.sql").write_text("SELECT 1;")
    (d / f"{_TS}_add-foo.down.sql").write_text(
        "DO $$\nBEGIN\n    DROP TABLE agent_events;\nEND $$;\n"
    )
    assert lint.main() == 0


def test_backfill_snapshot_requires_later_drop_plan(monkeypatch, tmp_path):
    """A backfill snapshot stays only until a later migration drops it."""
    lint, d = _lint(monkeypatch, tmp_path)
    snapshot = "agent_state_backfill_snapshot"
    (d / f"{_TS}_record-backfill.sql").write_text(f"CREATE TABLE {snapshot} (id BIGINT);")
    (d / f"{_TS}_record-backfill.down.sql").write_text(f"DROP TABLE IF EXISTS {snapshot};")

    assert lint.main() == 1

    (d / f"{_TS2}_drop-backfill.sql").write_text(f"DROP TABLE IF EXISTS {snapshot};")
    (d / f"{_TS2}_drop-backfill.down.sql").write_text(f"CREATE TABLE {snapshot} (id BIGINT);")
    assert lint.main() == 0


def test_backfill_snapshot_drop_plan_accepts_publicly_qualified_drop(monkeypatch, tmp_path):
    """A retirement migration may pin its drop target to the public schema."""
    lint, d = _lint(monkeypatch, tmp_path)
    snapshot = "agent_state_backfill_snapshot"
    (d / f"{_TS}_record-backfill.sql").write_text(f"CREATE TABLE {snapshot} (id BIGINT);")
    (d / f"{_TS}_record-backfill.down.sql").write_text(f"DROP TABLE IF EXISTS {snapshot};")
    (d / f"{_TS2}_drop-backfill.sql").write_text(f"DROP TABLE IF EXISTS public.{snapshot};")
    (d / f"{_TS2}_drop-backfill.down.sql").write_text(f"CREATE TABLE {snapshot} (id BIGINT);")

    assert lint.main() == 0


def test_backfill_snapshot_drop_plan_ignores_comments_and_literals(monkeypatch, tmp_path):
    """Only executable static SQL can retire a backfill snapshot."""
    lint, d = _lint(monkeypatch, tmp_path)
    snapshot = "agent_state_backfill_snapshot"
    (d / f"{_TS}_record-backfill.sql").write_text(f"CREATE TABLE {snapshot} (id BIGINT);")
    (d / f"{_TS}_record-backfill.down.sql").write_text(f"DROP TABLE IF EXISTS {snapshot};")
    fake_drop = d / f"{_TS2}_drop-backfill.sql"
    fake_drop.write_text(
        f"-- DROP TABLE IF EXISTS {snapshot};\nSELECT 'DROP TABLE IF EXISTS {snapshot}';\n"
    )
    (d / f"{_TS2}_drop-backfill.down.sql").write_text("SELECT 1;")

    assert lint.main() == 1

    fake_drop.write_text(f"DROP TABLE IF EXISTS {snapshot};\n")
    assert lint.main() == 0


def test_backfill_snapshot_check_ignores_comments_and_literals(monkeypatch, tmp_path):
    """Commented or quoted CREATE TABLE text must not require a drop plan."""
    lint, d = _lint(monkeypatch, tmp_path)
    (d / f"{_TS}_commented.sql").write_text(
        "-- CREATE TABLE comment_backfill_snapshot (id BIGINT);\n"
        "SELECT 'CREATE TABLE quoted_backfill_snapshot (id BIGINT)';\n"
    )
    (d / f"{_TS}_commented.down.sql").write_text("SELECT 1;")

    assert lint.main() == 0


def test_backfill_snapshot_check_ignores_non_do_dollar_quoted_literal(monkeypatch, tmp_path):
    """A dollar-quoted value is not a migration DDL statement."""
    lint, d = _lint(monkeypatch, tmp_path)
    (d / f"{_TS}_dollar-quoted.sql").write_text(
        "SELECT $$CREATE TABLE dollar_quoted_backfill_snapshot (id BIGINT)$$;\n"
    )
    (d / f"{_TS}_dollar-quoted.down.sql").write_text("SELECT 1;")

    assert lint.main() == 0


def test_schema_generate_series_seed_fails(monkeypatch, tmp_path):
    """A schema.sql still carrying the pre-cutover generate_series seed is rejected."""
    lint, _ = _lint(
        monkeypatch,
        tmp_path,
        schema_body="INSERT INTO schema_migrations (version) SELECT generate_series(1, 81);",
    )
    assert lint.main() == 1


def test_schema_missing_baseline_seed_fails(monkeypatch, tmp_path):
    """A schema.sql that does not stamp the baseline sentinel is rejected."""
    lint, _ = _lint(monkeypatch, tmp_path, schema_body="-- no baseline seed here")
    assert lint.main() == 1


# ── check 8: folded strict migrations must be stamped in the baseline seed ──


_FOLDED_SCHEMA = (
    """
CREATE TABLE widgets (
    id BIGINT PRIMARY KEY,
    folded_col INT,
    CONSTRAINT widgets_ck CHECK (id > 0)
);
CREATE TABLE fuzz (
    id BIGINT PRIMARY KEY,
    CONSTRAINT fuzz_ck CHECK (id > 0)
);
CREATE TABLE gadgets (id BIGINT PRIMARY KEY);
CREATE INDEX gadgets_idx ON gadgets (id);
CREATE TRIGGER gadgets_trg AFTER UPDATE ON gadgets
    FOR EACH ROW EXECUTE FUNCTION touch_gadget();
"""
    + _BASELINE_INSERT
)


def _write_migration(d, stem: str, body: str) -> None:
    (d / f"{stem}.sql").write_text(body)
    (d / f"{stem}.down.sql").write_text("SELECT 1;")


def test_folded_strict_add_column_without_seed_fails(monkeypatch, tmp_path):
    """A strict ADD COLUMN whose column already sits in db/schema.sql must be
    stamped in the baseline seed — a fresh DB replays the unseeded migration and
    dies on `column ... already exists`."""
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(d, f"{_TS}_add-folded-col", "ALTER TABLE widgets ADD COLUMN folded_col INT;")
    assert lint.main() == 1


def test_folded_strict_add_column_with_seed_passes(monkeypatch, tmp_path):
    stem = f"{_TS}_add-folded-col"
    lint, d = _lint(
        monkeypatch,
        tmp_path,
        schema_body=_FOLDED_SCHEMA + f"\nINSERT INTO schema_migrations (name) VALUES ('{stem}');\n",
    )
    _write_migration(d, stem, "ALTER TABLE widgets ADD COLUMN folded_col INT;")
    assert lint.main() == 0


def test_idempotent_add_column_needs_no_seed(monkeypatch, tmp_path):
    """`ADD COLUMN IF NOT EXISTS` is replay-safe on a fresh DB — no seed needed."""
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(
        d, f"{_TS}_add-folded-col", "ALTER TABLE widgets ADD COLUMN IF NOT EXISTS folded_col INT;"
    )
    assert lint.main() == 0


def test_do_block_guarded_add_column_needs_no_seed(monkeypatch, tmp_path):
    """A statement inside a DO block owns its guard — exempt from the seed rule."""
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(
        d,
        f"{_TS}_guarded-add",
        "DO $$\nBEGIN\n    IF NOT EXISTS (SELECT 1 FROM information_schema.columns) THEN\n"
        "        ALTER TABLE widgets ADD COLUMN folded_col INT;\n    END IF;\nEND $$;",
    )
    assert lint.main() == 0


def test_rebuilt_constraint_needs_no_seed(monkeypatch, tmp_path):
    """DROP + ADD of the same object in one migration is a replay-safe rebuild."""
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(
        d,
        f"{_TS}_rebuild-widgets-ck",
        "ALTER TABLE widgets DROP CONSTRAINT widgets_ck;\n"
        "ALTER TABLE widgets ADD CONSTRAINT widgets_ck CHECK (id > 0);",
    )
    assert lint.main() == 0


def test_unfolded_strict_add_column_needs_no_seed(monkeypatch, tmp_path):
    """A column absent from db/schema.sql cannot hit already-exists on replay;
    whether the baseline is missing the change at all is the smoke convergence
    gate's question, not this check's."""
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(d, f"{_TS}_add-new-col", "ALTER TABLE widgets ADD COLUMN brand_new INT;")
    assert lint.main() == 0


def test_same_named_object_on_another_table_needs_no_seed(monkeypatch, tmp_path):
    """Table-qualified matching: `folded_col` / `widgets_ck` exist on `widgets`,
    not on `fuzz` — adding them to `fuzz` is not a folded delta."""
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(
        d,
        f"{_TS}_fuzz-additions",
        "ALTER TABLE fuzz ADD COLUMN folded_col INT;\n"
        "ALTER TABLE fuzz ADD CONSTRAINT widgets_ck CHECK (id > 0);",
    )
    assert lint.main() == 0


def test_folded_strict_create_table_without_seed_fails(monkeypatch, tmp_path):
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(d, f"{_TS}_create-gadgets", "CREATE TABLE gadgets (id BIGINT PRIMARY KEY);")
    assert lint.main() == 1


def test_folded_strict_create_index_without_seed_fails(monkeypatch, tmp_path):
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(d, f"{_TS}_create-gadgets-idx", "CREATE INDEX gadgets_idx ON gadgets (id);")
    assert lint.main() == 1


def test_rebuilt_index_needs_no_seed(monkeypatch, tmp_path):
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(
        d,
        f"{_TS}_rebuild-gadgets-idx",
        "DROP INDEX IF EXISTS gadgets_idx;\nCREATE INDEX gadgets_idx ON gadgets (id);",
    )
    assert lint.main() == 0


def test_folded_strict_create_trigger_without_seed_fails(monkeypatch, tmp_path):
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(
        d,
        f"{_TS}_create-gadgets-trg",
        "CREATE TRIGGER gadgets_trg AFTER UPDATE ON gadgets\n"
        "    FOR EACH ROW EXECUTE FUNCTION touch_gadget();",
    )
    assert lint.main() == 1


def test_rebuilt_trigger_needs_no_seed(monkeypatch, tmp_path):
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(
        d,
        f"{_TS}_rebuild-gadgets-trg",
        "DROP TRIGGER IF EXISTS gadgets_trg ON gadgets;\n"
        "CREATE TRIGGER gadgets_trg AFTER UPDATE ON gadgets\n"
        "    FOR EACH ROW EXECUTE FUNCTION touch_gadget();",
    )
    assert lint.main() == 0


def test_folded_strict_add_constraint_without_seed_fails(monkeypatch, tmp_path):
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(
        d,
        f"{_TS}_add-widgets-ck",
        "ALTER TABLE widgets ADD CONSTRAINT widgets_ck CHECK (id > 0);",
    )
    assert lint.main() == 1


def test_cross_migration_replay_chain_needs_no_seed(monkeypatch, tmp_path):
    """An earlier unseeded migration dropping the object makes a later add of it
    replay-safe (the replay drops, then re-adds) — no seed required on either."""
    lint, d = _lint(monkeypatch, tmp_path, schema_body=_FOLDED_SCHEMA)
    _write_migration(d, f"{_TS}_drop-folded-col", "ALTER TABLE widgets DROP COLUMN folded_col;")
    _write_migration(
        d, f"{_TS2}_readd-folded-col", "ALTER TABLE widgets ADD COLUMN folded_col INT;"
    )
    assert lint.main() == 0


def test_role_switch_in_migration_fails(monkeypatch, tmp_path, capsys):
    """Schema SQL runs as the admin acting as the owner: no role switching."""
    lint, d = _lint(monkeypatch, tmp_path)
    (d / f"{_TS}_escalate.sql").write_text("SET ROLE NONE;\nCREATE TABLE t (id int);")
    (d / f"{_TS}_escalate.down.sql").write_text("DROP TABLE IF EXISTS t;")
    assert lint.main() == 1
    assert f"{_TS}_escalate.sql:1: SET ROLE" in capsys.readouterr().err


def test_session_authorization_in_baseline_fails(monkeypatch, tmp_path):
    lint, _ = _lint(
        monkeypatch,
        tmp_path,
        schema_body=f"{_BASELINE_INSERT}\nDO $$ BEGIN SET SESSION AUTHORIZATION DEFAULT; END $$;",
    )
    assert lint.main() == 1


def test_role_words_in_comments_and_literals_pass(monkeypatch, tmp_path):
    lint, d = _lint(monkeypatch, tmp_path)
    (d / f"{_TS}_note.sql").write_text(
        "-- never SET ROLE here\nCOMMENT ON TABLE t IS 'RESET ROLE is forbidden';"
    )
    (d / f"{_TS}_note.down.sql").write_text("SELECT 1;")
    assert lint.main() == 0
