"""Force acceptance cannot outrun a live hosted continuation or its exec child."""

import asyncio
import json
import subprocess
import threading
import traceback
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psutil
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import has_pending_interrupt
from agent.graph._exec_stream import StreamingTextIO
from agent.hosted_ownership import admit_hosted_runtime
from ops.ops_exit import _force_terminate_transaction
from services.agent_host.daemon import _cancel_turn_route
from services.agent_host.dispatcher import TurnScheduler
from services.agent_host.host import AgentHost
from shared.config import settings
from shared.hosted_force import original_host_force, recover_orphaned_hosted_forces
from shared.lifecycle_termination_observe import observe_applied_termination
from tests.agent.test_inbound_ownership import _agent, _insert


def _allow_model_config(
    *, model: str | None = None, config: dict[str, object] | None = None
) -> str:
    """Return the model name unchanged; fake-host tests carry no provider keys."""

    return model or "deepseek-v4-flash-vision-exp"


@pytest.fixture(autouse=True)
def _host_wakes_need_no_provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """AgentHost validates the wake's model config before admitting a turn.

    Main's reject-invalid-hosted-model-wake gate (#1494) needs a provider key
    for the cluster-default model; fake-host force-quiescence tests keep turns
    independent of installed credentials (same stance as
    tests/services/test_agent_host.py's wired fixture).
    """

    monkeypatch.setattr("services.agent_host.host.validate_model_config", _allow_model_config)


def _blocking_work(entered: threading.Event, release: threading.Event) -> None:
    entered.set()
    assert release.wait(20), "test must release the real thread"


def _observed_host(
    pool: AsyncConnectionPool, graph: Mock, patch: pytest.MonkeyPatch
) -> tuple[AgentHost, list[str]]:
    host = AgentHost(pool=pool, checkpointer=Mock(), graph=graph, machine="claim-test")
    patch.setattr(host, "_runtime_for", AsyncMock(return_value=Mock(llm=None)))
    original = host._run_turn
    errors: list[str] = []

    async def observed(agent_id: int) -> None:
        try:
            await original(agent_id)
        except BaseException:
            errors.append(traceback.format_exc())
            raise

    patch.setattr(host, "_run_turn", observed)
    return host, errors


def _configure_late_reader(kind: str, patch: pytest.MonkeyPatch, release: threading.Event) -> None:
    if kind != "reader":
        return
    from agent.graph import _exec_subprocess

    original = _exec_subprocess._drain_output

    def delayed(proc: subprocess.Popen[bytes], stream: StreamingTextIO) -> None:
        original(proc, stream)
        assert release.wait(20), "test must release real output reader"

    patch.setattr(_exec_subprocess, "_drain_output", delayed)
    patch.setattr("agent.graph._exec_process._READER_JOIN_TIMEOUT_S", 0.01)


async def _assert_pending_force(
    conn: psycopg.Connection, pool: AsyncConnectionPool, agent_id: int, command: int, chat: int
) -> None:
    assert await has_pending_interrupt(pool, agent_id)
    assert conn.execute(
        "SELECT status,applied_at IS NOT NULL,observed_at FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone() == ("claimed", True, None)
    conn.commit()  # The observer must not be a savepoint inside the earlier read transaction.
    with conn.transaction():
        assert not observe_applied_termination(conn, agent_id, "claim-test")
    assert (
        await admit_hosted_runtime(
            pool, agent_id, "claim-test", uuid4(), expected_from="terminated"
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
) -> None:
    # Simulate explicit resurrection's allocation, not a claim of RPC coverage.
    conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (agent_id,))
    conn.commit()
    replacement = AgentHost(pool=pool, checkpointer=Mock(), graph=graph, machine="claim-test")
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


@pytest.mark.parametrize("work_kind", ["thread", "exec", "reader"])
async def test_force_waits_for_real_work_and_delayed_cancel_cannot_hit_successor(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    work_kind: str,
) -> None:
    agent_id = _agent(db_conn)
    entered, release = threading.Event(), threading.Event()
    _configure_late_reader(work_kind, monkeypatch, release)
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
            from agent.graph._exec_subprocess import _run_in_subprocess

            await _run_in_subprocess(
                "from pathlib import Path\nimport time\n"
                f"Path({str(marker)!r}).touch()\n"
                + (
                    "print('reader finishes after bounded join')\n"
                    if work_kind == "reader"
                    else f"while not Path({str(release_file)!r}).exists(): time.sleep(0.01)\n"
                ),
                agent_id,
                asyncio.Event(),
                20,
                exec_dir=tmp_path,
            )
        return {"exit_requested": False, "restart_requested": False, "turn_idle": True}

    graph = Mock()
    graph.ainvoke = AsyncMock(side_effect=graph_return)
    host, errors = _observed_host(aops_pool, graph, monkeypatch)
    monkeypatch.setattr("services.agent_host.dispatcher.CANCEL_UNWIND_TIMEOUT_S", 0.05)
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
        )
        assert calls == 2
    finally:
        release.set()
        release_file.touch()
        successor_release.set()
        await scheduler.aclose()


async def test_idle_force_only_original_live_host_can_observe(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    host = AgentHost(pool=aops_pool, checkpointer=Mock(), graph=Mock(), machine="claim-test")
    assert (
        await admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", host._owner, expected_from="idling"
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
) -> None:
    """A dead host owner must not strand a force when no exec domain survived."""
    agent_id = _agent(db_conn)
    old_host = AgentHost(pool=aops_pool, checkpointer=Mock(), graph=Mock(), machine="claim-test")
    assert (
        await admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling"
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    monkeypatch.setattr("shared.hosted_force.exec_run_dir", lambda: tmp_path)

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
) -> None:
    """A request envelope survives its parent and forbids guessed quiescence."""
    agent_id = _agent(db_conn)
    old_host = AgentHost(pool=aops_pool, checkpointer=Mock(), graph=Mock(), machine="claim-test")
    assert (
        await admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", old_host._owner, expected_from="idling"
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    request = agent_dir / "req-live.json"
    request.write_text("{}")
    monkeypatch.setattr("shared.hosted_force.exec_run_dir", lambda: tmp_path)

    recovered, deferred = await recover_orphaned_hosted_forces(aops_pool, "claim-test")

    assert recovered == []
    assert deferred == {agent_id: (request,)}
    assert db_conn.execute(
        "SELECT status,applied_at IS NOT NULL,observed_at FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone() == ("claimed", True, None)
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (command,)


async def test_formatted_exec_cleanup_failure_retains_actual_resource_evidence(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agent.graph._exec_process import ExecProcessDomain
    from agent.graph._exec_result import _ExecCrashed
    from agent.graph._exec_subprocess import _run_in_subprocess
    from agent.hosted_ownership import apply_hosted_lifecycle, settle_hosted_runtime
    from shared.turn_identity import HostedTurnResources, bind_hosted_resources
    from tests.agent.test_inbound_ownership import _admit

    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    original_close = ExecProcessDomain.close

    def failed_close(domain: ExecProcessDomain) -> None:
        original_close(domain)
        raise PermissionError("injected unverifiable domain closure")

    monkeypatch.setattr(ExecProcessDomain, "close", failed_close)
    scope = HostedTurnResources()
    with bind_hosted_resources(scope):
        outcome, _ = await _run_in_subprocess(
            "print('resource-proof')", agent_id, asyncio.Event(), 10, exec_dir=tmp_path
        )
        assert isinstance(outcome, _ExecCrashed)
        assert "teardown failure" in outcome.output
        assert len(scope.unresolved) == 1
        path, domain = next(iter(scope.unresolved.items()))
        assert path.exists() and isinstance(domain, ExecProcessDomain)
        assert not scope.complete(path, object())
        assert scope.unresolved[path] is domain
        assert domain.proc.poll() is not None
        # A formatted tool failure cannot become a positive lifecycle barrier.
        assert await apply_hosted_lifecycle(aops_pool, incarnation) is None
        assert not await settle_hosted_runtime(aops_pool, incarnation)
    assert len(scope.unresolved) == 1  # cache/context reset does not erase the evidence


async def test_real_missing_executable_is_not_an_unresolved_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent.graph._exec_result import _ExecCrashed
    from agent.graph._exec_subprocess import _run_in_subprocess
    from shared.turn_identity import HostedTurnResources, bind_hosted_resources

    monkeypatch.setattr("agent.graph._exec_subprocess.sys.executable", str(tmp_path / "absent"))
    scope = HostedTurnResources()
    with bind_hosted_resources(scope):
        outcome, _ = await _run_in_subprocess(
            "raise AssertionError('must never execute')",
            None,
            asyncio.Event(),
            2,
            exec_dir=tmp_path,
        )
    assert isinstance(outcome, _ExecCrashed)
    assert "could not be spawned" in outcome.output
    assert not scope.unresolved


@pytest.mark.parametrize("failure", [PermissionError, psutil.AccessDenied])
def test_unreadable_group_member_is_not_an_empty_domain(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    from shared.exec_process_domain import _process_group_has_live_member

    process = Mock(info={"pid": 123, "status": psutil.STATUS_RUNNING})

    def iter_processes(_attrs: list[str]) -> Iterator[Mock]:
        return iter([process])

    monkeypatch.setattr(psutil, "process_iter", iter_processes)

    def unreadable(pid: int) -> int:
        raise failure()

    monkeypatch.setattr("shared.exec_process_domain.os.getpgid", unreadable)
    with pytest.raises(failure):
        _process_group_has_live_member(123)


async def test_cancel_validation_spanning_task_handoff_never_cancels_new_turn() -> None:
    first_entered, first_release = asyncio.Event(), asyncio.Event()
    second_entered, second_release = asyncio.Event(), asyncio.Event()
    validating, validated = asyncio.Event(), asyncio.Event()
    calls = 0

    async def run_turn(agent_id: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_entered.set()
            await first_release.wait()
        else:
            second_entered.set()
            await second_release.wait()

    async def validate(agent_id: int, command_id: int) -> bool:
        validating.set()
        await validated.wait()
        return True

    scheduler = TurnScheduler(run_turn)
    scheduler.wake(1)
    await first_entered.wait()
    cancellation = asyncio.create_task(scheduler.cancel_exact_force(1, 7, validate))
    await validating.wait()
    scheduler.wake(1)
    first_release.set()
    await second_entered.wait()
    validated.set()
    try:
        assert not await cancellation
        assert 1 in scheduler.active_agents
        assert calls == 2
    finally:
        second_release.set()
        await scheduler.aclose()
