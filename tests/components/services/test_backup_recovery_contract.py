from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).parents[3]


def _module_docstring(path: Path) -> str:
    return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""


def test_conversation_recovery_sources_are_checkpoint_tables_not_events_archive() -> None:
    backup_path = _ROOT / "services" / "backup" / "dump.py"
    checkpoint_path = _ROOT / "base" / "agents" / "history" / "checkpoint.py"
    schema_path = _ROOT / "db" / "schema.sql"

    backup_source = backup_path.read_text(encoding="utf-8")
    backup_docstring = _module_docstring(backup_path).lower()
    checkpoint_docstring = _module_docstring(checkpoint_path).lower()
    schema = schema_path.read_text(encoding="utf-8").lower()

    assert "checkpoint_blobs" in backup_docstring
    assert "checkpoints" in backup_docstring
    assert "checkpoint_writes" in backup_docstring
    assert "only copy of conversation history" in backup_docstring
    assert "--exclude-table" not in backup_source
    assert "rebuildable from events" not in backup_source.lower()
    assert "full message history" in checkpoint_docstring
    assert "lives in the stored checkpoint" in checkpoint_docstring
    assert "events archive (dropped" in schema
    assert "loki archive stream" in schema
