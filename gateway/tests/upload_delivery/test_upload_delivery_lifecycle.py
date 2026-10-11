"""Native hard-death staging, single wake, bounded copy attempts and actual future draining."""

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.agents.upload_delivery import source, storage
from base.agents.upload_delivery.models import UploadDeliveryConflictError
from base.db import Database
from base.events.live.bus import EventBus
from gateway.app import app
from gateway.tests.upload_delivery.test_upload_delivery_recovery import post, proof_for, request_for
from gateway.tests.upload_delivery.test_upload_delivery_recovery import (
    uploaded_agent as uploaded_agent,
)
from gateway.upload_delivery import worker
from tests.fixtures.gateway_config import gateway_test_client


@pytest.mark.parametrize("stage", ["before-link", "after-link"])
def test_hard_death_native_temp_not_double_charged_or_cleaned(
    uploaded_agent: tuple[TestClient, int],
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
):
    client, agent = uploaded_agent
    original = storage.publish

    def refuse(*_args: Any):
        raise OSError("source publication not started")

    monkeypatch.setattr(storage, "publish", refuse)
    assert post(client, agent).status_code == 500
    row = db_conn.execute("SELECT manifest FROM upload_delivery_batches").fetchone()
    assert row is not None
    manifest = source.Manifest.model_validate(row[0])
    directory = source.agent_upload_dir(agent, create=False).resolve()
    final = storage.object_path(directory, manifest, manifest.objects[0])
    script = """import os, sys
from pathlib import Path
from base.host import private_storage
original = private_storage.os.link
def crash(source, target):
    if sys.argv[2] == 'after-link':
        original(source, target)
    os._exit(73)
private_storage.os.link = crash
private_storage.create_private_bytes(Path(sys.argv[1]), b'bytes')
"""
    result = subprocess.run(  # noqa: S603 -- fixed interpreter/script and isolated fixture arguments
        [sys.executable, "-c", script, str(final), stage], env=dict(os.environ), check=False
    )
    assert result.returncode == 73
    temps = list(final.parent.glob(".*.tmp"))
    assert len(temps) == 1
    with app.state.db_pool.connection() as conn:
        storage.check_quota(conn, agent, directory, source.current_unit().machine, 0, 0, 5, 1)
    monkeypatch.setattr(storage, "publish", original)
    assert post(client, agent).status_code == 202
    assert final.read_bytes() == b"bytes"
    assert temps[0].exists()  # No orphan cleanup or inferred writer-dead takeover.


def test_hold_is_not_automatically_unsealed_by_late_copy_proof(
    uploaded_agent: tuple[TestClient, int], db_conn: psycopg.Connection[Any]
):
    client, agent = uploaded_agent
    request = request_for(app.state.db_pool, post(client, agent).json()["batch_id"])
    source.record_failure(
        app.state.db_pool, request.manifest.batch_id, "unsupported-receiver", hold=True
    )
    with pytest.raises(UploadDeliveryConflictError, match="held"):
        source.complete(app.state.db_pool, request, proof_for(request))
    assert source.status(app.state.db_pool, agent, request.manifest.batch_id).state == "hold"
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    "directory", ["relative", "/elsewhere", "/Downloads/AvaAgent-1/.delivered-v1/wrong"]
)
def test_unusable_native_directory_never_accepts_inbound(
    uploaded_agent: tuple[TestClient, int], db_conn: psycopg.Connection[Any], directory: str
):
    from pydantic import ValidationError

    client, agent = uploaded_agent
    request = request_for(app.state.db_pool, post(client, agent).json()["batch_id"])
    proof = proof_for(request).model_dump()
    proof["directory"] = directory
    with pytest.raises((ValidationError, UploadDeliveryConflictError)):
        source.complete(app.state.db_pool, request, source.CopyProof.model_validate(proof))
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_shutdown_drains_native_future_after_cancelled_http_wait(
    uploaded_agent: tuple[TestClient, int],
):
    recovery = worker.UploadRecovery(app.state.db_pool, app.state.db, app.state.bus)
    entered, finish = threading.Event(), threading.Event()

    def native_writer():
        entered.set()
        assert finish.wait(5)
        with app.state.db_pool.connection() as conn:
            return conn.execute("SELECT 42").fetchone()[0]

    task = asyncio.create_task(recovery.native(native_writer))
    async with asyncio.timeout(2):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    close = asyncio.create_task(recovery.close())
    try:
        await asyncio.sleep(0.02)
        assert not close.done() and len(recovery.calls) == 1
    finally:
        finish.set()
        await close
    assert not recovery.calls


@pytest.mark.asyncio
async def test_accepted_upload_wakes_once_and_a_lost_wake_is_not_repeated(
    uploaded_agent: tuple[TestClient, int], monkeypatch: pytest.MonkeyPatch
):
    client, agent = uploaded_agent
    batch_id = post(client, agent).json()["batch_id"]
    woken: list[int] = []

    def lost_wake(_db: Database, _bus: EventBus, _agent: int, iid: str):
        woken.append(int(iid))
        raise RuntimeError("redis unavailable")

    async def copied(_self: worker.UploadRecovery, request: Any):
        return proof_for(request)

    monkeypatch.setattr(worker, "publish_inbound_wake", lost_wake)
    monkeypatch.setattr(worker.UploadRecovery, "copy_proof", copied)
    recovery = worker.UploadRecovery(app.state.db_pool, app.state.db, app.state.bus)
    await recovery.round()
    await recovery.round()
    await recovery.round()
    status = source.status(app.state.db_pool, agent, batch_id)
    assert status.state == "accepted" and status.inbound_id is not None
    # One wake at acceptance; the still-pending chat is never re-announced by a scan.
    assert woken == [status.inbound_id]
    await recovery.close()


@pytest.mark.asyncio
async def test_failed_copies_hold_after_the_attempt_budget_and_alert_once(
    uploaded_agent: tuple[TestClient, int], monkeypatch: pytest.MonkeyPatch
):
    client, agent = uploaded_agent
    batch_id = post(client, agent).json()["batch_id"]
    calls: list[int] = []

    async def unreachable(_self: worker.UploadRecovery, _request: Any):
        calls.append(1)
        raise OSError("receiver unreachable")

    emitted: list[tuple[str, dict[str, Any]]] = []

    def record(_category: str, name: str, *, attributes: dict[str, Any]) -> None:
        emitted.append((name, attributes))

    monkeypatch.setattr(worker.UploadRecovery, "copy_proof", unreachable)
    monkeypatch.setattr(worker.telemetry, "emit", record)
    recovery = worker.UploadRecovery(app.state.db_pool, app.state.db, app.state.bus)
    for attempt in range(1, source.MAX_COPY_ATTEMPTS + 1):
        with app.state.db_pool.connection() as conn:
            conn.execute("UPDATE upload_delivery_batches SET next_attempt_at=now()")
        await recovery.round()
        status = source.status(app.state.db_pool, agent, batch_id)
        assert status.attempts == attempt
        assert status.state == ("hold" if attempt == source.MAX_COPY_ATTEMPTS else "pending")
    assert emitted == [
        (
            "upload_delivery_held",
            {"agent_id": agent, "batch_id": batch_id, "reason": "OSError"},
        )
    ]
    # Held means held: no later round copies it again.
    with app.state.db_pool.connection() as conn:
        conn.execute("UPDATE upload_delivery_batches SET next_attempt_at=now()")
    await recovery.round()
    assert len(calls) == source.MAX_COPY_ATTEMPTS
    await recovery.close()


@pytest.mark.asyncio
async def test_business_pause_skips_new_copy_and_db_round(
    uploaded_agent: tuple[TestClient, int], monkeypatch: pytest.MonkeyPatch
):
    client, agent = uploaded_agent
    request = request_for(app.state.db_pool, post(client, agent).json()["batch_id"])
    monkeypatch.setattr(worker, "business_paused", lambda: True)

    def forbidden(*_args: Any):
        raise AssertionError("paused source must not inspect/accept pending work")

    monkeypatch.setattr(source, "validate_pending", forbidden)
    recovery = worker.UploadRecovery(app.state.db_pool, app.state.db, app.state.bus)
    await recovery.copy(request)
    assert source.status(app.state.db_pool, agent, request.manifest.batch_id).state == "pending"
    await recovery.close()


@pytest.mark.asyncio
async def test_cancelled_shutdown_wait_still_drains_actual_native_writer(
    uploaded_agent: tuple[TestClient, int],
):
    recovery = worker.UploadRecovery(app.state.db_pool, app.state.db, app.state.bus)
    entered, finish = threading.Event(), threading.Event()

    def native_writer():
        entered.set()
        assert finish.wait(5)
        with app.state.db_pool.connection() as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)

    call = asyncio.create_task(recovery.native(native_writer))
    async with asyncio.timeout(2):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    close = asyncio.create_task(recovery.close())
    await asyncio.sleep(0.01)
    close.cancel()
    try:
        await asyncio.sleep(0.02)
        assert not close.done() and len(recovery.calls) == 1
    finally:
        finish.set()
        await call
        with pytest.raises(asyncio.CancelledError):
            await close
    assert not recovery.calls


def test_actual_gateway_lifespan_recovers_one_chat_without_ops_process(
    db_conn: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import time

    from base.config import settings
    from gateway.tests.events.test_notices_endpoint import _seed_agent
    from gateway.tests.upload_delivery.test_upload_delivery_recovery import SECRET

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", SECRET)
    unit = source.current_unit()
    agent = _seed_agent(db_conn)
    db_conn.execute(
        "INSERT INTO machines(name,gateway_url) VALUES (%s,'http://runner:7001') "
        "ON CONFLICT(name) DO UPDATE SET gateway_url=excluded.gateway_url",
        (unit.machine,),
    )
    db_conn.execute(
        "INSERT INTO machine_units(machine_name,home,serve_agent_runner,url) "
        "VALUES (%s,%s,true,'http://runner:7001')",
        (unit.machine, unit.home),
    )
    db_conn.execute("UPDATE agents_meta SET machine=%s WHERE id=%s", (unit.machine, agent))
    db_conn.commit()
    with gateway_test_client(app, headers={"Authorization": f"Bearer {SECRET}"}) as client:
        response = post(client, agent)
        assert response.status_code == 202
        deadline = time.monotonic() + 10
        while client.get(response.json()["status_url"]).json()["state"] != "accepted":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        recovery = app.state.upload_recovery
        assert recovery.task is not None and not recovery.task.done()
        assert db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
        ).fetchone() == (1,)
    assert recovery.task.done() and not recovery.calls
    assert app.state.db_pool.closed


def test_overlapping_gateway_lifespans_stop_their_original_upload_workers(
    db_conn: psycopg.Connection[Any],
) -> None:
    # Both existing HTTP fixture consumers nest TestClient(app). Shared state
    # exposes the newer worker, but each lifespan must drain its own TaskGroup.
    with gateway_test_client(app):
        process = app.state.gateway_process
        pipeline = process.event_pipeline()
        original = app.state.upload_recovery
        assert original.task is not None and not original.stopped.is_set()
        with gateway_test_client(app):
            assert app.state.gateway_process is process
            assert app.state.process_image is process.image
            assert process.event_pipeline() is pipeline
            replacement = app.state.upload_recovery
            assert replacement is not original
            assert replacement.task is not None and not replacement.stopped.is_set()
        assert not pipeline.stopped
        assert replacement.stopped.is_set() and replacement.task.done()
        assert not original.stopped.is_set() and not original.task.done()
    assert pipeline.stopped
    assert original.stopped.is_set() and original.task.done()
    assert not original.calls and not replacement.calls
