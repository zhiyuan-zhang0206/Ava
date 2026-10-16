from collections.abc import Callable, Iterator

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.packages.tests.lm.providers.provider_plugin_support import (
    provider_plugin as provider_plugin,
)
from base.paths import plugins_dir
from tests.fixtures.gateway_config import GatewayTestClient, gateway_test_client, spawn_via_http


@pytest.fixture
def failed_provider_agent(
    provider_plugin: Callable[..., None], *, machine_identity: MachineIdentity
) -> Iterator[tuple[GatewayTestClient, int]]:
    with gateway_test_client(machine=machine_identity) as client:
        born = spawn_via_http(client, {"prompt": "keep this agent", "prompt_source": "user"})
        assert born.status_code == 201, born.text
        agent_id = born.json()["id"]
    provider_plugin(dir_name="z_broken_provider")
    (plugins_dir() / "z_broken_provider" / "provider.py").write_text(
        'raise RuntimeError("provider private diagnostic")\n'
    )
    with gateway_test_client(machine=machine_identity) as client:
        yield client, agent_id


def test_unavailable_model_cannot_change_defaults_or_spawn(
    failed_provider_agent: tuple[GatewayTestClient, int], db_conn: psycopg.Connection
) -> None:
    client, _agent_id = failed_provider_agent
    original_default = db_conn.execute("SELECT * FROM cluster_defaults").fetchall()
    change_default = client.put("/api/config/default-model", json={"model": "deepseek-flash"})
    assert change_default.status_code == 503
    assert db_conn.execute("SELECT * FROM cluster_defaults").fetchall() == original_default
    before = db_conn.execute("SELECT count(*) FROM agents").fetchone()
    spawn = spawn_via_http(client, {"prompt": "blocked", "prompt_source": "user"})
    assert spawn.status_code == 503, spawn.text
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == before


def test_text_delivery_and_history_survive_provider_failure(
    failed_provider_agent: tuple[GatewayTestClient, int],
) -> None:
    client, agent_id = failed_provider_agent
    text = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "still reachable", "source": "user"},
    )
    assert text.status_code == 201, text.text
    assert client.get(f"/api/agents/{agent_id}/messages").status_code == 200


def test_normal_birth_pin_keeps_list_and_detail_capabilities_consistent(
    db_conn: psycopg.Connection,
    cluster_defaults_unset: None,
    *,
    machine_identity: MachineIdentity,
) -> None:
    with gateway_test_client(machine=machine_identity) as client:
        chosen = client.put("/api/config/default-model", json={"model": "claude-sonnet-5"})
        assert chosen.status_code == 200, chosen.text
        born = spawn_via_http(client, {"prompt": "retain the birth model", "prompt_source": "user"})
        assert born.status_code == 201, born.text
        agent_id = born.json()["id"]
        pins = db_conn.execute(
            "SELECT config_overlay ->> 'llm_model', birth_config ->> 'llm_model' FROM agents_meta WHERE id=%s",
            (agent_id,),
        ).fetchone()
        assert pins == (None, "claude-sonnet-5")
        changed = client.put("/api/config/default-model", json={"model": "deepseek-flash"})
        assert changed.status_code == 200, changed.text
        detail = client.get(f"/api/agents/{agent_id}")
        assert detail.status_code == 200 and detail.json()["supports_vision"] is True
        for path in ("/api/agents", "/api/agents/roster"):
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert response.json()["agents"][0]["supports_vision"] is True


def test_existing_agent_projection_survives_provider_failure(
    failed_provider_agent: tuple[GatewayTestClient, int],
) -> None:
    client, agent_id = failed_provider_agent
    for path in ("/api/agents", "/api/agents/roster"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert [card["agent_id"] for card in response.json()["agents"]] == [agent_id]
        assert response.json()["agents"][0]["supports_vision"] is None
    detail = client.get(f"/api/agents/{agent_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["supports_vision"] is None
    assert detail.json()["status"] == "idling"


def test_model_requests_report_unavailable(
    failed_provider_agent: tuple[GatewayTestClient, int],
) -> None:
    client, agent_id = failed_provider_agent
    for path in (
        "/api/models",
        "/api/config/default-model",
        "/api/config/resolved",
        f"/api/agents/{agent_id}/token-usage",
        f"/api/agents/{agent_id}/context-breakdown",
    ):
        response = client.get(path)
        assert response.status_code == 503, (path, response.text)
        assert response.json()["code"] == "model_catalog_unavailable"
    for path, body in (
        ("/api/memory/search", {"query": "recover the provider", "purpose": "search"}),
        (
            f"/api/agents/{agent_id}/restart",
            {"config_overlay": {"llm_model": "deepseek-flash"}},
        ),
        (
            f"/api/agents/{agent_id}/messages",
            {
                "source": "user",
                "content": [{"type": "image_url", "image_url": {"url": "upload://1/missing.png"}}],
            },
        ),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 503, (path, response.text)
        assert response.json()["code"] == "model_catalog_unavailable"
