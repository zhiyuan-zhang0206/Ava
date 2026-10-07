"""Real native transactions, filesystem faults and guarded delivered-upload consumers."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.agents.messages.inbound_provenance import InboundProvenance
from base.agents.upload_delivery import source, storage
from base.agents.upload_delivery.models import (
    CopyProof,
    ReceiveRequest,
    UploadDeliveryConflictError,
    current_unit,
)
from base.config import settings
from gateway.app import app
from gateway.tests.test_notices_endpoint import _seed_agent
from gateway.upload_delivery.worker import UploadRecovery
from ops import upload_delivery as receiver

SECRET = "delivered-upload-test-secret"  # noqa: S105 -- isolated credential
HEADERS = {"Idempotency-Key": "intent", "Idempotency-Scope": "principal-v1"}


@pytest.fixture
def uploaded_agent(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, int]]:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", SECRET)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    # Deterministic fault tests call a real round explicitly; no background race.
    monkeypatch.setattr(UploadRecovery, "start", lambda _self, _group: None)
    unit = current_unit()
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
    with TestClient(
        app, headers={"Authorization": f"Bearer {SECRET}"}, raise_server_exceptions=False
    ) as client:
        yield client, agent


def post(
    client: TestClient,
    agent: int,
    *,
    key: str = "intent",
    data: bytes = b"bytes",
    name: str = "photo.png",
):
    return client.post(
        f"/api/keyed/v1/agents/{agent}/uploads",
        headers={**HEADERS, "Idempotency-Key": key},
        files=[("files", (name, data, "image/png"))],
    )


def request_for(pool, batch_id: str) -> ReceiveRequest:
    return next(item for item in source.due(pool) if item.manifest.batch_id == batch_id)


def proof_for(request: ReceiveRequest) -> CopyProof:
    path = source.agent_upload_dir(request.manifest.agent_id, create=False).resolve()
    return CopyProof(
        version=1,
        target=request.target,
        manifest_hash=request.manifest.fingerprint(),
        directory=str(path / ".delivered-v1" / request.manifest.batch_id),
    )


def test_lost_response_replay_retains_original_notification_after_deletion(uploaded_agent, db_conn):
    client, agent = uploaded_agent
    response = post(client, agent)
    assert response.status_code == 202, response.text
    batch_id = response.json()["batch_id"]
    request = request_for(app.state.db_pool, batch_id)
    proof = proof_for(request)
    iid = source.complete(app.state.db_pool, request, proof)
    assert source.complete(app.state.db_pool, request, proof) == iid
    db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (iid,))
    db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent,))
    db_conn.execute("DELETE FROM agent_lifecycle_intervals WHERE agent_id=%s", (agent,))
    db_conn.execute("DELETE FROM agents WHERE id=%s", (agent,))
    db_conn.commit()
    assert source.complete(app.state.db_pool, request, proof) == iid
    assert post(client, agent).json() == response.json()
    assert client.get(response.json()["status_url"]).json()["inbound_id"] == iid
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)
    obj = f"{response.json()['status_url']}/objects/0"
    assert client.get(obj).content == b"bytes"


def test_changed_manifest_and_invalid_guard_have_no_effect(uploaded_agent, db_conn):
    client, agent = uploaded_agent
    assert post(client, agent).status_code == 202
    assert post(client, agent, data=b"changed").status_code == 409
    assert post(client, agent, key="invalid", name="bad.unsafe%url").status_code == 422
    for headers in (
        {},
        {"Idempotency-Key": "x"},
        {**HEADERS, "Idempotency-Key": ""},
        {**HEADERS, "Idempotency-Scope": "unknown"},
    ):
        assert (
            client.post(
                f"/api/keyed/v1/agents/{agent}/uploads",
                headers=headers,
                files=[("files", ("f", b"x"))],
            ).status_code
            == 422
        )
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)


def test_source_fsync_loss_and_concurrent_recovery_use_one_identity(
    uploaded_agent, db_conn, monkeypatch
):
    client, agent = uploaded_agent
    original = storage.publish

    def fail_after(*args):
        original(*args)
        raise OSError("fault after source fsync")

    monkeypatch.setattr(storage, "publish", fail_after)
    assert post(client, agent).status_code == 500
    assert db_conn.execute("SELECT state FROM upload_delivery_batches").fetchone() == ("receiving",)
    monkeypatch.setattr(storage, "publish", original)
    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(
            executor.map(
                lambda _: source.accept(
                    app.state.db_pool,
                    "direct",
                    agent,
                    ["f.png"],
                    [("f.png", b"native", "image/png")],
                    InboundProvenance("cluster_bearer", "http"),
                ),
                range(2),
            )
        )
    assert values[0] == values[1]
    assert post(client, agent).status_code == 202
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (2,)


def test_placement_or_bad_proof_rejected_before_inbound(uploaded_agent, db_conn):
    client, agent = uploaded_agent
    first = post(client, agent).json()
    request = request_for(app.state.db_pool, first["batch_id"])
    proof = proof_for(request)
    with pytest.raises(UploadDeliveryConflictError):
        source.complete(
            app.state.db_pool, request, proof.model_copy(update={"manifest_hash": "0" * 64})
        )
    db_conn.execute("UPDATE agents_meta SET machine='moved' WHERE id=%s", (agent,))
    db_conn.commit()
    with pytest.raises(UploadDeliveryConflictError):
        source.complete(app.state.db_pool, request, proof)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)


def test_receiver_same_physical_root_different_home_counts_once_and_verifies_ready(
    uploaded_agent, db_conn, monkeypatch
):
    client, agent = uploaded_agent
    first = post(client, agent).json()
    request = request_for(app.state.db_pool, first["batch_id"])
    unit = request.target.model_copy(update={"home": request.target.home + "-other"})
    request = request.model_copy(update={"target": unit})
    monkeypatch.setattr(receiver, "current_unit", lambda: unit)
    monkeypatch.setattr(receiver, "MAX_AGENT_UPLOAD_BYTES", 5)
    proof = receiver.receive(app.state.db_pool, request)
    assert proof.target == unit
    assert receiver.receive(app.state.db_pool, request) == proof
    path = Path(proof.directory) / request.manifest.objects[0].name
    path.write_bytes(b"wrong")
    with pytest.raises(UploadDeliveryConflictError):
        receiver.receive(app.state.db_pool, request)
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_copies").fetchone() == (1,)


def test_receiver_wrong_actual_unit_and_legacy_writer_cannot_overwrite_nested_final(
    uploaded_agent, monkeypatch
):
    client, agent = uploaded_agent
    first = post(client, agent).json()
    request = request_for(app.state.db_pool, first["batch_id"])
    monkeypatch.setattr(
        receiver, "current_unit", lambda: request.target.model_copy(update={"home": "/wrong"})
    )
    with pytest.raises(UploadDeliveryConflictError):
        receiver.receive(app.state.db_pool, request)
    from base.agents.uploads import sanitize_upload_name

    path = Path(proof_for(request).directory) / request.manifest.objects[0].name
    flat = path.parent.parent.parent / sanitize_upload_name(
        str(path.relative_to(path.parent.parent.parent))
    )
    flat.write_bytes(b"old disconnected writer")
    assert path.read_bytes() == b"bytes"


def test_legacy_flat_admission_counts_hidden_files_and_receiving_reservation(
    uploaded_agent, monkeypatch
):
    client, agent = uploaded_agent
    assert post(client, agent).status_code == 202
    from gateway.routers import uploads

    monkeypatch.setattr(uploads, "MAX_AGENT_UPLOAD_BYTES", 5)
    response = client.post(
        f"/api/agents/{agent}/uploads?deliver=false", files=[("files", ("flat", b"x"))]
    )
    assert response.status_code == 413


def test_name_byte_budget_includes_atomic_primitive_temporary_name(uploaded_agent, db_conn):
    client, agent = uploaded_agent
    rejected = post(client, agent, name="x." + "a" * 184)
    assert rejected.status_code == 422
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (0,)
    accepted = post(client, agent, name="x." + "a" * 182)
    assert accepted.status_code == 202, accepted.text
    assert len(accepted.json()["files"][0]["name"].encode()) == 217
    assert client.get(accepted.json()["status_url"] + "/objects/0").content == b"bytes"


@pytest.mark.parametrize("changed", ["source", "target", "manifest"])
def test_completed_receipt_rejects_different_immutable_intent(uploaded_agent, changed):
    client, agent = uploaded_agent
    batch = post(client, agent).json()["batch_id"]
    request = request_for(app.state.db_pool, batch)
    iid = source.complete(app.state.db_pool, request, proof_for(request))
    raw = request.model_dump()
    if changed == "manifest":
        raw["manifest"]["objects"][0]["filename"] = "different-display.png"
    else:
        raw[changed]["home"] += "-different"
    changed_request = ReceiveRequest.model_validate(raw)
    with pytest.raises(UploadDeliveryConflictError, match="stored intent"):
        source.complete(app.state.db_pool, changed_request, proof_for(changed_request))
    assert source.complete(app.state.db_pool, request, proof_for(request)) == iid


def test_manifest_serving_preserves_display_filename_and_safe_renderable_headers(uploaded_agent):
    from urllib.parse import quote

    client, agent = uploaded_agent
    name = "original display café.html"
    response = client.post(
        f"/api/keyed/v1/agents/{agent}/uploads",
        headers=HEADERS,
        files=[("files", (name, b"<script>bad()</script>", "text/html"))],
    )
    assert response.status_code == 202, response.text
    assert response.json()["files"][0]["filename"] == name
    served = client.get(response.json()["status_url"] + "/objects/0")
    assert served.status_code == 200
    assert served.headers["content-type"] == "text/html; charset=utf-8"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["content-disposition"] == "attachment; filename*=utf-8''" + quote(name)
    assert served.content == b"<script>bad()</script>"
