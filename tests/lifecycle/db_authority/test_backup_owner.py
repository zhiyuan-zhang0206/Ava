"""Logical backup maintenance on an always-authenticated born home.

A home born by the real start steps (`test_single_box.born`): NOLOGIN schema
owner, write generation 0, a credential-free `AVA_DB_URL`. The maintenance
process here holds NO write-generation login: its `AVA_DB_URL` is the
credential-free endpoint and no `AVA_DB_ADMIN_PASSWORD` exists anywhere. The
scheduled dump must still work, because it dials the home's own authority
(`base.db.pg_admin`): the OS-user administrator over the owner-only socket,
acting as the schema owner.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.conninfo import conninfo_to_dict

from base.cluster import authority
from base.config import settings
from base.host.net.url_secret import url_with_port
from cli.commands.data_plane import cluster_instance as ci
from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.tests.test_single_box import Born
from cli.commands.tests.test_single_box import born as born
from cli.commands.tests.test_single_box import configured as configured
from services.gateway_side.backup import offsite

pytestmark = pytest.mark.skipif(
    not (Path(pooler.pgbouncer_bin()).exists() or shutil.which(pooler.pgbouncer_bin())),
    reason="pgbouncer not installed (brew/apt)",
)


def _no_publish(*_args: object, **_kwargs: object) -> None:
    """The off-site leg is not under test here."""


@pytest.fixture
def maintenance(born: Born, monkeypatch: pytest.MonkeyPatch) -> Born:
    """The born home, seen by a process that holds no write-generation login."""
    endpoint = born.endpoint()
    assert conninfo_to_dict(endpoint).get("password") is None
    monkeypatch.setattr(settings.data_plane, "db_url", endpoint)
    monkeypatch.setitem(os.environ, "AVA_DB_URL", endpoint)
    monkeypatch.delitem(os.environ, authority.GENERATION_ENV, raising=False)
    monkeypatch.delenv("AVA_DB_ADMIN_PASSWORD", raising=False)
    assert "AVA_DB_ADMIN_PASSWORD" not in (born.home / ".env").read_text()
    monkeypatch.setattr(offsite, "publish", _no_publish)
    return born


def _seed_conversation(born: Born) -> int:
    """One agent with a readable checkpoint conversation, written as the gateway."""
    gateway = url_with_port(born.dsn("gateway"), born.pg_port)
    with psycopg.connect(gateway, autocommit=True) as conn:
        row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = datetime.now(UTC).isoformat()
    checkpoint["channel_values"] = {"messages": [HumanMessage(content="backed up")]}
    checkpoint["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(gateway) as saver:
        saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata={"source": "input", "step": 1, "parents": {}},
            new_versions={"messages": "1"},
        )
    return agent_id


def _assert_owner_dial(conninfo: str) -> None:
    """The home's administrator over its owner-only socket, acting as the owner."""
    target = conninfo_to_dict(conninfo)
    assert target["host"] == str(ci._pg_socket_dir())
    assert target["user"] == getpass.getuser()
    assert target["dbname"] == "ava"
    assert target["options"] == "-c role=ava"
    assert "password" not in target


def test_scheduled_backup_dumps_as_the_owner_and_restores(
    maintenance: Born, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.data_plane_ops import restore_drill
    from services.backup_scheduler import worker

    agent_id = _seed_conversation(maintenance)
    dumps: list[tuple[list[str], dict[str, str] | None]] = []
    real = cast("Callable[..., subprocess.CompletedProcess[bytes]]", subprocess.run)

    # Without a progress sink the backup runs each stage as exactly `subprocess.run`, so this
    # is what the dump child is spawned with.
    def spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if str(argv[0]).endswith("pg_dump"):
            dumps.append((argv, kwargs.get("env")))
        return real(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    # The scheduled worker's own operation, then the controller's publication.
    result = worker._execute({"kind": "dump", "now": datetime.now(UTC).isoformat()}, work)
    artifact = worker.commit_scheduled_backup(
        work / "artifact" / str(result["artifact"]), str(result["sha256"])
    )

    assert artifact.parent == maintenance.home / "backups" / "db"
    ((argv, env),) = dumps
    _assert_owner_dial(argv[argv.index("--dbname") + 1])
    assert env == {}  # no PGPASSWORD: the dial carries no credential at all

    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    report, _elapsed = restore_drill.run_drill(artifact, foreground=True, scratch_root=scratch)
    assert report.agents == 1
    assert report.sample_agent_id == agent_id
    assert report.sample_message_count == 1
    assert report.agents_owner == "ava"
