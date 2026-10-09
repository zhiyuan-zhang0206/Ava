"""Real-Postgres silent upload receipts and immutable URL compatibility."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx2 as httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from base.agents.upload_delivery.paths import image_mime_for, parse_upload_url
from gateway.app import app
from gateway.routers.upload import batches as upload_batches
from gateway.routers.upload.batches import UploadItem


def _post(
    client: TestClient, agent: int, key: str, *, body: bytes = b"image", name: str = "photo.png"
) -> httpx.Response:
    return client.post(
        f"/api/agents/{agent}/uploads?deliver=false",
        headers={"Idempotency-Key": key},
        files=[("files", (name, body, "image/png"))],
    )


@pytest.fixture
def uploaded_agent(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, int]]:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with TestClient(app) as client:
        agent = client.post("/api/agents", json={}).json()["id"]
        yield client, agent


def test_lost_response_replays_receipt_and_preserves_old_image(
    uploaded_agent: tuple[TestClient, int], db_conn: psycopg.Connection
) -> None:
    client, agent = uploaded_agent
    first = _post(client, agent, "first")
    assert first.status_code == 200
    assert _post(client, agent, "first").json() == first.json()
    second = _post(client, agent, "second", body=b"other")
    file = first.json()["files"][0]
    assert second.json()["files"][0]["url"] != file["url"]
    assert file["filename"] == "photo.png"
    assert client.get(file["url"]).content == b"image"
    parsed = parse_upload_url(file["url"])
    assert parsed is not None and parsed[0] == agent
    assert image_mime_for(parsed[1]) == "image/png"
    row = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone()
    assert row is not None and row[0] == 0

    reserved = Path(file["path"]).name
    legacy = client.post(
        f"/api/agents/{agent}/uploads?deliver=false",
        files=[("files", (reserved, b"replace", "image/png"))],
    )
    assert legacy.status_code == 409
    assert client.get(file["url"]).content == b"image"


@pytest.mark.parametrize("change", ["bytes", "name", "type", "deliver"])
def test_manifest_conflict_before_file_effect(
    uploaded_agent: tuple[TestClient, int], change: str
) -> None:
    client, agent = uploaded_agent
    first = _post(client, agent, "fixed")
    assert first.status_code == 200
    response = client.post(
        f"/api/agents/{agent}/uploads?deliver={'true' if change == 'deliver' else 'false'}",
        headers={"Idempotency-Key": "fixed"},
        files=[
            (
                "files",
                (
                    "other.png" if change == "name" else "photo.png",
                    b"different" if change == "bytes" else b"image",
                    "image/jpeg" if change == "type" else "image/png",
                ),
            )
        ],
    )
    assert response.status_code == 409
    directory = Path(first.json()["files"][0]["path"]).parent
    assert len([p for p in directory.iterdir() if p.is_file()]) == 1


def test_unimplemented_delivery_and_invalid_suffix_do_not_claim(
    uploaded_agent: tuple[TestClient, int], db_conn: psycopg.Connection
) -> None:
    client, agent = uploaded_agent
    unsupported = client.post(
        f"/api/agents/{agent}/uploads",
        headers={"Idempotency-Key": "delivery"},
        files=[("files", ("a.png", b"image", "image/png"))],
    )
    assert unsupported.status_code == 422
    assert _post(client, agent, "suffix", name="a." + "z" * 129).status_code == 422
    row = db_conn.execute("SELECT count(*) FROM agent_upload_batches").fetchone()
    assert row is not None and row[0] == 0


def test_long_display_name_keeps_bounded_object_name(
    uploaded_agent: tuple[TestClient, int],
) -> None:
    client, agent = uploaded_agent
    name = "x" * 300 + ".png"
    response = _post(client, agent, "long", name=name)
    assert response.status_code == 200
    file = response.json()["files"][0]
    assert file["filename"] == name
    assert len(Path(file["path"]).name.encode()) < 255
    assert client.get(file["url"]).content == b"image"


def test_ready_receipt_survives_deleted_target(
    uploaded_agent: tuple[TestClient, int],
    db_conn: psycopg.Connection,
) -> None:
    client, _ = uploaded_agent
    row = db_conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent = row[0]
    db_conn.commit()
    accepted = _post(client, agent, "historical")
    assert accepted.status_code == 200
    db_conn.execute("DELETE FROM agents WHERE id = %s", (agent,))
    db_conn.commit()
    assert _post(client, agent, "historical").json() == accepted.json()
    assert _post(client, agent, "fresh").status_code == 404
    assert client.get(accepted.json()["files"][0]["url"]).status_code == 404


def test_immutable_url_passes_native_image_message_validation(
    uploaded_agent: tuple[TestClient, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.lm import factory

    client, agent = uploaded_agent

    def supports_vision(model: str) -> bool:
        return True

    monkeypatch.setattr(factory, "model_supports_vision", supports_vision)
    url = _post(client, agent, "native").json()["files"][0]["url"]
    response = client.post(
        f"/api/agents/{agent}/messages",
        json={
            "content": [{"type": "image_url", "image_url": {"url": url}}],
            "source": "user",
        },
    )
    assert response.status_code == 201, response.text


def test_concurrent_same_key_one_reservation(
    uploaded_agent: tuple[TestClient, int], db_conn: psycopg.Connection
) -> None:
    client, agent = uploaded_agent
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_post, client, agent, "shared") for _ in range(4)]
        results = [future.result() for future in futures]
    assert all(response.status_code == 200 for response in results)
    assert all(response.json() == results[0].json() for response in results)
    row = db_conn.execute("SELECT count(*) FROM agent_upload_batches").fetchone()
    assert row is not None and row[0] == 1


def test_receiving_reservation_survives_partial_publish_and_recovery(
    uploaded_agent: tuple[TestClient, int],
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, agent = uploaded_agent
    original = upload_batches.publish_files

    def fail_after_publish(
        directory: Path, manifest: list[dict[str, Any]], batch: list[UploadItem]
    ) -> None:
        original(directory, manifest, batch)
        raise OSError("lost DB transaction after durable publication")

    monkeypatch.setattr(upload_batches, "publish_files", fail_after_publish)
    with pytest.raises(OSError, match="lost DB"):
        _post(client, agent, "recover")
    row = db_conn.execute("SELECT manifest, receipt FROM agent_upload_batches").fetchone()
    assert row is not None and row[1] is None
    assert _post(client, agent, "recover", body=b"changed").status_code == 409
    monkeypatch.setattr(upload_batches, "publish_files", original)
    recovered = _post(client, agent, "recover")
    assert recovered.status_code == 200
    assert Path(recovered.json()["files"][0]["path"]).name == row[0][0]["stored_name"]
    row = db_conn.execute("SELECT receipt IS NOT NULL FROM agent_upload_batches").fetchone()
    assert row is not None and row[0]


def test_shared_quota_counts_receiving_once(
    uploaded_agent: tuple[TestClient, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent = uploaded_agent
    from gateway.routers.upload import router

    monkeypatch.setattr(router, "MAX_AGENT_UPLOAD_BYTES", 5)
    original = upload_batches.publish_files

    def interrupted(
        directory: Path, manifest: list[dict[str, Any]], batch: list[UploadItem]
    ) -> None:
        original(directory, manifest, batch)
        raise OSError("interrupted")

    monkeypatch.setattr(upload_batches, "publish_files", interrupted)
    with pytest.raises(OSError):
        _post(client, agent, "quota")
    other = client.post(
        f"/api/agents/{agent}/uploads?deliver=false",
        files=[("files", ("ordinary.txt", b"x", "text/plain"))],
    )
    assert other.status_code == 413
    monkeypatch.setattr(upload_batches, "publish_files", original)
    assert _post(client, agent, "quota").status_code == 200


def test_duplicate_display_names_have_distinct_objects_and_order_is_identity(
    uploaded_agent: tuple[TestClient, int],
) -> None:
    client, agent = uploaded_agent
    files = [("files", ("same.png", body, "image/png")) for body in (b"a", b"b")]
    response = client.post(
        f"/api/agents/{agent}/uploads?deliver=false",
        headers={"Idempotency-Key": "ordered"},
        files=files,
    )
    assert response.status_code == 200
    urls = [file["url"] for file in response.json()["files"]]
    assert len(set(urls)) == 2
    assert [client.get(url).content for url in urls] == [b"a", b"b"]
    assert (
        client.post(
            f"/api/agents/{agent}/uploads?deliver=false",
            headers={"Idempotency-Key": "ordered"},
            files=list(reversed(files)),
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"Idempotency-Key": ""},
        {"Idempotency-Key": "x" * 129},
        {"Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "scoped", "Idempotency-Scope": "unsupported"},
    ],
)
def test_invalid_identity_before_claim(
    uploaded_agent: tuple[TestClient, int],
    db_conn: psycopg.Connection,
    headers: dict[str, str],
) -> None:
    client, agent = uploaded_agent
    response = client.post(
        f"/api/agents/{agent}/uploads?deliver=false",
        headers=headers,
        files=[("files", ("a.png", b"a", "image/png"))],
    )
    assert response.status_code == 400
    row = db_conn.execute("SELECT count(*) FROM agent_upload_batches").fetchone()
    assert row is not None and row[0] == 0
