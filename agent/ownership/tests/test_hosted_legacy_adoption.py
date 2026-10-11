"""Legacy NULL-resource rows admit early only on machine-local death evidence.

Reproduces issue #2156: the 2026-09-10 force restart on macmini left
predecessor-owned rows with ``incarnation_resources = NULL`` and unexpired
leases; every wake refused (``row left idling (concurrent lifecycle op)``)
until the lease expired naturally. Admission now takes the evidence-gated
legacy path — renewal silence at or beyond ``LEGACY_HOST_ADOPTION_SILENCE_S``
plus no live same-home host daemon and no live exec child of this agent —
re-pins the proposal to the exact locked row, and records a
``hosted_legacy_adoption`` audit event.

NULL evidence alone is never enough: a fresh lease, a live local host, a live
exec child, an unattributable probe, a crash-marked row or a remote machine
all keep the original fence. The spawned look-alikes below carry this pytest
worker's own AVA_HOME (conftest gives each process its own home), so they can
never leak into another xdist worker's evidence scan.
"""

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from pathlib import Path
from uuid import UUID, uuid4

import psutil
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import admit_hosted_runtime
from base.agents.incarnation.host_process_evidence import LocalHostEvidence, local_host_evidence
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.paths import ava_home, exec_run_dir


def _seed(
    db_conn: psycopg.Connection,
    *,
    owner: UUID | None = None,
    lease_s: float = 300.0,
    status: str = "idling",
    machine: str = "host-test",
    marked: bool = False,
) -> tuple[int, UUID]:
    """Seed the force-restart shape: predecessor owner + NULL evidence + lease."""
    agent_id = create_agent(db_conn)
    prior_owner = owner if owner is not None else uuid4()
    db_conn.execute(
        "INSERT INTO agents_meta (id, status, machine, runtime_kind, runtime_generation, "
        "runtime_owner, incarnation_resources, lease_expires_at, last_turn_fatal_at) "
        "VALUES (%s, %s, %s, 'hosted', %s, %s, NULL, "
        "clock_timestamp()+make_interval(secs => %s), "
        "CASE WHEN %s THEN clock_timestamp() END) "
        "ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status, machine=EXCLUDED.machine, "
        "runtime_kind=EXCLUDED.runtime_kind, runtime_generation=EXCLUDED.runtime_generation, "
        "runtime_owner=EXCLUDED.runtime_owner, "
        "incarnation_resources=EXCLUDED.incarnation_resources, "
        "lease_expires_at=EXCLUDED.lease_expires_at, "
        "last_turn_fatal_at=EXCLUDED.last_turn_fatal_at",
        (agent_id, status, machine, uuid4(), prior_owner, lease_s, marked),
    )
    db_conn.commit()
    return agent_id, prior_owner


async def _admit(
    pool: AsyncConnectionPool,
    agent_id: int,
    owner: UUID,
    *,
    expected_from: str = "idling",
    database_gate: ProcessDbGate,
) -> RuntimeIncarnation | None:
    return await admit_hosted_runtime(
        pool,
        agent_id,
        "host-test",
        owner,
        expected_from=expected_from,
        db=Database.from_settings(gate=database_gate),
    )


def _evidence(agent_id: int) -> LocalHostEvidence:
    return local_host_evidence(agent_id, ava_home(), exclude_pid=os.getpid())


def _wait_until(predicate: Callable[[], bool], *, what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


@contextlib.contextmanager
def _look_alike(argv_tail: list[str], env_overrides: dict[str, str | None]) -> Generator[int]:
    """Spawn a sleeping process whose argv/env match one evidence shape."""
    env = os.environ.copy()
    env.pop("AVA_EXEC_REQUEST_FILE", None)
    env.pop("AVA_AGENT_ID", None)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    process = subprocess.Popen(  # noqa: S603 -- test-local look-alike, fixed interpreter
        [sys.executable, "-c", "import time; time.sleep(30)", *argv_tail],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until(
            lambda: _argv_visible(process.pid, argv_tail),
            what="the look-alike process to expose its argv",
        )
        yield process.pid
    finally:
        # SIGKILL: SIGTERM=SIG_IGN inherited from the session shell would
        # leave this look-alike alive and hang the fixture teardown.
        process.kill()
        process.wait(timeout=5)


def _argv_visible(pid: int, argv_tail: list[str]) -> bool:
    try:
        argv = psutil.Process(pid).cmdline()
    except psutil.Error:
        return False
    width = len(argv_tail)
    return any(argv[i : i + width] == argv_tail for i in range(len(argv) - width + 1))


def _row(db_conn: psycopg.Connection, agent_id: int) -> tuple[object, object, str, bool]:
    row = db_conn.execute(
        "SELECT runtime_owner, incarnation_resources, status, "
        "lease_expires_at > clock_timestamp() FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    return row


def _audit_attributes(
    db: psycopg.Connection, agent_id: int, event_name: str
) -> list[dict[str, object]]:
    """The `audit_events` rows of one name for the agent, oldest first."""
    rows = db.execute(
        "SELECT attributes FROM audit_events WHERE agent_id=%s AND event_name=%s ORDER BY id",
        (agent_id, event_name),
    ).fetchall()
    db.commit()
    return [row[0] for row in rows]


async def test_legacy_null_row_admits_over_dead_local_host_without_full_ttl(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    """The #2156 shape admits at renewal silence, not at lease expiry.

    The seeded lease has 300s left (renewed 300s ago) against a 600s TTL: the
    old path refused this row for its full remaining lease; the evidence-gated
    path admits it now, because nothing on this machine can still be the
    predecessor.
    """
    agent_id, prior = _seed(db_conn, lease_s=300.0)
    successor = await _admit(aops_pool, agent_id, uuid4(), database_gate=database_gate)
    assert successor is not None
    stored_owner, resources, status, lease_fresh = _row(db_conn, agent_id)
    assert stored_owner == successor.owner
    assert status == "running"
    # Adopted before the predecessor's lease expired — the incident's stall.
    assert lease_fresh is True
    # The set stays unknown: admission never mints an empty evidence set.
    assert resources is None
    # Both audit facts commit in the admission transaction.
    assert _audit_attributes(db_conn, agent_id, "status_change") == [
        {"from": "idling", "to": "running"}
    ]
    adoption = _audit_attributes(db_conn, agent_id, "hosted_legacy_adoption")
    assert len(adoption) == 1
    assert adoption[0]["predecessor_owner"] == str(prior)
    silence = adoption[0]["lease_silence_s"]
    assert isinstance(silence, float) and silence >= 60.0


async def test_an_admission_whose_audit_fact_cannot_be_recorded_does_not_admit(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
) -> None:
    async def refuse(_conn: object, _event: object) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr("agent.ownership.hosted.record_audit_async", refuse)
    agent_id, prior = _seed(db_conn, lease_s=300.0)

    with pytest.raises(RuntimeError, match="audit write failed"):
        await _admit(aops_pool, agent_id, uuid4(), database_gate=database_gate)

    assert _row(db_conn, agent_id) == (prior, None, "idling", True)


async def test_legacy_null_row_still_waits_while_the_lease_looks_beaten(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    """A lease renewed 10s ago is not silence: the fence is not shortcut.

    Also the evidence that no blanket lease shortening was added — the
    fresh-lease shape refuses exactly as before.
    """
    agent_id, prior = _seed(db_conn, lease_s=590.0)  # 10s of silence
    assert await _admit(aops_pool, agent_id, uuid4(), database_gate=database_gate) is None
    assert _row(db_conn, agent_id) == (prior, None, "idling", True)
    assert _audit_attributes(db_conn, agent_id, "hosted_legacy_adoption") == []


async def test_legacy_null_row_refuses_while_same_home_host_daemon_lives(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    """A live same-home host daemon outranks the silence probe."""
    agent_id, prior = _seed(db_conn)
    successor_owner = uuid4()
    with _look_alike(
        ["-m", "services.agent_runner.agent_host.daemon"], {"AVA_HOME": str(ava_home())}
    ) as daemon_pid:
        _wait_until(
            lambda: daemon_pid in _evidence(agent_id).live_hosts,
            what="the daemon look-alike to appear in the evidence",
        )
        assert (
            await _admit(aops_pool, agent_id, successor_owner, database_gate=database_gate) is None
        )
    assert _row(db_conn, agent_id) == (prior, None, "idling", True)
    _wait_until(lambda: _evidence(agent_id).clean, what="the daemon look-alike to exit")
    assert (
        await _admit(aops_pool, agent_id, successor_owner, database_gate=database_gate) is not None
    )


async def test_legacy_null_row_refuses_while_an_exec_child_of_the_agent_lives(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    """A live exec child of this agent is an unresolved resource: wait."""
    agent_id, prior = _seed(db_conn)
    successor_owner = uuid4()
    request = exec_run_dir() / str(agent_id) / f"{uuid4()}.json"
    with _look_alike(
        ["-m", "agent.execution.child"],
        {
            "AVA_HOME": str(ava_home()),
            "AVA_EXEC_REQUEST_FILE": str(request),
            "AVA_AGENT_ID": str(agent_id),
        },
    ) as child_pid:
        _wait_until(
            lambda: child_pid in _evidence(agent_id).live_exec_children,
            what="the exec-child look-alike to appear in the evidence",
        )
        assert (
            await _admit(aops_pool, agent_id, successor_owner, database_gate=database_gate) is None
        )
    assert _row(db_conn, agent_id) == (prior, None, "idling", True)
    _wait_until(lambda: _evidence(agent_id).clean, what="the exec child to exit")
    assert (
        await _admit(aops_pool, agent_id, successor_owner, database_gate=database_gate) is not None
    )


async def test_legacy_null_row_ignores_a_foreign_home_host_daemon(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    tmp_path: Path,
    database_gate: ProcessDbGate,
) -> None:
    """A co-located unit's daemon is not evidence about this home."""
    agent_id, _prior = _seed(db_conn)
    with _look_alike(
        ["-m", "services.agent_runner.agent_host.daemon"],
        {"AVA_HOME": str(tmp_path / "other-home")},
    ) as daemon_pid:
        assert daemon_pid not in _evidence(agent_id).live_hosts
        assert await _admit(aops_pool, agent_id, uuid4(), database_gate=database_gate) is not None


async def test_legacy_null_row_refuses_an_unattributable_exec_child(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    """A child-shaped process without readable identity is never guessed away."""
    agent_id, prior = _seed(db_conn)
    successor_owner = uuid4()
    with _look_alike(["-m", "agent.execution.child"], {"AVA_HOME": str(ava_home())}) as child_pid:
        _wait_until(
            lambda: any(f"pid {child_pid}" in reason for reason in _evidence(agent_id).unreadable),
            what="the unattributable child to appear in the evidence",
        )
        assert (
            await _admit(aops_pool, agent_id, successor_owner, database_gate=database_gate) is None
        )
    assert _row(db_conn, agent_id) == (prior, None, "idling", True)


async def test_legacy_null_row_refuses_a_remote_machine(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    """Another machine's rows are not this host's to replace, evidence or not."""
    agent_id, prior = _seed(db_conn, machine="other-machine")
    assert await _admit(aops_pool, agent_id, uuid4(), database_gate=database_gate) is None
    assert _row(db_conn, agent_id) == (prior, None, "idling", True)


async def test_legacy_null_row_refuses_a_crash_marked_row(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    """Crash corpses keep their reaper/resurrect recovery, not this path."""
    agent_id, prior = _seed(db_conn, marked=True)
    assert await _admit(aops_pool, agent_id, uuid4(), database_gate=database_gate) is None
    assert _row(db_conn, agent_id) == (prior, None, "idling", True)


async def test_two_successors_admit_only_one_legacy_owner(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    """Concurrent successors of a dead legacy host cannot both take the row."""
    agent_id, _prior = _seed(db_conn)
    results = await asyncio.gather(
        *(_admit(aops_pool, agent_id, uuid4(), database_gate=database_gate) for _ in range(2)),
        return_exceptions=True,
    )
    admitted = [value for value in results if isinstance(value, RuntimeIncarnation)]
    assert len(admitted) == 1
    assert sum(value is None for value in results) == 1
    stored_owner, _resources, _status, _fresh = _row(db_conn, agent_id)
    assert stored_owner == admitted[0].owner


async def test_stale_proposal_cannot_adopt_a_row_that_moved_on(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
) -> None:
    """Evidence gathered for one row state is void once that state moved.

    A live owner renewing its lease — or another successor adopting — between
    the evidence scan and the row lock must defeat the proposal: the pin is
    (owner, generation, lease), re-checked inside the transaction.
    """
    from agent.ownership.hosted import _legacy_dead_host_adoption

    agent_id, prior = _seed(db_conn)
    proposal = await _legacy_dead_host_adoption(aops_pool, agent_id, "host-test", uuid4())
    assert proposal is not None and proposal.owner == prior
    # The row moves on (a renewal with the same owner) after the evidence scan.
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at = lease_expires_at + interval '30 seconds' "
        "WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()

    async def _stale(*_args: object, **_kw: object):
        return proposal

    monkeypatch.setattr("agent.ownership.hosted._legacy_dead_host_adoption", _stale)
    assert await _admit(aops_pool, agent_id, uuid4(), database_gate=database_gate) is None
    stored_owner, _resources, status, _fresh = _row(db_conn, agent_id)
    assert stored_owner == prior and status == "idling"
