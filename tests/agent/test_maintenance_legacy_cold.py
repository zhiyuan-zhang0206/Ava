"""Real persisted ENDs distinguish retired consumers from unfinished work."""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg_pool import AsyncConnectionPool

from agent.hosted_ownership import admit_hosted_runtime, settle_hosted_runtime
from ops.agent_pause import resume_agents
from shared import maintenance_cohort, pause_owner
from shared.db import insert_inbound_message
from shared.machine import machine_name
from tests.agent.test_maintenance import WHEN, _agent
from tests.agent.test_maintenance import isolate as isolate


class _State(TypedDict):
    halted: bool
    exit_requested: bool
    restart_requested: bool
    history: list[str]


def _persist_end(
    conn: psycopg.Connection[Any], agent: int, *, restart: bool, pending: str | None = None
) -> None:
    def claim(_state: _State) -> Command[Any]:
        return Command(
            update={"halted": True, "exit_requested": restart, "restart_requested": False},
            goto=END if pending is None else "work",
        )

    def work(_state: _State) -> Command[Any]:
        interrupt("The action still needs input")
        return Command(goto=END)

    saver = PostgresSaver(conn)
    builder: Any = StateGraph(_State)
    builder.add_node("claim", claim)
    builder.add_node("work", work)
    builder.add_edge(START, "claim")
    graph = builder.compile(
        checkpointer=saver, interrupt_before=["work"] if pending == "branch" else []
    )
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    graph.invoke(
        {
            "halted": False,
            "exit_requested": False,
            "restart_requested": False,
            "history": ["Keep the original context"],
        },
        config,
    )
    snapshot = graph.get_state(config)
    if pending is None:
        assert snapshot.next == () and snapshot.tasks == ()
    else:
        assert snapshot.next == ("work",) and snapshot.tasks
        if pending == "interrupt":
            assert snapshot.tasks[0].interrupts
    assert snapshot.values["exit_requested"] is restart
    conn.commit()


def _retired(conn: psycopg.Connection[Any], *, restart: bool) -> int:
    agent = _agent(conn)
    conn.execute(
        "UPDATE agents_meta SET status=%s,runtime_kind='hosted',runtime_owner=%s,"
        "runtime_generation=%s,lease_expires_at=%s WHERE id=%s",
        (
            "restarting" if restart else "idling",
            uuid4(),
            uuid4(),
            datetime.now(UTC) - timedelta(minutes=5),
            agent,
        ),
    )
    if restart:
        command = insert_inbound_message(conn, agent, "", "system:update", kind="restart")
        conn.execute(
            "UPDATE inbound_messages SET status='done',claimed_at=clock_timestamp() WHERE id=%s",
            (command,),
        )
    conn.commit()
    _persist_end(conn, agent, restart=restart)
    return agent


@pytest.mark.parametrize("restart", [False, True])
def test_completed_retired_consumer_parks_without_inventing_receipts(
    db_conn: psycopg.Connection[Any], restart: bool
) -> None:
    agent = _retired(db_conn, restart=restart)
    before = db_conn.execute(
        "SELECT runtime_kind,runtime_owner,runtime_generation,lease_expires_at,"
        "lifecycle_command_id,incarnation_resources FROM agents_meta WHERE id=%s",
        (agent,),
    ).fetchone()
    messages = db_conn.execute(
        "SELECT * FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent,)
    ).fetchall()
    checkpoints = db_conn.execute(
        "SELECT * FROM checkpoints WHERE thread_id=%s ORDER BY checkpoint_id", (str(agent),)
    ).fetchall()
    db_conn.commit()
    pause_owner.begin_maintenance("cold", WHEN)
    hold = maintenance_cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=None,
        holder="cold",
        acquired_at=WHEN,
        host_absent=True,
    )
    assert hold.parked == (agent,)  # time-bomb-ok: WHEN is hold identity; this compares agent IDs.
    assert not hold.commands and not hold.drained
    maintenance_cohort.verify_drained(db_conn, hold)
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "idling",
    )
    assert (
        db_conn.execute(
            "SELECT runtime_kind,runtime_owner,runtime_generation,lease_expires_at,"
            "lifecycle_command_id,incarnation_resources FROM agents_meta WHERE id=%s",
            (agent,),
        ).fetchone()
        == before
    )
    assert (
        db_conn.execute(
            "SELECT * FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent,)
        ).fetchall()
        == messages
    )
    assert (
        db_conn.execute(
            "SELECT * FROM checkpoints WHERE thread_id=%s ORDER BY checkpoint_id", (str(agent),)
        ).fetchall()
        == checkpoints
    )


def _prepare(conn: psycopg.Connection[Any]) -> None:
    pause_owner.begin_maintenance("cold", WHEN)
    maintenance_cohort.prepare(
        conn,
        machine=machine_name(),
        host_owner=None,
        holder="cold",
        acquired_at=WHEN,
        host_absent=True,
    )


@pytest.mark.parametrize(
    "defect", ["fresh_lease", "old_end", "unknown_version", "branch", "interrupt", "no_end"]
)
def test_incomplete_or_live_restart_is_not_normalized(
    db_conn: psycopg.Connection[Any], defect: str
) -> None:
    agent = _retired(db_conn, restart=True)
    if defect == "fresh_lease":
        db_conn.execute(
            "UPDATE agents_meta SET lease_expires_at=clock_timestamp()+interval '1 hour' "
            "WHERE id=%s",
            (agent,),
        )
    elif defect == "old_end":
        db_conn.execute(
            "UPDATE inbound_messages SET claimed_at=clock_timestamp() WHERE agent_id=%s",
            (agent,),
        )
    elif defect == "unknown_version":
        db_conn.execute(
            "UPDATE checkpoints SET checkpoint=jsonb_set(checkpoint,'{v}','99') WHERE thread_id=%s",
            (str(agent),),
        )
    elif defect == "no_end":
        _persist_end(db_conn, agent, restart=False)
    else:
        _persist_end(db_conn, agent, restart=True, pending=defect)
    db_conn.commit()
    before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
    db_conn.commit()
    with pytest.raises(RuntimeError):
        _prepare(db_conn)
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("kind", ["restart", "terminate"])
def test_pending_lifecycle_keeps_the_original_cold_intent(
    db_conn: psycopg.Connection[Any], restart: bool, kind: str
) -> None:
    agent = _retired(db_conn, restart=restart)
    insert_inbound_message(db_conn, agent, "", "user", kind=kind)
    before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
    db_conn.commit()
    with pytest.raises(RuntimeError):
        _prepare(db_conn)
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before


def test_real_unrecorded_legacy_consumer_blocks_cold_prepare(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    agent = _retired(db_conn, restart=True)
    package = tmp_path / "agent"
    package.mkdir()
    (package / "__init__.py").write_text("")
    ready = tmp_path / "ready"
    (package / "__main__.py").write_text(
        "from pathlib import Path\nimport time\nPath('ready').touch()\ntime.sleep(30)\n"
    )
    # This private package exercises the real -m agent launch shape without
    # importing the framework or contacting any production cluster.
    with subprocess.Popen(  # noqa: S603 — fixed Python fixture module under tmp_path
        [sys.executable, "-m", "agent", "--agent-id", str(agent)],
        cwd=tmp_path,
        env=dict(os.environ),
    ) as process:
        try:
            deadline = time.monotonic() + 5
            while not ready.exists():
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.02)
            with pytest.raises(RuntimeError, match="native consumer"):
                _prepare(db_conn)
        finally:
            process.terminate()
            process.wait(timeout=5)
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "restarting",
    )


def _aged_request(
    exec_dir: Path, agent_id: int, *, owner: object | None, age_s: float = 3600.0
) -> Path:
    """One request envelope whose mtime predates any live birth window."""
    agent_dir = exec_dir / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    request = agent_dir / f"req-{uuid4().hex}.json"
    envelope: dict[str, object] = {
        "v": 1,
        "code": "print('x')",
        "agent_id": agent_id,
        "timeout_s": 30.0,
    }
    if owner is not None:
        envelope["incarnation"] = {"generation": str(uuid4()), "owner": str(owner)}
    request.write_text(json.dumps(envelope))
    stamp = request.stat().st_mtime - age_s
    os.utime(request, (stamp, stamp))
    return request


def _exec_request_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    exec_dir = tmp_path / "exec"
    quarantine = tmp_path / "quarantined-exec-requests"
    monkeypatch.setattr("shared.exec_request_evidence.exec_run_dir", lambda: exec_dir)
    monkeypatch.setattr(
        "shared.exec_request_evidence.quarantined_exec_requests_dir", lambda: quarantine
    )
    return exec_dir, quarantine


def test_superseded_exec_envelope_is_quarantined_and_cold_prepare_parks(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A superseded incarnation's envelope no longer fences cold preparation."""
    agent = _retired(db_conn, restart=True)
    exec_dir, quarantine = _exec_request_dirs(tmp_path, monkeypatch)
    request = _aged_request(exec_dir, agent, owner=uuid4())
    before = request.read_text()

    _prepare(db_conn)

    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "idling",
    )
    assert not request.exists()
    (moved,) = quarantine.glob(f"*/{agent}/{request.name}")
    assert moved.read_text() == before
    receipt = json.loads((moved.parent / "receipt.json").read_text())
    assert receipt["reason"] == "maintenance cold prepare"
    assert receipt["entries"][0]["source"] == str(request)


def test_unattributable_exec_envelope_refuses_cold_prepare_with_disposition(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No incarnation attribution: refuse, name the file, keep the evidence."""
    agent = _retired(db_conn, restart=True)
    exec_dir, quarantine = _exec_request_dirs(tmp_path, monkeypatch)
    request = _aged_request(exec_dir, agent, owner=None)

    with pytest.raises(RuntimeError, match="unsettled exec request") as excinfo:
        _prepare(db_conn)

    message = str(excinfo.value)
    assert request.name in message
    assert f"--agent {agent}" in message and "shared.exec_request_evidence" in message
    assert request.exists() and not quarantine.exists()
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "restarting",
    )


def test_failed_current_lifecycle_cannot_be_parked(db_conn: psycopg.Connection[Any]) -> None:
    agent = _retired(db_conn, restart=False)
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,status,source,content,target_generation,"
        "target_owner,claimed_at,payload) SELECT id,'restart','done','system:update','',"
        'runtime_generation,runtime_owner,clock_timestamp(),\'{"lifecycle_result":{"outcome":"failed"}}\' '
        "FROM agents_meta WHERE id=%s",
        (agent,),
    )
    db_conn.commit()
    with pytest.raises(RuntimeError, match="failed lifecycle"):
        _prepare(db_conn)


@pytest.mark.parametrize("restart", [False, True])
def test_checkpoint_replaced_by_real_second_connection_refuses_normalization(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, restart: bool
) -> None:
    from shared import maintenance_cold

    agent = _retired(db_conn, restart=restart)
    original = maintenance_cold.require_persisted_end

    def replace(conn: psycopg.Connection[Any], agent_id: int, *, restarting: bool) -> str:
        checkpoint = original(conn, agent_id, restarting=restarting)
        with psycopg.connect(db_conn.info.dsn, autocommit=True) as writer:
            _persist_end(writer, agent_id, restart=restart)
        return checkpoint

    monkeypatch.setattr(maintenance_cold, "require_persisted_end", replace)
    with pytest.raises(RuntimeError, match="checkpoint changed"):
        _prepare(db_conn)
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "restarting" if restart else "idling",
    )


@pytest.mark.parametrize("restart", [False, True])
async def test_resume_admits_a_successor_without_rewriting_legacy_history(
    db_conn: psycopg.Connection[Any], aops_pool: AsyncConnectionPool[Any], restart: bool
) -> None:
    agent = _retired(db_conn, restart=restart)
    history = db_conn.execute(
        "SELECT * FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent,)
    ).fetchall()
    checkpoint = PostgresSaver(db_conn).get_tuple({"configurable": {"thread_id": str(agent)}})
    db_conn.commit()
    _prepare(db_conn)
    current = pause_owner.read()
    assert current.maintenance is not None
    maintenance_cohort.verify_drained(db_conn, current.maintenance)
    db_conn.commit()
    owner = uuid4()
    assert (
        await admit_hosted_runtime(aops_pool, agent, machine_name(), owner, expected_from="idling")
        is None
    )
    resume_agents()
    admitted = await admit_hosted_runtime(
        aops_pool, agent, machine_name(), owner, expected_from="idling"
    )
    assert admitted is not None and admitted.owner == owner
    assert await settle_hosted_runtime(aops_pool, admitted)
    assert db_conn.execute(
        "SELECT status,runtime_owner FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone() == ("idling", owner)
    assert (
        db_conn.execute(
            "SELECT * FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent,)
        ).fetchall()
        == history
    )
    assert (
        PostgresSaver(db_conn).get_tuple({"configurable": {"thread_id": str(agent)}}) == checkpoint
    )
