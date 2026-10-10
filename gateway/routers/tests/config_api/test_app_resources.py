"""Configuration HTTP requests use the serving app's resources, without its lifespan."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.cluster import machines
from base.cluster.machine import machine_name
from base.config import get_config_metadata
from base.config.service_read import ConfigAuthority
from base.db import Database
from gateway.routers.configuration import runtime
from ops.cluster import rpc
from ops.rpc_schemas import ConfigReadResult, HostConfigField


@pytest.fixture
def clients(
    monkeypatch: pytest.MonkeyPatch, config_authority: ConfigAuthority, tmp_path: Path
) -> list[TestClient]:
    """Two router-only apps have distinct pools, role/RPC handles and config paths."""
    apps = [FastAPI(), FastAPI()]
    for index, app in enumerate(apps):
        app.include_router(runtime.router)
        app.state.db = MagicMock(spec=Database)
        app.state.db_pool = MagicMock()
        cursor = app.state.db_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (1,)
        env_path = tmp_path / f"{index}.env"
        env_path.write_text(f"AVA_MODEL=model-{index}\n")
        app.state.config_authority = ConfigAuthority(
            config_authority.runtime, config_authority.all_domains, env_path
        )

    def index_of(db: Database) -> int:
        return next(index for index, app in enumerate(apps) if db is app.state.db)

    def roles(db: Database, target: str) -> list[str]:
        index = index_of(db)
        assert target in {"remote", f"remote-{index}", machine_name()}
        return ["agent-runner"] if index == 0 else ["agent-runner", "gateway"]

    def runners(db: Database) -> list[tuple[str, str | None]]:
        index = index_of(db)
        return [(f"remote-{index}", None)]

    async def dispatch(
        db: Database, *, target_machine: str, kind: str, payload: dict[str, object]
    ) -> dict[str, object]:
        index = index_of(db)
        if kind == "config_read":
            return ConfigReadResult(
                machine=target_machine,
                host_fields={
                    meta.name: HostConfigField(
                        value=index + 3 if meta.name == "ops_concurrency" else meta.current_value,
                        overridden=False,
                        remote_writable=meta.remote_writable,
                    )
                    for meta in get_config_metadata(authority=apps[index].state.config_authority)
                    if meta.scope == "host"
                },
                raw_overrides={"ops_concurrency": index + 3},
            ).model_dump()
        if kind == "config_audit_read":
            assert payload == {"last": 2}
            return {
                "machine": target_machine,
                "records": [{"ts": target_machine, "site": str(index)}],
            }
        assert kind == "config_write"
        assert payload["local"] is (target_machine == machine_name())
        assert payload["overrides"] == {"ops_concurrency": 4}
        return {
            "machine": target_machine,
            "results": {"ops_concurrency": {"ok": index == 0, "reason": str(index)}},
            "applied": index == 0,
            "restart_required": ["ops"] if index == 0 else [],
        }

    monkeypatch.setattr(machines, "lookup_role", roles)
    monkeypatch.setattr(machines, "list_agent_runners", runners)
    monkeypatch.setattr(rpc, "dispatch_to_machine", dispatch)
    monkeypatch.setattr(runtime, "check_env_integrity", lambda: None)
    return [TestClient(app) for app in apps]


def test_unknown_machine_uses_each_apps_pool(clients: list[TestClient]) -> None:
    """A machine known to the first app is unknown to the second app's pool."""
    second = cast(FastAPI, clients[1].app)
    cursor = second.state.db_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None
    for index in (0, 1, 0):
        response = clients[index].get("/api/config?machine=remote")
        assert response.status_code == (200 if index == 0 else 404), response.text


def test_config_read_and_capabilities_use_each_apps_db(clients: list[TestClient]) -> None:
    for index in (1, 0, 1):
        response = clients[index].get("/api/config")
        assert response.status_code == 200, response.text
        fields = {field["name"]: field for field in response.json()["fields"]}
        assert fields["ops_concurrency"]["current_value"] == index + 3
        assert fields["llm_model"]["current_value"] == f"model-{index}"

    # A remote view also uses this app's DB for its capability projection.
    for index in (1, 0, 1):
        response = clients[index].get("/api/config?machine=remote")
        assert response.status_code == 200, response.text
        assert response.json()["machine_capabilities"] == (
            ["agent-runner"] if index == 0 else ["agent-runner", "gateway"]
        )


@pytest.mark.parametrize("machine", [None, "all"])
def test_audit_reads_and_fanout_use_each_apps_db(
    clients: list[TestClient], machine: str | None
) -> None:
    for index in (1, 0, 1):
        params: dict[str, str | int] = {"last": 2}
        if machine is not None:
            params["machine"] = machine
        response = clients[index].get("/api/config/audit", params=params)
        assert response.status_code == 200, response.text
        records = response.json()["records"]
        assert len(records) == (2 if machine == "all" else 1)
        assert {record["site"] for record in records} == {str(index)}
        assert {record["machine"] for record in records} == (
            {machine_name(), f"remote-{index}"} if machine == "all" else {machine_name()}
        )


@pytest.mark.parametrize("machine", [None, "remote"])
def test_config_write_uses_each_apps_db(clients: list[TestClient], machine: str | None) -> None:
    for index in (0, 1, 0):
        params = {"machine": machine} if machine else {}
        response = clients[index].put("/api/config", params=params, json={"ops_concurrency": 4})
        assert response.status_code == 200, response.text
        assert response.json()["applied"] is (index == 0)
        assert response.json()["results"]["ops_concurrency"]["reason"] == str(index)
