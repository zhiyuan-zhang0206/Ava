"""Exec dispatch and execution-domain cancellation use the exact owner resource map."""

import asyncio
import contextlib
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest

from base.agents.incarnation.exec_owner_protocol import OwnerClosed, OwnerContext, OwnerReady
from base.agents.incarnation.resources import IncarnationResources, decode_resources
from base.agents.incarnation.tests.test_resources import _admitted
from base.native_process.runtime_incarnation import RuntimeIncarnation


async def test_real_exec_dispatch_uses_owner_and_discharges_exact_map(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.graph.exec import _owned_run
    from agent.graph.exec._result import _ExecDone
    from agent.graph.exec._subprocess import _run_in_subprocess

    target = _admitted(db_conn)

    def admitted(_agent_id: int) -> RuntimeIncarnation:
        return target

    monkeypatch.setattr(_owned_run, "current_incarnation", admitted)
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


async def test_managed_exec_streams_output_and_keepalive_before_completion(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.graph.exec import _owned_run
    from agent.graph.exec._result import _ExecDone
    from agent.graph.exec._stream import ExecOutputChunkPublisher
    from agent.graph.exec._subprocess import _run_in_subprocess

    target = _admitted(db_conn)

    def admitted(_agent_id: int) -> RuntimeIncarnation:
        return target

    monkeypatch.setattr(_owned_run, "current_incarnation", admitted)
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
    from agent.graph.exec import _owned_run
    from agent.graph.exec._subprocess import _run_in_subprocess

    target = _admitted(db_conn)

    def admitted(_agent_id: int) -> RuntimeIncarnation:
        return target

    monkeypatch.setattr(_owned_run, "current_incarnation", admitted)
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
    from agent.graph.exec import _owned_run
    from agent.graph.exec._subprocess import _run_in_subprocess

    target = _admitted(db_conn)

    def admitted(_agent_id: int) -> RuntimeIncarnation:
        return target

    monkeypatch.setattr(_owned_run, "current_incarnation", admitted)
    original_register = _owned_run._register_attached
    entered = threading.Event()
    release = threading.Event()

    def delayed_register(context: OwnerContext, ready: OwnerReady) -> None:
        entered.set()
        assert release.wait(10)
        original_register(context, ready)

    monkeypatch.setattr(_owned_run, "_register_attached", delayed_register)
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
