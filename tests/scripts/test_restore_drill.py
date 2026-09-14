"""Contract tests for the standalone encrypted-backup restore drill."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from datetime import UTC, datetime
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
    checkpoint["ts"] = datetime.now(UTC).isoformat()  # real conversations carry a ts
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


def _grant_prod_roles(db_conn: psycopg.Connection) -> None:
    """Reproduce the prod role set in the test DB so the dump carries the
    GRANT/OWNER statements that broke the 2026-08-27 prod drill (the throwaway
    cluster only has `ava`, so pg_restore hit `role does not exist`)."""
    from psycopg import sql as pgsql

    with db_conn.cursor() as cur:
        for role in restore_drill._RESTORE_ROLES:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone() is None:
                cur.execute(pgsql.SQL("CREATE ROLE {} LOGIN").format(pgsql.Identifier(role)))
        cur.execute("GRANT SELECT ON agents TO ava_runner")
        cur.execute("GRANT SELECT ON agents TO grafana_ro")
        cur.execute(
            pgsql.SQL("ALTER TABLE agents OWNER TO {}").format(pgsql.Identifier("ava_main"))
        )
    db_conn.commit()


@pytest.mark.skipif(not restore_drill.pg_tool("pg_dump").exists(), reason="needs native pg_dump")
def test_run_drill_restores_an_encrypted_artifact_into_throwaway_postgres(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command path decrypts, restores, and proves a checkpoint
    reader can consume the restored conversation without touching the source DB.

    The test DB carries the prod role set (ava_main / ava_runner / grafana_ro
    with grants) so the dump exercises the same restore path that failed in
    production on 2026-08-27."""
    _grant_prod_roles(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
    assert row is not None
    agent_id = row[0]
    db_conn.commit()
    _write_checkpoint(agent_id)

    monkeypatch.setattr(restore_drill.backup, "backup_dir", lambda: tmp_path)

    def _no_publish(_artifact: object) -> None:
        return None

    monkeypatch.setattr(restore_drill.backup, "_publish_offsite", _no_publish)
    artifact = restore_drill.backup.run_backup()
    report, elapsed = restore_drill.run_drill(artifact)

    assert report.agents == 1
    assert report.sample_agent_id == agent_id
    assert report.sample_message_count == 1
    assert report.agents_owner == "ava_main"
    assert elapsed > 0


def test_scratch_space_requirement_scales_with_the_dump_size(tmp_path: Path) -> None:
    """The base is picked against a multiple of the decrypted dump's size: the
    2026-09-14 restore needed >=17 GiB from a 10.16 GiB artifact (~1.7x), and the
    drill reserves 2x as the floor for choosing a base."""
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"x" * 1000)
    assert restore_drill._scratch_space_requirement(dump) == 2000


def test_restore_failure_message_names_the_base_and_the_capacity_knob(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A server killed mid-COPY must not surface as a bare exit code (the
    2026-09-14 WSL failure: `pg_restore exited 1` plus a host-side dmesg signal).
    The message carries the base, its free space, the stderr tail, and the
    override knob that moves the base."""
    base = tmp_path / "fallback"
    base.mkdir()
    monkeypatch.setattr(
        restore_drill.shutil,
        "disk_usage",
        lambda _path: types.SimpleNamespace(free=512 * 2**20),  # pyright: ignore[reportUnknownArgumentType]
    )
    proc = subprocess.CompletedProcess(
        args=["pg_restore"],
        returncode=1,
        stderr=b"pg_restore: error: PQputCopyData: server closed the connection unexpectedly\n",
    )
    message = restore_drill._restore_failure_message(proc, base)
    assert "pg_restore exited 1" in message
    assert str(base) in message
    assert "512 MiB free" in message
    assert "AVA_PG_THROWAWAY_BASE" in message
    assert "PQputCopyData: server closed the connection" in message
