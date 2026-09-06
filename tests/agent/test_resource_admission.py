"""Actual resource admission and exact completion preserve predecessor facts."""

import asyncio
import contextlib
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from shared.exec_owner_protocol import OwnerClosed, OwnerContext, OwnerReady
from shared.incarnation_resources import (
    IncarnationResources,
    ResourceEvidenceError,
    ResourceProcess,
    attach_exec,
    complete_exec,
    decode_resources,
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
    assert isinstance(result, _ExecDone), result.output
    assert payload is not None and payload.kind == "done"
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone()
    assert row is not None and row[0]["requests"] == {}
    receipts = list((tmp_path / str(target.agent_id) / "domains").glob("*/owner.closed"))
    assert len(receipts) == 1


async def test_force_at_owner_ready_leaves_no_resurrection_blocker(  # noqa: PLR0915 -- one synchronized race proof.
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force before native attachment cannot freeze an unattached reservation."""
    from agent.graph import _exec_owned_run
    from agent.graph._exec_result import _ExecCrashed
    from agent.graph._exec_subprocess import _run_in_subprocess
    from agent.hosted_ownership import admit_hosted_runtime
    from shared.hosted_force import original_host_force

    target = _admitted(db_conn)
    marker = tmp_path / "must-not-run"

    def admitted(_agent_id: int) -> RuntimeIncarnation:
        return target

    monkeypatch.setattr(_exec_owned_run, "current_incarnation", admitted)
    original_validate = _exec_owned_run.validate_native_ready
    ready = threading.Event()
    force_done = threading.Event()
    failures: list[BaseException] = []
    commands: list[int] = []
    path = db_conn.execute("SHOW search_path").fetchone()
    assert path is not None
    db_conn.commit()

    def force_after_ready() -> None:
        try:
            assert ready.wait(10)
            with psycopg.connect(db_conn.info.dsn) as writer:
                writer.execute("SELECT set_config('search_path',%s,false)", (path[0],))
                writer.commit()
                with writer.transaction():
                    commands.append(_force(writer, target))
        except BaseException as exc:
            failures.append(exc)
        finally:
            force_done.set()

    def validate_then_wait(
        receipt: OwnerReady,
        launcher_pid: int,
        launcher_birth: float,
        context_path: Path,
    ) -> None:
        original_validate(receipt, launcher_pid, launcher_birth, context_path)
        ready.set()
        assert force_done.wait(10)

    monkeypatch.setattr(_exec_owned_run, "validate_native_ready", validate_then_wait)
    thread = threading.Thread(target=force_after_ready)
    thread.start()
    result, _ = await _run_in_subprocess(
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
        target.agent_id,
        asyncio.Event(),
        30,
        exec_dir=tmp_path,
    )
    thread.join(10)
    assert not thread.is_alive() and failures == [] and len(commands) == 1
    assert isinstance(result, _ExecCrashed)
    assert not marker.exists()
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone()
    assert row is not None
    frozen = decode_resources(row[0])
    assert isinstance(frozen, IncarnationResources)
    assert frozen.requests == {}
    db_conn.commit()
    assert await original_host_force(
        aops_pool,
        target.agent_id,
        target.owner,
        "resource-test",
        command_id=commands[0],
        quiescent=True,
    )

    db_conn.execute(
        "UPDATE agents_meta SET status='idling',termination_source=NULL WHERE id=%s",
        (target.agent_id,),
    )
    db_conn.commit()
    successor = await admit_hosted_runtime(
        aops_pool,
        target.agent_id,
        "resource-test",
        uuid4(),
        expected_from="idling",
    )
    assert successor is not None and successor.generation != target.generation


async def test_managed_exec_streams_output_and_keepalive_before_completion(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.graph import _exec_owned_run
    from agent.graph._exec_result import _ExecDone
    from agent.graph._exec_stream import ExecOutputChunkPublisher
    from agent.graph._exec_subprocess import _run_in_subprocess

    target = _admitted(db_conn)

    def admitted(_agent_id: int) -> RuntimeIncarnation:
        return target

    monkeypatch.setattr(_exec_owned_run, "current_incarnation", admitted)
    output_seen = asyncio.Event()
    keepalive_seen = asyncio.Event()
    events: list[dict[str, object]] = []

    def record(raw: str) -> None:
        event = json.loads(raw)
        events.append(event)
        (keepalive_seen if event["keepalive"] else output_seen).set()

    emitter = MagicMock()
    emitter.emit.side_effect = record
    publisher = ExecOutputChunkPublisher(emitter, agent_id=target.agent_id, item_id="7.0")
    task = asyncio.create_task(
        _run_in_subprocess(
            "import time; print('managed-first', flush=True); time.sleep(1.4)",
            target.agent_id,
            asyncio.Event(),
            30,
            publisher,
            exec_dir=tmp_path,
        )
    )
    try:
        await asyncio.wait_for(output_seen.wait(), 5)
        assert not task.done()
        assert any(event["content"] == "managed-first\n" for event in events)
        await asyncio.wait_for(keepalive_seen.wait(), 2)
        assert not task.done()
        result, _ = await task
        assert isinstance(result, _ExecDone)
        assert result.output == "managed-first\n"
        assert "".join(str(event["content"]) for event in events) == "managed-first\n"
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_execution_domain_cancellation_consumes_exact_owner_receipt(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation returns only after the attached allocation is discharged."""
    from agent.graph import _exec_owned_run
    from agent.graph._exec_subprocess import _run_in_subprocess

    target = _admitted(db_conn)

    def admitted(_agent_id: int) -> RuntimeIncarnation:
        return target

    monkeypatch.setattr(_exec_owned_run, "current_incarnation", admitted)
    task = asyncio.create_task(
        _run_in_subprocess(
            "import time; print('managed-started', flush=True); time.sleep(60)",
            target.agent_id,
            asyncio.Event(),
            30,
            exec_dir=tmp_path,
        )
    )
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        row = db_conn.execute(
            "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
        ).fetchone()
        assert row is not None
        resources = decode_resources(row[0])
        if (
            isinstance(resources, IncarnationResources)
            and len(resources.requests) == 1
            and next(iter(resources.requests.values())).owner_process is not None
        ):
            break
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)

    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    receipts = list((tmp_path / str(target.agent_id) / "domains").glob("*/owner.closed"))
    assert len(receipts) == 1
    receipt = OwnerClosed.model_validate_json(receipts[0].read_bytes())
    assert receipt.reason == "host_eof"
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone()
    assert row is not None
    resources = decode_resources(row[0])
    assert isinstance(resources, IncarnationResources)
    assert resources.requests == {}


async def test_execution_domain_cancellation_waits_for_inflight_registration(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled to_thread caller cannot orphan a later registration commit."""
    from agent.graph import _exec_owned_run
    from agent.graph._exec_subprocess import _run_in_subprocess

    target = _admitted(db_conn)

    def admitted(_agent_id: int) -> RuntimeIncarnation:
        return target

    monkeypatch.setattr(_exec_owned_run, "current_incarnation", admitted)
    original_register = _exec_owned_run._register_attached
    entered = threading.Event()
    release = threading.Event()

    def delayed_register(context: OwnerContext, ready: OwnerReady) -> None:
        entered.set()
        assert release.wait(10)
        original_register(context, ready)

    monkeypatch.setattr(_exec_owned_run, "_register_attached", delayed_register)
    task = asyncio.create_task(
        _run_in_subprocess(
            "raise AssertionError('host cancellation must win before user code')",
            target.agent_id,
            asyncio.Event(),
            30,
            exec_dir=tmp_path,
        )
    )
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    deadline = asyncio.get_running_loop().time() + 10
    receipts: list[Path] = []
    while not receipts:
        receipts = list((tmp_path / str(target.agent_id) / "domains").glob("*/owner.closed"))
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    receipt = OwnerClosed.model_validate_json(receipts[0].read_bytes())
    assert receipt.reason == "host_eof"
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone()
    assert row is not None
    resources = decode_resources(row[0])
    assert isinstance(resources, IncarnationResources)
    assert resources.requests == {}


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


def test_actual_owner_receipt_recovers_only_exact_persisted_allocation(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import exec_owner_recovery
    from tests.agent.test_exec_owner_entry import _context, _ready, _start

    target = _admitted(db_conn)
    context = _context(tmp_path, target.agent_id)
    directory = tmp_path / str(target.agent_id) / "domains" / str(context.allocation.request)
    directory.mkdir(parents=True)
    request = directory / context.request_path.name
    context.request_path.rename(request)
    context = context.model_copy(
        update={
            "agent_id": target.agent_id,
            "generation": target.generation,
            "runtime_owner": target.owner,
            "request_path": request,
            "result_path": directory / "result.json",
        }
    )
    monkeypatch.setattr(exec_owner_recovery, "exec_run_dir", lambda: tmp_path)
    with db_conn.transaction():
        register_exec(db_conn, target, context.allocation)
    proc = _start(directory, context)
    try:
        ready = _ready(directory, proc)
        with db_conn.transaction():
            attach_exec(db_conn, target, context.allocation, ready.allocation)
        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
        receipt = directory / "owner.closed"
        withheld = directory / "withheld.closed"
        receipt.rename(withheld)
        exec_owner_recovery.recover_local_resources(target.agent_id, "resource-test")
        row = db_conn.execute(
            "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
        ).fetchone()
        assert row is not None and str(context.allocation.request) in row[0]["requests"]
        db_conn.commit()
        withheld.rename(receipt)
        exec_owner_recovery.recover_local_resources(target.agent_id, "resource-test")
        row = db_conn.execute(
            "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
        ).fetchone()
        assert row is not None and row[0]["requests"] == {}
        # Replaying the same exact observation is a no-op, not a new admission.
        db_conn.commit()
        exec_owner_recovery.recover_local_resources(target.agent_id, "resource-test")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
