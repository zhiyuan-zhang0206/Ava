"""Force acceptance cannot outrun a live hosted continuation or its exec child."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import traceback
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import has_pending_interrupt
from agent.graph.exec._subprocess import _run_in_subprocess
from agent.ownership import hosted
from agent.tests.claim.test_inbound_ownership import _agent, _insert
from base.agents.context import AvaContext
from base.agents.incarnation import exec_request_evidence
from base.agents.incarnation.exec_request_evidence import Verdict
from base.agents.incarnation.hosted_force import original_host_force, recover_orphaned_hosted_forces
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.native_process.turn_identity import HostedTurnResources
from ops.agents.resurrection_retry import ResurrectSettlementDeferredError
from ops.agents.wake import resurrect_agent
from ops.lifecycle.termination import _force_terminate_transaction
from services.agent_runner.agent_host.daemon import _cancel_turn_route
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.host import AgentHost
from tests.fixtures.pin_agent import exec_context as ctx_of


def _allow_model_config(
    *,
    model: str | None = None,
    overrides: ModelOverrides | None = None,
    catalog: ModelCatalog,
    llm_override: str,
) -> str:
    """Return the model name unchanged; fake-host tests carry no provider keys."""
    del overrides, catalog, llm_override
    return model or "deepseek-v4-flash-vision-exp"


@pytest.fixture(autouse=True)
def _host_wakes_need_no_provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fake-host wakes independent of installed provider credentials."""
    monkeypatch.setattr(
        "services.agent_runner.agent_host.runtime.validate_model_config", _allow_model_config
    )


def _blocking_work(entered: threading.Event, release: threading.Event) -> None:
    entered.set()
    assert release.wait(20), "test must release the real thread"


def _observed_host(
    pool: AsyncConnectionPool,
    graph: Mock,
    patch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
) -> tuple[AgentHost, list[str]]:
    host = AgentHost(
        pool=pool,
        checkpointer=Mock(),
        graph=graph,
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    patch.setattr(host, "_runtime_for", AsyncMock(return_value=Mock(llm=None)))
    original = host._run_turn
    errors: list[str] = []

    async def observed(agent_id: int, *, resources: HostedTurnResources | None) -> None:
        try:
            await original(agent_id, resources=resources)
        except BaseException:
            errors.append(traceback.format_exc())
            raise

    patch.setattr(host, "_run_turn", observed)
    return host, errors


def _configure_late_reader(
    kind: str, patch: pytest.MonkeyPatch, release: threading.Event, agent_id: int
) -> None:
    if kind != "reader":
        return
    reader_name = f"exec-reader-{agent_id}"
    original_run = threading.Thread.run
    original_join = threading.Thread.join

    def delayed(thread: threading.Thread) -> None:
        original_run(thread)
        if thread.name == reader_name:
            assert release.wait(20), "test must release real output reader"

    def bounded_join(thread: threading.Thread, timeout: float | None = None) -> None:
        if thread.name == reader_name and timeout is not None:
            timeout = min(timeout, 0.01)
        original_join(thread, timeout)

    patch.setattr(threading.Thread, "run", delayed)
    patch.setattr(threading.Thread, "join", bounded_join)


async def _assert_pending_force(
    conn: psycopg.Connection, pool: AsyncConnectionPool, agent_id: int, command: int, chat: int
) -> None:
    assert await has_pending_interrupt(pool, agent_id, incarnation=None, work=None)
    assert conn.execute(
        "SELECT status,applied_at IS NOT NULL,observed_at FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone() == ("claimed", True, None)
    conn.commit()
    handles = Database.from_settings(), EventBus.from_settings()
    with pytest.raises(ResurrectSettlementDeferredError):
        await asyncio.to_thread(resurrect_agent, *handles, agent_id, resurrected_by="user")
    assert (
        await hosted.admit_hosted_runtime(
            pool, agent_id, "claim-test", uuid4(), expected_from="terminated", db=handles[0]
        )
        is None
    )
    assert conn.execute("SELECT status FROM inbound_messages WHERE id=%s", (chat,)).fetchone() == (
        "pending",
    )
    conn.commit()


async def _prove_successor_ignores_old_cancel(
    conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    patch: pytest.MonkeyPatch,
    agent_id: int,
    graph: Mock,
    payload: bytes,
    entered: asyncio.Event,
    release: asyncio.Event,
    *,
    model_catalog: ModelCatalog,
) -> None:
    # Simulate explicit resurrection's allocation, not a claim of RPC coverage.
    conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (agent_id,))
    conn.commit()
    replacement = AgentHost(
        pool=pool,
        checkpointer=Mock(),
        graph=graph,
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    patch.setattr(replacement, "_runtime_for", AsyncMock(return_value=Mock()))
    scheduler = TurnScheduler(replacement.run_turn)
    scheduler.wake(agent_id)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        route = _cancel_turn_route(scheduler, replacement)
        _, response, _ = await route(payload)
        assert json.loads(response) == {"cancelled": False}
        assert agent_id in scheduler.active_agents
        release.set()
        async with asyncio.timeout(3):
            while agent_id in scheduler.active_agents:
                await asyncio.sleep(0.01)
    finally:
        release.set()
        await scheduler.aclose()


def _exec_context(hosted: AvaContext) -> AvaContext:
    """The fake host is SQL-only; exec keeps the test SDK clients and actual custody."""
    context = replace(
        ctx_of(hosted.require_identity().agent_id),
        original_incarnation=hosted.original_incarnation,
        native_work=hosted.native_work,
        hosted_resources=hosted.hosted_resources,
    )
    assert context.hosted_resources is hosted.hosted_resources
    return context


@pytest.mark.parametrize("work_kind", ["thread", "exec", "reader"])
async def test_force_waits_for_real_work_and_delayed_cancel_cannot_hit_successor(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    work_kind: str,
    database: Database,
    *,
    model_catalog: ModelCatalog,
) -> None:
    agent_id = _agent(db_conn)
    entered, release = threading.Event(), threading.Event()
    _configure_late_reader(work_kind, monkeypatch, release, agent_id)
    marker, release_file = tmp_path / "entered", tmp_path / "release"
    successor_entered, successor_release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def graph_return(*args: object, **kwargs: object) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        if calls > 1:
            successor_entered.set()
            await successor_release.wait()
        elif work_kind == "thread":
            await asyncio.to_thread(_blocking_work, entered, release)
        else:
            await _run_in_subprocess(
                database,
                "from pathlib import Path\nimport time\n"
                f"Path({str(marker)!r}).touch()\n"
                + (
                    "print('reader finishes after bounded join')\n"
                    if work_kind == "reader"
                    else f"while not Path({str(release_file)!r}).exists(): time.sleep(0.01)\n"
                ),
                _exec_context(cast(AvaContext, kwargs["context"])),
                asyncio.Event(),
                20,
                exec_dir=tmp_path,
                accumulation_max_chars=1_000_000,
            )
        return {"exit_requested": False, "restart_requested": False, "turn_idle": True}

    graph = Mock()
    graph.ainvoke = AsyncMock(side_effect=graph_return)
    host, errors = _observed_host(aops_pool, graph, monkeypatch, model_catalog=model_catalog)
    monkeypatch.setattr("services.agent_runner.agent_host.dispatcher.CANCEL_UNWIND_TIMEOUT_S", 0.05)
    scheduler = TurnScheduler(host.run_turn)
    scheduler.wake(agent_id)
    try:
        async with asyncio.timeout(15):
            while not (entered.is_set() if work_kind == "thread" else marker.exists()):
                assert not errors, "\n".join(errors)
                await asyncio.sleep(0.01)
        with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
            _, _, _, command = await asyncio.to_thread(
                _force_terminate_transaction, agent_id, pool, source="user"
            )
        chat = _insert(db_conn, agent_id)
        payload = json.dumps({"agent_id": agent_id, "command_id": command}).encode()
        status, response, _ = await _cancel_turn_route(scheduler, host)(payload)
        assert status == 200 and json.loads(response) == {"cancelled": False}
        assert agent_id in scheduler.active_agents
        await _assert_pending_force(db_conn, aops_pool, agent_id, command, chat)
        release.set()
        release_file.touch()
        async with asyncio.timeout(10):
            while agent_id in scheduler.active_agents:
                await asyncio.sleep(0.01)
        assert db_conn.execute(
            "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (command,)
        ).fetchone() == ("done", True)
        await _prove_successor_ignores_old_cancel(
            db_conn,
            aops_pool,
            monkeypatch,
            agent_id,
            graph,
            payload,
            successor_entered,
            successor_release,
            model_catalog=model_catalog,
        )
        assert calls == 2
    finally:
        release.set()
        release_file.touch()
        successor_release.set()
        await scheduler.aclose()


async def test_idle_force_only_original_live_host_can_observe(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    *,
    model_catalog: ModelCatalog,
) -> None:
    agent_id = _agent(db_conn)
    host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    assert not await original_host_force(
        aops_pool, agent_id, uuid4(), "claim-test", command_id=command, quiescent=True
    )
    assert [wake.agent_id for wake in await host.pending_inbound_wakes(0)] == [agent_id]
    await host.run_turn(agent_id)
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("done", True)


async def test_exclusive_host_boot_recovers_resource_free_applied_force(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    *,
    model_catalog: ModelCatalog,
) -> None:
    """A dead host owner must not strand a force when no exec domain survived."""
    agent_id = _agent(db_conn)
    old_host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command = await asyncio.to_thread(
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
) -> None:
    """A command torn into `done` with the pointer alive (task #3678) is blind to
    the claimed-only boot recovery; the widened candidate predicate settles it."""
    agent_id = _agent(db_conn)
    old_host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
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
) -> None:
    """A request envelope survives its parent and forbids guessed quiescence."""
    agent_id = _agent(db_conn)
    old_host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command = await asyncio.to_thread(
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
) -> None:
    """Old-owner evidence is preserved, not deleted, and stops fencing the force."""
    agent_id = _agent(db_conn)
    old_host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command = await asyncio.to_thread(
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
) -> None:
    """The 6285 shape: a zero-byte remnant no longer defers boot recovery."""

    agent_id = _agent(db_conn)
    old_host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command = await asyncio.to_thread(
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
) -> None:
    """A fresh remnant may still settle: the bound is not a cleanup timer."""

    agent_id = _agent(db_conn)
    old_host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command = await asyncio.to_thread(
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
) -> None:
    """A live matching child defers; the same evidence recovers on the next boot."""
    agent_id = _agent(db_conn)
    old_host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command = await asyncio.to_thread(
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
