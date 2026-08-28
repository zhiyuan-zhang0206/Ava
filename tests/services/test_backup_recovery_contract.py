"""Recovery-source facts shared by backup, checkpoint, and schema layers.

This is intentionally a source contract: these are durable ownership facts,
not a runtime workflow. A future edit that excludes checkpoint tables or calls
the frozen events archive a conversation-recovery source must stop here before
that false claim reaches an operator playbook.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).parents[2]


def _module_docstring(path: Path) -> str:
    return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""


def test_conversation_recovery_sources_are_checkpoint_tables_not_events_archive() -> None:
    """Checkpoint tables are the backed-up write and recovery source; the PG
    events archive was dropped (task #1823) and Loki owns the live stream."""
    backup_path = _ROOT / "services" / "backup.py"
    checkpoint_path = _ROOT / "shared" / "checkpoint.py"
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
    # The PG events archive was dropped with the #1823 cleanup — the baseline
    # documents the drop and the Loki archive stream as the archive's home.
    assert "events archive (dropped" in schema
    assert "loki archive stream" in schema
