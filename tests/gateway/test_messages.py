"""POST /api/agents/{id}/messages integration tests.

Uses FastAPI TestClient with real app, real ava_test DB. POST writes kind='chat'
pending (kernel picks it up for the agent).
"""

from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared.machine import machine_name


def _seed_agent(db_conn: psycopg.Connection, status: str = "idling") -> int:
    """Insert agents + agents_meta rows directly in DB without triggering a session launch.
    Writes the local machine name, close to the real spawn path (auto-resurrect uses
    machine to decide local execution vs forward; default 'unknown' would be treated
    as an unreachable remote home machine)."""
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, machine) VALUES (%s, 'test', %s, %s)",
            (new_id, status, machine_name()),
        )
    db_conn.commit()
    return new_id


def _pending_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, status, content FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def test_post_message_inserts_chat_pending(db_conn: psycopg.Connection) -> None:
    tid = _seed_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": "hi agent", "source": "user"}
        )
    assert resp.status_code == 201
    assert "status" in resp.json()  # AgentMessageEnqueued returns agent status
    assert _pending_rows(db_conn, tid) == [("chat", "pending", "hi agent")]


def test_post_message_empty_content_422(db_conn: psycopg.Connection) -> None:
    """Blank / oversized rejected by schema layer (pydantic 422), not stuffed into pending."""
    tid = _seed_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{tid}/messages", json={"content": "   ", "source": "user"})
    assert resp.status_code == 422
    assert _pending_rows(db_conn, tid) == []


def test_post_message_large_content_is_accepted(db_conn: psycopg.Connection) -> None:
    """A handoff-sized message remains a normal durable chat inbound."""
    tid = _seed_agent(db_conn)
    content = "a" * 100_000
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": content, "source": "user"}
        )
    assert resp.status_code == 201
    assert _pending_rows(db_conn, tid) == [("chat", "pending", content)]


def test_post_message_content_at_one_mib_is_accepted(db_conn: psycopg.Connection) -> None:
    """The byte cap is inclusive: exactly 1 MiB remains deliverable."""
    tid = _seed_agent(db_conn)
    content = "a" * 1_048_576
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": content, "source": "user"}
        )
    assert resp.status_code == 201
    assert _pending_rows(db_conn, tid) == [("chat", "pending", content)]


def test_post_message_content_over_one_mib_is_413(db_conn: psycopg.Connection) -> None:
    """The limit counts UTF-8 bytes and tells callers how to deliver large content."""
    tid = _seed_agent(db_conn)
    content = "é" * 524_289
    assert len(content.encode("utf-8")) > 1_048_576
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": content, "source": "user"}
        )
    assert resp.status_code == 413
    assert "1 MiB" in resp.json()["detail"]
    assert "file path" in resp.json()["detail"]
    assert _pending_rows(db_conn, tid) == []


def test_post_message_block_list_over_one_mib_is_413(db_conn: psycopg.Connection) -> None:
    """The limit also counts serialized content-block lists, not only strings."""
    tid = _seed_agent(db_conn)
    blocks = [{"type": "text", "text": "a" * 1_100_000}]
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": blocks, "source": "user"}
        )
    assert resp.status_code == 413
    assert "1 MiB" in resp.json()["detail"]
    assert _pending_rows(db_conn, tid) == []


def test_reconcile_rejects_over_limit_after_timeout(db_conn: psycopg.Connection) -> None:
    """The idempotency reconcile route carries the same byte gate as the POST."""
    tid = _seed_agent(db_conn)
    content = "é" * 524_289
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages/reconcile",
            json={"content": content, "source": "user"},
            headers={"Idempotency-Key": "k1"},
        )
    assert resp.status_code == 413
    assert _pending_rows(db_conn, tid) == []


def test_post_message_404_when_agent_missing(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        resp = client.post("/api/agents/9999/messages", json={"content": "x", "source": "user"})
    assert resp.status_code == 404
    assert _pending_rows(db_conn, 9999) == []


def test_post_message_terminated_agent_allows_insert(db_conn: psycopg.Connection) -> None:
    """Terminated agent receives message: SDK path does not block — auto-resurrect
    triggers automatically, delivering chat inbound + resurrect lifecycle inbound.
    Test env has no session backend, so resurrection fails (status reverts to terminated),
    but both inbounds are already in the queue."""
    tid = _seed_agent(db_conn, status="terminated")
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{tid}/messages", json={"content": "x", "source": "user"})
    assert resp.status_code == 201  # SDK path does not block terminated
    assert "status" in resp.json()
    rows = _pending_rows(db_conn, tid)
    # Both the chat inbound and the auto-resurrect lifecycle inbound are queued
    assert rows[0] == ("chat", "pending", "x")
    assert rows[1] == ("resurrect", "pending", "")


def test_post_message_command_chain_is_one_inbound(db_conn: psycopg.Connection) -> None:
    """Several commands in one send stay one inbound, stored verbatim — the
    expansion happens in the agent's claim node, not here, so the agent reads
    the chain as a single composite instruction."""
    tid = _seed_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages",
            json={"content": "/recap /plan the migration", "source": "user"},
        )
    assert resp.status_code == 201
    assert _pending_rows(db_conn, tid) == [("chat", "pending", "/recap /plan the migration")]


def test_post_message_does_not_inspect_the_command_chain(db_conn: psycopg.Connection) -> None:
    """The endpoint stores command text without reading it. Even `/compact`
    chained with another command — the combination most likely to read oddly —
    is queued verbatim: the gateway has no opinion about which prompts belong
    together, and adding one would put prompt semantics in the transport."""
    tid = _seed_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages",
            json={"content": "/compact /recap the week", "source": "user"},
        )
    assert resp.status_code == 201
    assert _pending_rows(db_conn, tid) == [("chat", "pending", "/compact /recap the week")]


def test_post_message_invalid_source_422(db_conn: psycopg.Connection) -> None:
    """Source not in envelope whitelist (system / agent:N / user / watcher:N)
    must be rejected at HTTP layer with 422; never written to inbound_messages.
    Prevents a buggy client from writing an invalid source into DB, causing the
    agent claim node to hit ValueError and kill the process. The old `ui:web`
    channel prefix has been removed and is the canonical example of this rejected
    source (see envelope.validate_source)."""
    tid = _seed_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{tid}/messages", json={"content": "x", "source": "ui:web"})
    assert resp.status_code == 422
    assert "Unrecognized inbound source" in resp.text
    assert _pending_rows(db_conn, tid) == []


def _seed_vision_agent(db_conn: psycopg.Connection, model: str = "claude-opus-4-8") -> int:
    """Seed an agent whose per-agent overlay pins a vision-capable model, so the
    capability gate lets image messages through."""
    tid = _seed_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
            (f'{{"llm_model": "{model}"}}', tid),
        )
    db_conn.commit()
    return tid


def _payload_row(conn: psycopg.Connection, agent_id: int) -> tuple[str, dict | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content, payload FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0], row[1]


class TestMultimodalMessage:
    def test_image_to_non_vision_model_422(self, db_conn: psycopg.Connection) -> None:
        """An explicitly text-only model gates image input before it is queued."""
        tid = _seed_vision_agent(db_conn, model="deepseek-v4-pro")
        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{tid}/messages",
                json={
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"/api/agents/{tid}/uploads/a.png"},
                        },
                    ],
                    "source": "user",
                },
            )
        assert resp.status_code == 422
        assert "cannot see images" in resp.text
        assert _pending_rows(db_conn, tid) == []

    def test_image_to_vision_model_stores_blocks(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A vision agent: upload silently (deliver=false), then a multimodal
        message stores the text part in content + the blocks in payload."""
        from pathlib import Path

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        tid = _seed_vision_agent(db_conn)
        with TestClient(app) as client:
            up = client.post(
                f"/api/agents/{tid}/uploads?deliver=false",
                files=[("files", ("shot.png", b"\x89PNG\r\n\x1a\npix", "image/png"))],
            )
            assert up.status_code == 200
            url = up.json()["files"][0]["url"]
            assert url == f"/api/agents/{tid}/uploads/shot.png"
            # deliver=false must not have queued any inbound.
            assert _pending_rows(db_conn, tid) == []

            resp = client.post(
                f"/api/agents/{tid}/messages",
                json={
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                    "source": "user",
                },
            )
        assert resp.status_code == 201
        content, payload = _payload_row(db_conn, tid)
        assert content == "describe this"
        assert payload is not None
        blocks = payload["content_blocks"]
        assert blocks[0] == {"type": "text", "text": "describe this"}
        assert blocks[1]["type"] == "image_url"
        assert blocks[1]["image_url"]["url"] == url

    def test_image_to_deepseek_vision_model_stores_blocks(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default vision model accepts image input without an overlay."""
        from pathlib import Path

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        tid = _seed_agent(db_conn)
        with TestClient(app) as client:
            up = client.post(
                f"/api/agents/{tid}/uploads?deliver=false",
                files=[("files", ("shot.png", b"\x89PNG\r\n\x1a\npix", "image/png"))],
            )
            assert up.status_code == 200
            url = up.json()["files"][0]["url"]

            resp = client.post(
                f"/api/agents/{tid}/messages",
                json={
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                    "source": "user",
                },
            )
        assert resp.status_code == 201
        content, payload = _payload_row(db_conn, tid)
        assert content == "describe this"
        assert payload is not None
        blocks = payload["content_blocks"]
        assert blocks[0] == {"type": "text", "text": "describe this"}
        assert blocks[1]["type"] == "image_url"
        assert blocks[1]["image_url"]["url"] == url

    def test_image_ref_wrong_agent_422(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An image_url pointing at another agent's upload is rejected."""
        from pathlib import Path

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        tid = _seed_vision_agent(db_conn)
        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{tid}/messages",
                json={
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "/api/agents/999/uploads/x.png"},
                        },
                    ],
                    "source": "user",
                },
            )
        assert resp.status_code == 422
        assert "different agent" in resp.text
        assert _pending_rows(db_conn, tid) == []

    def test_image_ref_missing_file_404(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A well-formed ref to a file that was never uploaded → 404."""
        from pathlib import Path

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        tid = _seed_vision_agent(db_conn)
        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{tid}/messages",
                json={
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"/api/agents/{tid}/uploads/nope.png"},
                        },
                    ],
                    "source": "user",
                },
            )
        assert resp.status_code == 404
        assert _pending_rows(db_conn, tid) == []

    def test_plain_string_still_works(self, db_conn: psycopg.Connection) -> None:
        """The string content path is unchanged (no payload written)."""
        tid = _seed_agent(db_conn)
        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{tid}/messages", json={"content": "hi", "source": "user"}
            )
        assert resp.status_code == 201
        content, payload = _payload_row(db_conn, tid)
        assert content == "hi"
        assert payload is None
