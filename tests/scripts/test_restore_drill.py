"""Contract tests for the standalone encrypted-backup restore drill."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

from shared.config import settings

_SCRIPT = Path(__file__).parents[2] / "scripts" / "restore_drill.py"
_SPEC = importlib.util.spec_from_file_location("restore_drill", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
restore_drill = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = restore_drill
_SPEC.loader.exec_module(restore_drill)


def _write_checkpoint(agent_id: int) -> None:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": [HumanMessage(content="restored conversation")]}
    checkpoint["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata={"source": "input", "step": 1, "parents": {}},
            new_versions={"messages": "1"},
        )


def test_verification_reports_schema_counts_and_readable_conversation(
    db_conn: psycopg.Connection,
) -> None:
    """The drill's verification is stronger than a successful pg_restore: it
    reads every checkpoint table and loads one restored conversation through
    the production checkpoint reader."""
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
    assert row is not None
    agent_id = row[0]
    db_conn.commit()
    _write_checkpoint(agent_id)

    report = restore_drill.verify_restored_database(settings.data_plane.db_url)

    assert report.agents == 1
    assert report.checkpoints >= 1
    assert report.checkpoint_blobs >= 0
    assert report.checkpoint_writes >= 0
    assert report.sample_agent_id == agent_id
    assert report.sample_message_count == 1


@pytest.mark.skipif(not restore_drill.pg_tool("pg_dump").exists(), reason="needs native pg_dump")
def test_run_drill_restores_an_encrypted_artifact_into_throwaway_postgres(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command path decrypts, unzips, restores, and proves a checkpoint
    reader can consume the restored conversation without touching the source DB."""
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
    assert row is not None
    agent_id = row[0]
    db_conn.commit()
    _write_checkpoint(agent_id)

    monkeypatch.setattr(restore_drill.backup, "backup_dir", lambda: tmp_path)
    monkeypatch.setattr(restore_drill.backup, "find_writable_google_drive", lambda: None)
    artifact = restore_drill.backup.run_backup()
    report, elapsed = restore_drill.run_drill(artifact)

    assert report.agents == 1
    assert report.sample_agent_id == agent_id
    assert report.sample_message_count == 1
    assert elapsed > 0
