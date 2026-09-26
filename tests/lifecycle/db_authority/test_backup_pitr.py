"""Logical backup and PITR maintenance on an always-authenticated born home.

A home born by the real start steps (`test_single_box.born`): NOLOGIN schema
owner, write generation 0, a credential-free `AVA_DB_URL`. The maintenance
process here holds NO write-generation login: its `AVA_DB_URL` is the
credential-free endpoint and no `AVA_DB_ADMIN_PASSWORD` exists anywhere. Dumps,
DDL and PITR probes must still work, because they dial the home's own authority
(`shared.pg_admin`): the OS-user administrator over the owner-only socket,
acting as the schema owner for dumps/DDL/reads and as itself for server admin,
custody-checked against the home's postmaster.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.conninfo import conninfo_to_dict

from cli.commands import _cluster_instance as ci
from cli.commands import _pgbouncer as pooler
from services import backup
from services.pitr import store_factory
from shared import cluster
from shared.cluster import authority
from shared.config import settings
from shared.pg_admin import local_owner_authority
from shared.pg_tools import pg_tool
from shared.process_env import restricted_process_env
from shared.url_secret import url_with_port
from tests.lifecycle.db_authority.test_single_box import Born
from tests.lifecycle.db_authority.test_single_box import born as born
from tests.lifecycle.db_authority.test_single_box import configured as configured

pytestmark = pytest.mark.skipif(
    not (Path(pooler.pgbouncer_bin()).exists() or shutil.which(pooler.pgbouncer_bin())),
    reason="pgbouncer not installed (brew/apt)",
)

_REPO = Path(__file__).resolve().parents[3]


def _no_store() -> Any:
    raise RuntimeError("no backup store configured")


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

    def _registry() -> dict[str, cluster.ClusterRecord]:
        return {str(born.home): born.record}

    monkeypatch.setattr(cluster, "load_registry", _registry)
    monkeypatch.setattr(store_factory, "get_store_group", _no_store)
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
    from scripts import restore_drill
    from services.backup_scheduler import worker

    agent_id = _seed_conversation(maintenance)
    dumps: list[tuple[list[str], dict[str, str] | None]] = []
    real = backup._run_with_progress

    def spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if argv[0].endswith("pg_dump"):
            dumps.append((argv, kwargs.get("env")))
        return real(argv, **kwargs)

    monkeypatch.setattr(backup, "_run_with_progress", spy)
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


def test_pre_activation_snapshot_dumps_the_frozen_owner_target(maintenance: Born) -> None:
    from cli.commands import _pitr_activation as activation
    from services.gateway_side.backup import snapshot

    state = activation._read_pg_state()
    _assert_owner_dial(state["dump_conninfo"])
    lines: list[str] = []
    artifact = snapshot.create_pre_activation_snapshot(
        operation_id=str(uuid.uuid4()), db_url=state["dump_conninfo"], progress=lines.append
    )
    assert artifact.name.endswith(".dump.enc") and backup._is_activation(artifact)
    assert lines[-1] == f"{artifact} (verified)"
    # The frozen face is stable, so the snapshot's before/during checks hold.
    activation._require_same_pg_state(state, "after snapshot")


def test_rollback_snapshot_exports_and_retires_as_the_owner(
    maintenance: Born, tmp_path: Path
) -> None:
    from services.pitr.rollback_snapshot_archive import (
        drop_rollback_snapshot_table,
        export_rollback_snapshot_table,
    )

    table = "agents_backfill_rollback_probe"
    with local_owner_authority().session(autocommit=True) as conn:
        conn.execute(f"CREATE TABLE {table} AS SELECT 1 AS id")
    # A write generation can read the snapshot but never drop the owner's table.
    gateway = url_with_port(maintenance.dsn("gateway"), maintenance.pg_port)
    with (
        psycopg.connect(gateway, autocommit=True) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(f"DROP TABLE {table}")

    dump = tmp_path / f"{table}.dump"
    export_rollback_snapshot_table(table, dump)
    listing = subprocess.run(  # noqa: S603 — resolved pg_restore + test-owned dump
        [str(pg_tool("pg_restore")), "--list", str(dump)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"TABLE DATA public {table}" in listing.stdout

    drop_rollback_snapshot_table(table)
    drop_rollback_snapshot_table(table)  # idempotent
    with maintenance.admin() as conn:
        assert conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone() == (None,)


# The restricted restore worker's live probes: identity and live counts, with
# no inherited environment, on exactly the dial the controller hands it.
_WORKER_PROBE = """
import json, sys
import psycopg
request = json.loads(sys.stdin.read())
with psycopg.connect(request["live_db_url"], connect_timeout=5) as conn:
    row = conn.execute(
        "SELECT current_user, system_identifier::text, pg_postmaster_start_time()::text"
        " FROM pg_control_system()"
    ).fetchone()
    agents = conn.execute("SELECT count(*) FROM agents").fetchone()
print(json.dumps({"user": row[0], "system": row[1], "agents": agents[0]}))
"""


def test_pitr_probes_dial_the_home_authority(
    maintenance: Born, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.commands import _pitr_activation_config as activation_config
    from services.pitr import activation_runtime, base_candidate, base_operation_runtime
    from services.pitr.base_candidate import BaseCandidateError
    from services.pitr.restore_postgres import _live_identity

    # The restore/drill controller: the worker's dial and the live PGDATA.
    conninfo = base_operation_runtime.live_probe_conninfo()
    _assert_owner_dial(conninfo)
    data_directory = base_operation_runtime.live_data_directory()
    assert Path(data_directory).resolve() == (maintenance.home / "pg").resolve()
    probe = subprocess.run(  # noqa: S603 — this interpreter + a fixed probe script
        [sys.executable, "-c", _WORKER_PROBE],
        input=json.dumps({"live_db_url": conninfo}),
        env=restricted_process_env(),
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    observed = json.loads(probe.stdout)
    assert observed["user"] == "ava" and observed["agents"] == 0
    identity = _live_identity(conninfo, data_directory)
    assert identity.system_identifier == observed["system"]

    # The base candidate's capture-time facts, read as the owner.
    with local_owner_authority().session() as conn:
        major, system_id, _segment, timeline, database = base_candidate._server_facts(conn)
        migration_set = base_candidate._migration_set_sha256(conn)
    assert (major, system_id, timeline, database) == (17, observed["system"], 1, "ava")
    assert len(migration_set) == 64
    # The replication preflight reaches pg_hba on the admin session: no PITR
    # replication row exists on this home, so it refuses on the rule itself.
    with pytest.raises(BaseCandidateError, match="physical-replication rule"):
        base_candidate._validate_replication_hba({"user": "ava_pitr_repl", "host": "127.0.0.1"})

    # Activation: the privilege probe, the WAL-switch capture and the config reader.
    activation_runtime.probe_switch_privilege()
    evidence = activation_runtime.prepare_wal_switch()
    assert evidence["timeline"] == "1" and len(evidence["segment"]) == 24

    def _record(_home: Path) -> cluster.ClusterRecord:
        return maintenance.record

    monkeypatch.setattr(activation_config, "get_record", _record)  # bound at its import
    settings_now = activation_config._persistent_archive_settings(maintenance.home)
    assert settings_now["archive_mode"] == "__ABSENT__"

    # Custody binds each admin session to this home: the same socket read as
    # another home's server refuses before any statement.
    foreign = tmp_path / "foreign-home"
    (foreign / "pg").mkdir(parents=True)
    monkeypatch.setattr(activation_runtime, "ava_home", lambda: foreign)
    with pytest.raises(RuntimeError, match="postmaster is absent"):
        activation_runtime.probe_switch_privilege()
