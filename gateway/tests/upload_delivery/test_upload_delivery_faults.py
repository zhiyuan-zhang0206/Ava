"""Remote copy and transactional inbound response-loss fault boundaries."""

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx2 as httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base.agents.upload_delivery import source
from base.agents.upload_delivery.models import CopyProof, ReceiveRequest
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.lm.catalog import ModelCatalog
from gateway.app import app
from gateway.tests.upload_delivery.test_upload_delivery_recovery import (
    post,
    proof_for,
    request_for,
)
from gateway.tests.upload_delivery.test_upload_delivery_recovery import (
    uploaded_agent as uploaded_agent,
)
from gateway.upload_delivery import worker
from ops import upload_delivery as receiver
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable
from ops.rpc_schemas import OpStatus
from services.agent_runner.agent_ops.dispatch_sync import dispatch_sync


@pytest.mark.parametrize("stage", ["before", "after"])
def test_source_ready_commit_failure_or_lost_receipt_recovers(
    uploaded_agent: tuple[TestClient, int],
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
):
    client, agent = uploaded_agent
    original = source.write_transaction
    count = 0

    @contextmanager
    def faulty(pool: ConnectionPool):
        nonlocal count
        count += 1
        this = count
        with original(pool) as conn:
            yield conn
            if this == 2 and stage == "before":
                raise psycopg.OperationalError("source ready commit failure")
        if this == 2 and stage == "after":
            raise psycopg.OperationalError("source ready response lost")

    monkeypatch.setattr(source, "write_transaction", faulty)
    assert post(client, agent).status_code == 500
    assert db_conn.execute("SELECT state FROM upload_delivery_batches").fetchone() == (
        "receiving" if stage == "before" else "pending",
    )
    monkeypatch.setattr(source, "write_transaction", original)
    recovered = post(client, agent)
    assert recovered.status_code == 202
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (1,)
    assert client.get(recovered.json()["status_url"] + "/objects/0").content == b"bytes"


@pytest.mark.parametrize("stage", ["before", "after"])
def test_inbound_transaction_rollback_and_commit_ack_loss(
    uploaded_agent: tuple[TestClient, int],
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
):
    client, agent = uploaded_agent
    request = request_for(app.state.db_pool, post(client, agent).json()["batch_id"])
    proof = proof_for(request)
    original = source.write_transaction

    @contextmanager
    def faulty(pool: ConnectionPool):
        with original(pool) as conn:
            yield conn
            if stage == "before":
                raise psycopg.OperationalError("inbound commit failure")
        if stage == "after":
            raise psycopg.OperationalError("inbound commit ack lost")

    monkeypatch.setattr(source, "write_transaction", faulty)
    with pytest.raises(psycopg.OperationalError):
        source.complete(app.state.db_pool, request, proof)
    expected = 0 if stage == "before" else 1
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (expected,)
    monkeypatch.setattr(source, "write_transaction", original)
    iid = source.complete(app.state.db_pool, request, proof)
    assert source.complete(app.state.db_pool, request, proof) == iid
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)
    assert db_conn.execute(
        "SELECT source, source_verified_by, source_transport FROM inbound_messages WHERE id=%s",
        (iid,),
    ).fetchone() == ("user", "cluster_bearer", "http")


@pytest.mark.parametrize("version", [None, True, 1.0, 2, "1"])
def test_explicit_native_request_and_proof_versions_fail_fast(
    uploaded_agent: tuple[TestClient, int],
    monkeypatch: pytest.MonkeyPatch,
    version: object,
    *,
    config_authority: ConfigAuthority,
):
    client, agent = uploaded_agent
    request = request_for(app.state.db_pool, post(client, agent).json()["batch_id"])
    for model, payload in (
        (ReceiveRequest, request.model_dump()),
        (CopyProof, proof_for(request).model_dump()),
    ):
        if version is None:
            del payload["version"]
        else:
            payload["version"] = version
        with pytest.raises(ValidationError):
            model.model_validate(payload)
    payload = request.model_dump()
    if version is None:
        del payload["version"]
    else:
        payload["version"] = version
    with pytest.raises(ValidationError):
        dispatch_sync(
            "upload-receive-v1",
            payload,
            pool=app.state.db_pool,
            db=app.state.db,
            authority=config_authority,
            image=app.state.process_image,
        )


@pytest.mark.asyncio
async def test_real_remote_receiver_after_fsync_response_loss_one_inbound(
    uploaded_agent: tuple[TestClient, int],
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    config_authority: ConfigAuthority,
):
    client, agent = uploaded_agent
    source_unit = receiver.current_unit()
    target = source_unit.model_copy(update={"machine": "remote", "home": "/remote/unit"})
    db_conn.execute("INSERT INTO machines(name,gateway_url) VALUES ('remote','http://remote:7001')")
    db_conn.execute(
        "INSERT INTO machine_units(machine_name,home,serve_agent_runner,url) "
        "VALUES ('remote','/remote/unit',true,'http://remote:7001')"
    )
    db_conn.execute("UPDATE agents_meta SET machine='remote' WHERE id=%s", (agent,))
    db_conn.commit()
    first = post(client, agent).json()
    request = request_for(app.state.db_pool, first["batch_id"])
    remote_dir = tmp_path / "remote-disk" / f"AvaAgent-{agent}"
    monkeypatch.setattr(receiver, "current_unit", lambda: target)

    def directory(_id: int, **_kwargs: Any) -> Path:
        return remote_dir

    monkeypatch.setattr(receiver, "agent_upload_dir", directory)

    def get(url: str, **_kwargs: Any):
        # A receiver file/network phase has released its own DB connection.
        with app.state.db_pool.connection(timeout=1) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        result = client.get(url.removeprefix(receiver.gateway_api_base().rstrip("/")))
        return httpx.Response(
            result.status_code, content=result.content, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(receiver, "http_get", get)
    calls = 0

    async def dispatch(
        _db: Database, machine: str, kind: str, payload: dict[str, Any], **kwargs: Any
    ):
        nonlocal calls
        assert machine == "remote" and kind == "upload-receive-v1"
        assert kwargs.get("idempotency_key") is None
        calls += 1
        status, proof = await asyncio.to_thread(
            dispatch_sync,
            kind,
            payload,
            image=app.state.process_image,
            pool=app.state.db_pool,
            db=app.state.db,
            authority=config_authority,
        )
        assert status == OpStatus.COMPLETED
        if calls == 1:
            raise ClusterOpUnreachable("remote fsync/ready succeeded but response lost")
        return proof

    monkeypatch.setattr(worker, "dispatch_to_machine", dispatch)
    recovery = worker.UploadRecovery(app.state.db_pool, app.state.db, app.state.bus)
    await recovery.copy(request)
    assert source.status(app.state.db_pool, agent, first["batch_id"]).state == "pending"
    assert db_conn.execute(
        "SELECT count(*) FROM upload_delivery_copies WHERE ready_at IS NOT NULL"
    ).fetchone() == (1,)
    await recovery.copy(request)
    assert source.status(app.state.db_pool, agent, first["batch_id"]).state == "accepted"
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)
    assert (
        remote_dir / ".delivered-v1" / request.manifest.batch_id / request.manifest.objects[0].name
    ).read_bytes() == b"bytes"
    await recovery.close()


@pytest.mark.asyncio
async def test_unknown_remote_kind_holds_without_legacy_fallback(
    uploaded_agent: tuple[TestClient, int],
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
):
    from services.agent_runner.agent_ops import daemon

    client, agent = uploaded_agent
    request = request_for(app.state.db_pool, post(client, agent).json()["batch_id"])
    request = request.model_copy(
        update={"target": request.target.model_copy(update={"home": "/old"})}
    )
    original = daemon.is_op_kind

    def old_kind(value: str) -> bool:
        return value != "upload-receive-v1" and original(value)

    monkeypatch.setattr(daemon, "is_op_kind", old_kind)
    with daemon._op_thread_pool() as executor:
        status, result = await daemon._dispatch(
            "upload-receive-v1",
            request.model_dump(),
            active_ops={},
            workers=set(),
            pool=app.state.db_pool,
            executor=executor,
            authority=config_authority,
            catalog=model_catalog,
            database=lambda: app.state.db,
            image=app.state.process_image,
        )
    assert status == OpStatus.FAILED and "unknown kind" in str(result["error"])
    # Source does not reinterpret an unsupported version as legacy upload_receive.
    recovery = worker.UploadRecovery(app.state.db_pool, app.state.db, app.state.bus)

    async def dispatch(*_args: Any, **_kwargs: Any):
        raise ClusterOpFailed(result)

    monkeypatch.setattr(worker, "dispatch_to_machine", dispatch)

    def eligible(*_args: Any) -> bool:
        return True

    monkeypatch.setattr(source, "validate_pending", eligible)
    await recovery.copy(request)
    assert source.status(app.state.db_pool, agent, request.manifest.batch_id).state == "hold"
    await recovery.close()


@pytest.mark.parametrize("stage", ["before", "after"])
def test_receiver_ready_commit_boundary_recovers_without_second_copy(
    uploaded_agent: tuple[TestClient, int],
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
):
    client, agent = uploaded_agent
    request = request_for(app.state.db_pool, post(client, agent).json()["batch_id"])
    original = receiver.write_transaction
    count = 0

    @contextmanager
    def faulty(pool: ConnectionPool):
        nonlocal count
        count += 1
        current = count
        with original(pool) as conn:
            yield conn
            if current == 2 and stage == "before":
                raise psycopg.OperationalError("receiver ready rollback")
        if current == 2 and stage == "after":
            raise psycopg.OperationalError("receiver ready ack lost")

    monkeypatch.setattr(receiver, "write_transaction", faulty)
    with pytest.raises(psycopg.OperationalError):
        receiver.receive(app.state.db_pool, request)
    monkeypatch.setattr(receiver, "write_transaction", original)
    proof = receiver.receive(app.state.db_pool, request)
    assert proof == receiver.receive(app.state.db_pool, request)
    assert db_conn.execute(
        "SELECT count(*) FROM upload_delivery_copies WHERE ready_at IS NOT NULL"
    ).fetchone() == (1,)


def test_concurrent_receivers_and_inbound_acceptance_one_identity(
    uploaded_agent: tuple[TestClient, int], db_conn: psycopg.Connection[Any]
):
    from concurrent.futures import ThreadPoolExecutor

    client, agent = uploaded_agent
    request = request_for(app.state.db_pool, post(client, agent).json()["batch_id"])

    def copied(_index: int) -> CopyProof:
        return receiver.receive(app.state.db_pool, request)

    def completed(proof: CopyProof) -> int:
        return source.complete(app.state.db_pool, request, proof)

    with ThreadPoolExecutor(max_workers=3) as executor:
        proofs = list(executor.map(copied, range(3)))
        receipts = list(executor.map(completed, proofs))
    assert proofs[0] == proofs[1] == proofs[2]
    assert len(set(receipts)) == 1
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_copies").fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)


def test_receiver_changed_manifest_and_deleted_source_url_fail_without_false_ready(
    uploaded_agent: tuple[TestClient, int],
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_authority: ConfigAuthority,
):
    client, agent = uploaded_agent
    request = request_for(app.state.db_pool, post(client, agent).json()["batch_id"])
    proof = receiver.receive(app.state.db_pool, request)
    item = request.manifest.objects[0]
    changed = request.model_copy(
        update={
            "manifest": request.manifest.model_copy(
                update={"objects": [item.model_copy(update={"sha256": "0" * 64})]}
            )
        }
    )
    from base.agents.upload_delivery.models import UploadDeliveryConflictError

    with pytest.raises(UploadDeliveryConflictError):
        receiver.receive(app.state.db_pool, changed)
    path = Path(proof.directory) / item.name
    path.unlink()

    def gone(url: str, **_kwargs: Any):
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(receiver, "http_get", gone)
    status, result = dispatch_sync(
        "upload-receive-v1",
        request.model_dump(),
        pool=app.state.db_pool,
        db=app.state.db,
        authority=config_authority,
        image=app.state.process_image,
    )
    assert status == OpStatus.FAILED and result["reason"] == "upload-source-unavailable-v1"
    assert not path.exists()


def test_actual_old_gateway_has_no_guarded_upload_admission(
    uploaded_agent: tuple[TestClient, int], db_conn: psycopg.Connection[Any]
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gateway.routers.upload.router import router as old_routes

    _client, agent = uploaded_agent
    old = FastAPI()
    old.include_router(old_routes)
    with TestClient(old) as client:
        result = post(client, agent)
    assert result.status_code in (404, 405)
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (0,)


def test_single_connection_receiver_releases_pool_across_http(
    uploaded_agent: tuple[TestClient, int], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    client, agent = uploaded_agent
    request = request_for(app.state.db_pool, post(client, agent).json()["batch_id"])
    remote_dir = tmp_path / "single-native" / f"AvaAgent-{agent}"

    def directory(_id: int, **_kwargs: Any) -> Path:
        return remote_dir

    monkeypatch.setattr(receiver, "agent_upload_dir", directory)
    with app.state.db.pool(min_size=1, max_size=1) as pool:

        def get(url: str, **_kwargs: Any):
            with pool.connection(timeout=0.5) as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
            result = client.get(url.removeprefix(receiver.gateway_api_base().rstrip("/")))
            return httpx.Response(
                result.status_code, content=result.content, request=httpx.Request("GET", url)
            )

        monkeypatch.setattr(receiver, "http_get", get)
        proof = receiver.receive(pool, request)
    assert (Path(proof.directory) / request.manifest.objects[0].name).read_bytes() == b"bytes"


def test_auth_revocation_and_missing_target_never_recover_by_bypassing_admission(
    uploaded_agent: tuple[TestClient, int],
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
):
    from base.config import settings

    client, agent = uploaded_agent
    response = post(client, agent)
    assert response.status_code == 202
    assert post(client, agent + 999).status_code == 404
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "new-test-secret")
    assert post(client, agent).status_code in (401, 403)
    assert client.get(response.json()["status_url"]).status_code in (401, 403)
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (1,)


@pytest.mark.asyncio
async def test_same_machine_other_home_requires_native_rpc_not_local_shortcut(
    uploaded_agent: tuple[TestClient, int],
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_authority: ConfigAuthority,
):
    client, agent = uploaded_agent
    unit = receiver.current_unit()
    target = unit.model_copy(update={"home": unit.home + "-second"})
    db_conn.execute("UPDATE machine_units SET url=NULL WHERE machine_name=%s", (unit.machine,))
    db_conn.execute(
        "INSERT INTO machine_units(machine_name,home,serve_agent_runner,url) "
        "VALUES (%s,%s,true,'http://runner:7001')",
        (unit.machine, target.home),
    )
    db_conn.commit()
    response = post(client, agent)
    assert response.status_code == 202
    request = request_for(app.state.db_pool, response.json()["batch_id"])
    assert request.source != request.target and request.target == target
    monkeypatch.setattr(receiver, "current_unit", lambda: target)
    monkeypatch.setattr(receiver, "MAX_AGENT_UPLOAD_BYTES", 5)
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def dispatch(
        _db: Database, machine: str, kind: str, payload: dict[str, Any], **kwargs: Any
    ):
        calls.append((machine, kind, kwargs))
        status, result = await asyncio.to_thread(
            dispatch_sync,
            kind,
            payload,
            image=app.state.process_image,
            pool=app.state.db_pool,
            db=app.state.db,
            authority=config_authority,
        )
        assert status == OpStatus.COMPLETED
        return result

    monkeypatch.setattr(worker, "dispatch_to_machine", dispatch)
    recovery = worker.UploadRecovery(app.state.db_pool, app.state.db, app.state.bus)
    await recovery.copy(request)
    assert len(calls) == 1 and calls[0][0] == unit.machine
    assert source.status(app.state.db_pool, agent, request.manifest.batch_id).state == "accepted"
    await recovery.close()


@pytest.mark.parametrize("ready", [False, True])
def test_legacy_remote_quota_counts_hidden_ready_and_receiving(
    uploaded_agent: tuple[TestClient, int],
    monkeypatch: pytest.MonkeyPatch,
    ready: bool,
    *,
    config_authority: ConfigAuthority,
):
    from base.agents.upload_delivery import storage
    from base.agents.upload_delivery.models import UploadQuotaExceededError
    from ops import uploads

    client, agent = uploaded_agent

    def paused(*_args: Any) -> None:
        raise OSError("paused")

    if not ready:
        monkeypatch.setattr(storage, "publish", paused)
    assert post(client, agent).status_code == (202 if ready else 500)
    monkeypatch.setattr(uploads, "MAX_AGENT_UPLOAD_BYTES", 5)

    def flat_get(*_args: Any, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200, content=b"x", request=httpx.Request("GET", "http://gateway.test")
        )

    monkeypatch.setattr(uploads, "http_get", flat_get)
    with pytest.raises(UploadQuotaExceededError):
        dispatch_sync(
            "upload_receive",
            {"agent_id": agent, "name": "flat.txt"},
            pool=app.state.db_pool,
            db=app.state.db,
            authority=config_authority,
            image=app.state.process_image,
        )
    assert not (source.agent_upload_dir(agent, create=False) / "flat.txt").exists()


def test_legacy_remote_overwrite_net_quota_and_network_without_db_borrow(
    uploaded_agent: tuple[TestClient, int],
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_authority: ConfigAuthority,
):
    from ops import uploads

    _, agent = uploaded_agent
    target = source.agent_upload_dir(agent) / "flat.txt"
    target.write_bytes(b"12345")
    monkeypatch.setattr(uploads, "MAX_AGENT_UPLOAD_BYTES", 5)
    with app.state.db.pool(min_size=1, max_size=1) as pool:
        pool.wait()

        def fetch(*_args: Any, **_kwargs: Any):
            with pool.connection() as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
            return httpx.Response(
                200, content=b"abcde", request=httpx.Request("GET", "http://gateway.test")
            )

        monkeypatch.setattr(uploads, "http_get", fetch)
        status, result = dispatch_sync(
            "upload_receive",
            {"agent_id": agent, "name": "flat.txt"},
            pool=pool,
            db=app.state.db,
            authority=config_authority,
            image=app.state.process_image,
        )
    assert status == OpStatus.COMPLETED and result == {"path": str(target)}
    assert target.read_bytes() == b"abcde"
