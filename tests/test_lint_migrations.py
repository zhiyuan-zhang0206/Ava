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
