from pathlib import Path

import psycopg
import pytest

from base.api_contracts.idempotency import keyed_headers
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.daemon.schedules.completion_notices import (
    current_default_completion_notice_policy,
    policy_for_agent,
)
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import AddModels
from tests.fixtures.gateway_config import gateway_test_client

UPLOAD_HEADERS = {"Idempotency-Key": "silent-upload", "Idempotency-Scope": "principal-v1"}


def _seed_agent(
    db_conn: psycopg.Connection, status: str = "idling", *, local_machine: MachineIdentity
) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, machine) VALUES (%s, 'test', %s, %s)",
            (new_id, status, local_machine.name()),
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


def test_post_message_inserts_chat_pending(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": "hi agent", "source": "user"}
        )
    assert resp.status_code == 201
    assert "status" in resp.json()
    assert _pending_rows(db_conn, tid) == [("chat", "pending", "hi agent")]


def test_hourly_completion_policy_buffers_every_notice(
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
            ('{"completion_notice_policy": "hourly"}', tid),
        )
    db_conn.commit()
    policy = policy_for_agent(
        db_conn, tid, current_default_completion_notice_policy(authority=config_authority)
    )
    with gateway_test_client(machine=machine_identity) as client:
        first = client.post(
            f"/api/agents/{tid}/messages",
            json={
                "content": "Background command 'build' finished. Full output at log.",
                "source": "shell:55",
                "completion_notice": True,
            },
        )
        second = client.post(
            f"/api/agents/{tid}/messages",
            json={
                "content": "Background command 'test' finished. Full output at log.",
                "source": "shell:56",
                "completion_notice": True,
            },
        )
    assert policy == "hourly"
    assert first.status_code == 201 and first.json()["inbound_id"] is None
    assert second.status_code == 201 and second.json()["inbound_id"] is None
    assert len(_pending_rows(db_conn, tid)) == 0
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM completion_notice_events WHERE agent_id = %s",
            (tid,),
        )
        assert cur.fetchone() == (2,)


def test_default_completion_policy_preserves_every_completion(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with gateway_test_client(machine=machine_identity) as client:
        response = client.post(
            f"/api/agents/{tid}/messages",
            json={
                "content": "Background command 'default' finished. Full output at log.",
                "source": "shell:59",
                "completion_notice": True,
            },
        )
    assert response.status_code == 201 and response.json()["inbound_id"] is not None
    assert len(_pending_rows(db_conn, tid)) == 1


def test_post_message_empty_content_422(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(f"/api/agents/{tid}/messages", json={"content": "   ", "source": "user"})
    assert resp.status_code == 422
    assert _pending_rows(db_conn, tid) == []


def test_post_message_large_content_is_accepted(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    content = "a" * 100_000
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": content, "source": "user"}
        )
    assert resp.status_code == 201
    assert _pending_rows(db_conn, tid) == [("chat", "pending", content)]


def test_post_message_content_at_one_mib_is_accepted(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    content = "a" * 1_048_576
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": content, "source": "user"}
        )
    assert resp.status_code == 201
    assert _pending_rows(db_conn, tid) == [("chat", "pending", content)]


def test_post_message_content_over_one_mib_is_413(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    content = "é" * 524_289
    assert len(content.encode("utf-8")) > 1_048_576
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": content, "source": "user"}
        )
    assert resp.status_code == 413
    assert "1 MiB" in resp.json()["detail"]
    assert "file path" in resp.json()["detail"]
    assert _pending_rows(db_conn, tid) == []


def test_post_message_block_list_over_one_mib_is_413(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    blocks = [{"type": "text", "text": "a" * 1_100_000}]
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages", json={"content": blocks, "source": "user"}
        )
    assert resp.status_code == 413
    assert "1 MiB" in resp.json()["detail"]
    assert _pending_rows(db_conn, tid) == []


def test_reconcile_rejects_over_limit_after_timeout(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    content = "é" * 524_289
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages/reconcile",
            json={"content": content, "source": "user"},
            headers=keyed_headers("k1"),
        )
    assert resp.status_code == 413
    assert _pending_rows(db_conn, tid) == []


def test_post_message_404_when_agent_missing(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        resp = client.post("/api/agents/9999/messages", json={"content": "x", "source": "user"})
    assert resp.status_code == 404
    assert _pending_rows(db_conn, 9999) == []


def test_post_message_terminated_agent_allows_insert(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    tid = _seed_agent(db_conn, status="terminated", local_machine=machine_identity)
    db_conn.execute(
        "UPDATE agents_meta SET runtime_kind = 'hosted', runtime_generation = gen_random_uuid(), "
        "runtime_owner = gen_random_uuid() WHERE id = %s",
        (tid,),
    )
    db_conn.commit()
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(f"/api/agents/{tid}/messages", json={"content": "x", "source": "user"})
    assert resp.status_code == 201
    assert "status" in resp.json()
    rows = _pending_rows(db_conn, tid)
    assert rows[0] == ("chat", "pending", "x")
    assert rows[1] == ("resurrect", "pending", "")


def test_post_message_command_chain_is_one_inbound(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages",
            json={"content": "/recap the week /compact", "source": "user"},
        )
    assert resp.status_code == 201
    assert _pending_rows(db_conn, tid) == [("chat", "pending", "/recap the week /compact")]


def test_post_message_does_not_inspect_the_command_chain(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(
            f"/api/agents/{tid}/messages",
            json={"content": "/compact /recap the week", "source": "user"},
        )
    assert resp.status_code == 201
    assert _pending_rows(db_conn, tid) == [("chat", "pending", "/compact /recap the week")]


def test_post_message_invalid_source_422(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    tid = _seed_agent(db_conn, local_machine=machine_identity)
    with gateway_test_client(machine=machine_identity) as client:
        resp = client.post(f"/api/agents/{tid}/messages", json={"content": "x", "source": "ui:web"})
    assert resp.status_code == 422
    assert "Unrecognized inbound source" in resp.text
    assert _pending_rows(db_conn, tid) == []


def _seed_vision_agent(
    db_conn: psycopg.Connection, model: str = "claude-opus-4-8", *, local_machine: MachineIdentity
) -> int:
    tid = _seed_agent(db_conn, local_machine=local_machine)
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
    def test_image_to_non_vision_model_422(
        self, db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
    ) -> None:
        tid = _seed_agent(db_conn, local_machine=machine_identity)
        with gateway_test_client(machine=machine_identity) as client:
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
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        from pathlib import Path

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        tid = _seed_vision_agent(db_conn, local_machine=machine_identity)
        with gateway_test_client(machine=machine_identity) as client:
            up = client.post(
                f"/api/agents/{tid}/uploads",
                headers=UPLOAD_HEADERS,
                files=[("files", ("shot.png", b"\x89PNG\r\n\x1a\npix", "image/png"))],
            )
            assert up.status_code == 200
            url = up.json()["files"][0]["url"]
            assert url.startswith(f"/api/agents/{tid}/uploads/ava-upload-")
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

    def test_image_to_withdrawn_vision_model_resolves_text_only_422(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        add_models: AddModels,
        model_catalog: ModelCatalog,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        from dataclasses import replace
        from pathlib import Path

        from base.packages.plugin_providers import build_model_catalog

        model = "deepseek-vision-fixture"
        base = build_model_catalog().models["deepseek-flash"]
        catalog = add_models(
            model_catalog,
            {
                model: replace(
                    base,
                    spawnable=False,
                    unavailable_fallback="deepseek-flash",
                    media_types=frozenset({"image"}),
                )
            },
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        tid = _seed_vision_agent(db_conn, model=model, local_machine=machine_identity)
        with gateway_test_client(machine=machine_identity) as client:
            client.app.state.catalog = catalog
            up = client.post(
                f"/api/agents/{tid}/uploads",
                headers=UPLOAD_HEADERS,
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
        assert resp.status_code == 422
        assert "cannot see images" in resp.text
        assert _pending_rows(db_conn, tid) == []

    def test_image_ref_wrong_agent_422(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        from pathlib import Path

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        tid = _seed_vision_agent(db_conn, local_machine=machine_identity)
        with gateway_test_client(machine=machine_identity) as client:
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
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        from pathlib import Path

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        tid = _seed_vision_agent(db_conn, local_machine=machine_identity)
        with gateway_test_client(machine=machine_identity) as client:
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

    def test_plain_string_still_works(
        self, db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
    ) -> None:
        tid = _seed_agent(db_conn, local_machine=machine_identity)
        with gateway_test_client(machine=machine_identity) as client:
            resp = client.post(
                f"/api/agents/{tid}/messages", json={"content": "hi", "source": "user"}
            )
        assert resp.status_code == 201
        content, payload = _payload_row(db_conn, tid)
        assert content == "hi"
        assert payload is None
