"""Actual resource admission and exact completion preserve predecessor facts."""

import asyncio
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from shared.incarnation_resources import (
    ResourceEvidenceError,
    ResourceProcess,
    attach_exec,
    complete_exec,
    register_exec,
)
from shared.resource_admission import admit_resources
from shared.runtime_incarnation import RuntimeIncarnation
from tests.agent.test_incarnation_resources import _admitted, _entry, _force


async def test_real_exec_dispatch_uses_owner_and_discharges_exact_map(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.graph import _exec_owned_run
    from agent.graph._exec_result import _ExecDone
    from agent.graph._exec_subprocess import _run_in_subprocess

    target = _admitted(db_conn)

    def admitted(_agent_id: int) -> RuntimeIncarnation:
        return target

    monkeypatch.setattr(_exec_owned_run, "current_incarnation", admitted)
    result, payload = await _run_in_subprocess(
        "print('owned-runtime-proof')", target.agent_id, asyncio.Event(), 30, exec_dir=tmp_path
    )
    assert isinstance(result, _ExecDone), result
    assert payload is not None and payload.kind == "done"
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone()
    assert row is not None and row[0]["requests"] == {}
    receipts = list((tmp_path / str(target.agent_id) / "domains").glob("*/owner.closed"))
    assert len(receipts) == 1


def test_successor_cannot_reset_unknown_or_unresolved_set(db_conn: psycopg.Connection) -> None:
    target = _admitted(db_conn)
    entry = _entry()
    with db_conn.transaction():
        register_exec(db_conn, target, entry)
    successor = RuntimeIncarnation(target.agent_id, uuid4(), uuid4())
    with pytest.raises(ResourceEvidenceError), db_conn.transaction():
        admit_resources(db_conn, successor, ResourceProcess(pid=999, birth=1.0))
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone()
    assert row is not None and str(entry.request) in row[0]["requests"]


def test_exact_terminal_consumption_survives_force_but_replay_refuses(
    db_conn: psycopg.Connection,
) -> None:
    target = _admitted(db_conn)
    entry = _entry()
    attached = entry.model_copy(
        update={
            "owner_process": ResourceProcess(pid=10, birth=1.0),
            "root_process": ResourceProcess(pid=11, birth=2.0),
        }
    )
    with db_conn.transaction():
        register_exec(db_conn, target, entry)
        attach_exec(db_conn, target, entry, attached)
        command = _force(db_conn, target)
    with pytest.raises(ResourceEvidenceError), db_conn.transaction():
        complete_exec(db_conn, target, attached.model_copy(update={"domain": uuid4()}))
    with db_conn.transaction():
        complete_exec(db_conn, target, attached)
    with pytest.raises(ResourceEvidenceError), db_conn.transaction():
        complete_exec(db_conn, target, attached)
    row = db_conn.execute(
        "SELECT incarnation_resources,lifecycle_command_id FROM agents_meta WHERE id=%s",
        (target.agent_id,),
    ).fetchone()
    assert row is not None and row[0]["requests"] == {} and row[1] == command


def test_malformed_never_downgrades_to_legacy(db_conn: psycopg.Connection) -> None:
    target = _admitted(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb({"version": 9}), target.agent_id),
    )
    db_conn.commit()
    with pytest.raises(ValueError), db_conn.transaction():
        admit_resources(db_conn, target, ResourceProcess(pid=10, birth=1.0))
