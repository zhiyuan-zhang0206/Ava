"""Exclusive host boot reconciles force evidence after the original owner dies."""

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.ownership import hosted
from agent.tests.claim.test_inbound_ownership import agent_row
from base.agents.incarnation import exec_request_evidence
from base.agents.incarnation.exec_request_evidence import Verdict
from base.agents.incarnation.hosted_force import recover_orphaned_hosted_forces
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.lifecycle.termination import _force_terminate_transaction
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.lifecycle.force_host_setup import (
    host_wakes_need_no_provider_credentials,
)


async def test_exclusive_host_boot_recovers_resource_free_applied_force(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """A dead host owner must not strand a force when no exec domain survived."""
    agent_id = agent_row(db_conn)
    old_host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command, _cutoff = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.exec_run_dir", lambda: tmp_path
    )

    recovered, deferred = await recover_orphaned_hosted_forces(aops_pool, "claim-test")

    assert recovered == [agent_id]
    assert deferred == {}
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("done", True)
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)


async def test_exclusive_host_boot_recovers_torn_pointer_done_force(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """A command torn into `done` with the pointer alive (task #3678) is blind to
    the claimed-only boot recovery; the widened candidate predicate settles it."""
    agent_id = agent_row(db_conn)
    old_host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.exec_run_dir", lambda: tmp_path
    )

    # Build the torn shape directly: done + applied + unobserved with the pointer
    # still alive. INSERT is outside the commit-time guard's UPDATE window, so this
    # mirrors the historical out-of-band write.
    db_conn.execute(
        "UPDATE agents_meta SET status='terminated', termination_source='user', "
        "status_changed_at=clock_timestamp() WHERE id=%s",
        (agent_id,),
    )
    row = db_conn.execute(
        "INSERT INTO inbound_messages "
        "(agent_id, content, kind, source, status, applied_at, claimed_at, "
        " target_generation, target_owner) "
        "SELECT %s, '', 'terminate', 'user', 'done', clock_timestamp(), "
        "       clock_timestamp(), runtime_generation, runtime_owner "
        "FROM agents_meta WHERE id=%s RETURNING id",
        (agent_id, agent_id),
    ).fetchone()
    assert row is not None
    command = row[0]
    db_conn.execute(
        "UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (command, agent_id)
    )
    db_conn.commit()

    recovered, deferred = await recover_orphaned_hosted_forces(aops_pool, "claim-test")

    assert recovered == [agent_id]
    assert deferred == {}
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("done", True)
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)


async def test_exclusive_host_boot_defers_force_with_persistent_exec_evidence(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """A request envelope survives its parent and forbids guessed quiescence."""
    agent_id = agent_row(db_conn)
    old_host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command, _cutoff = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    request = _aged_envelope(tmp_path, agent_id, owner=None)
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.exec_run_dir", lambda: tmp_path
    )

    recovered, deferred = await recover_orphaned_hosted_forces(aops_pool, "claim-test")

    assert recovered == []
    (entry,) = deferred[agent_id]
    # No structured attribution: retained conservatively, never quarantined.
    assert entry.verdict is Verdict.UNKNOWN
    assert entry.path == request
    assert request.exists()
    assert db_conn.execute(
        "SELECT status,applied_at IS NOT NULL,observed_at FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone() == ("claimed", True, None)
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (command,)


def _aged_envelope(
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


async def test_exclusive_host_boot_quarantines_superseded_evidence_and_recovers(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """Old-owner evidence is preserved, not deleted, and stops fencing the force."""
    agent_id = agent_row(db_conn)
    old_host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command, _cutoff = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    request = _aged_envelope(tmp_path, agent_id, owner=uuid4())
    quarantine = tmp_path / "quarantined-exec-requests"
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.exec_run_dir", lambda: tmp_path
    )
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.quarantined_exec_requests_dir",
        lambda: quarantine,
    )

    recovered, deferred = await recover_orphaned_hosted_forces(aops_pool, "claim-test")

    assert recovered == [agent_id] and deferred == {}
    assert not request.exists()
    (moved,) = quarantine.glob(f"*/{agent_id}/{request.name}")
    receipt = json.loads((moved.parent / "receipt.json").read_text())
    assert receipt["reason"] == "hosted boot recovery"
    assert receipt["entries"][0]["source"] == str(request)
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("done", True)
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)


def _unreadable_envelope(exec_dir: Path, agent_id: int, *, age_s: float) -> Path:
    """One killed parent's unreadable remnant, aged as asked."""
    agent_dir = exec_dir / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    request = agent_dir / f"req-{uuid4().hex}.json"
    request.write_text("")  # zero-byte remnant: nothing to attribute
    stamp = request.stat().st_mtime - age_s
    os.utime(request, (stamp, stamp))
    return request


def _no_process_iteration(*_args: Any, **_kwargs: Any) -> Iterator[Any]:
    """A `psutil.process_iter` stand-in yielding no processes at all."""
    return iter(())


def _hide_machine_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """This box runs other agents' exec children; isolate the test's own legs."""
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.psutil.process_iter", _no_process_iteration
    )


async def test_exclusive_host_boot_disposes_aged_unreadable_evidence_and_recovers(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """The 6285 shape: a zero-byte remnant no longer defers boot recovery."""

    agent_id = agent_row(db_conn)
    old_host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command, _cutoff = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    bound = exec_request_evidence._unreadable_expiry_age_s()
    request = _unreadable_envelope(tmp_path, agent_id, age_s=bound + 60)
    quarantine = tmp_path / "quarantined-exec-requests"
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.exec_run_dir", lambda: tmp_path
    )
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.quarantined_exec_requests_dir",
        lambda: quarantine,
    )
    _hide_machine_processes(monkeypatch)

    recovered, deferred = await recover_orphaned_hosted_forces(aops_pool, "claim-test")

    assert recovered == [agent_id] and deferred == {}
    assert not request.exists()
    (moved,) = quarantine.glob(f"*/{agent_id}/{request.name}")
    assert moved.read_bytes() == b""
    receipt = json.loads((moved.parent / "receipt.json").read_text())
    assert receipt["reason"] == "hosted boot recovery"
    assert receipt["entries"][0]["verdict"] == "disposable"
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("done", True)
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)


async def test_exclusive_host_boot_still_defers_young_unreadable_evidence(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """A fresh remnant may still settle: the bound is not a cleanup timer."""

    agent_id = agent_row(db_conn)
    old_host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command, _cutoff = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    request = _unreadable_envelope(tmp_path, agent_id, age_s=0.0)
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.exec_run_dir", lambda: tmp_path
    )
    _hide_machine_processes(monkeypatch)

    recovered, deferred = await recover_orphaned_hosted_forces(aops_pool, "claim-test")

    assert recovered == [] and set(deferred) == {agent_id}
    assert request.exists()
    assert db_conn.execute(
        "SELECT status,applied_at IS NOT NULL,observed_at FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone() == ("claimed", True, None)
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (command,)


async def test_exclusive_host_boot_defers_while_a_live_child_references_the_request(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """A live matching child defers; the same evidence recovers on the next boot."""
    agent_id = agent_row(db_conn)
    old_host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command, _cutoff = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    request = _aged_envelope(tmp_path, agent_id, owner=old_host._owner, age_s=0.0)
    quarantine = tmp_path / "quarantined-exec-requests"
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.exec_run_dir", lambda: tmp_path
    )
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.quarantined_exec_requests_dir",
        lambda: quarantine,
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=dict(os.environ, AVA_EXEC_REQUEST_FILE=str(request)),
    )
    try:
        recovered, deferred = await recover_orphaned_hosted_forces(aops_pool, "claim-test")
        assert recovered == []
        (entry,) = deferred[agent_id]
        assert entry.verdict is Verdict.LIVE and child.pid in entry.live_pids
        assert request.exists() and not quarantine.exists()
        assert db_conn.execute(
            "SELECT status,observed_at FROM inbound_messages WHERE id=%s", (command,)
        ).fetchone() == ("claimed", None)
    finally:
        # SIGKILL: a SIGTERM-ignoring session (SIG_IGN is inherited from
        # Ava shell sessions) would leave this child alive and hang here.
        child.kill()
        child.wait(timeout=5)
    # The child ended and a later boot sees the same evidence: recovery proceeds.
    stamp = request.stat().st_mtime - 3600.0
    os.utime(request, (stamp, stamp))
    recovered, deferred = await recover_orphaned_hosted_forces(aops_pool, "claim-test")
    assert recovered == [agent_id] and deferred == {}
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("done", True)


pytestmark = pytest.mark.usefixtures(host_wakes_need_no_provider_credentials.__name__)
