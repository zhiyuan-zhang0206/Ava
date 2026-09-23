"""Bootstrap serves the authoritative hosted turn limit without rewriting zero."""

import json
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routers.bootstrap import router
from shared import bootstrap, config, runtime_config
from shared.cluster_auth import bearer_header

_SECRET = "bootstrap-admission-test"  # noqa: S105 — isolated test credential
_TURN_LIMIT = "AVA_HOST_MAX_CONCURRENT_TURNS"


@pytest.fixture
def gateway_snapshot(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    aliases = runtime_config.read_env_aliases()
    for alias in (_TURN_LIMIT, "AVA_HOST_DB_POOL_MAX_SIZE", "AVA_HOST_CONTROL_POOL_MAX_SIZE"):
        aliases.pop(alias, None)
    monkeypatch.setattr(runtime_config, "read_env_aliases", lambda: aliases)
    monkeypatch.setattr(config.settings.daemon, "host_max_concurrent_turns", 0)
    monkeypatch.setattr(config.settings.daemon, "host_db_pool_max_size", 64)
    monkeypatch.setattr(config.settings.daemon, "host_control_pool_max_size", 8)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    return aliases


@pytest.fixture
def gateway_client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("role", [None, "runner"])
@pytest.mark.parametrize(
    "raw", [None, "0", "00", "+0", " 0 ", "0.0", "7", "+007", "7.0", "invalid"]
)
def test_bootstrap_serves_turn_limit_verbatim(
    gateway_snapshot: dict[str, str], gateway_client: TestClient, role: str | None, raw: str | None
) -> None:
    if raw is not None:
        gateway_snapshot[_TURN_LIMIT] = raw
    params = {"role": role} if role else {}
    response = gateway_client.get("/api/bootstrap", params=params, headers=bearer_header(_SECRET))
    assert response.status_code == 200
    served = response.json()
    assert served[_TURN_LIMIT] == (raw if raw is not None else "0")
    assert served["AVA_HOST_DB_POOL_MAX_SIZE"] == "64"
    assert served["AVA_HOST_CONTROL_POOL_MAX_SIZE"] == "8"


@pytest.mark.parametrize("secret", [None, "wrong-secret"])
def test_bootstrap_admission_preserves_auth(
    gateway_snapshot: dict[str, str], gateway_client: TestClient, secret: str | None
) -> None:
    headers = bearer_header(secret) if secret else {}
    response = gateway_client.get("/api/bootstrap", headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize("role", [None, "runner"])
def test_no_secret_bootstrap_serves_zero_verbatim(
    gateway_snapshot: dict[str, str],
    gateway_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
) -> None:
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "")
    response = gateway_client.get("/api/bootstrap", params={"role": role} if role else {})
    assert response.status_code == 200
    assert response.json()[_TURN_LIMIT] == "0"


def test_runner_fetch_replaces_stale_admission(
    gateway_snapshot: dict[str, str], gateway_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {
        "AVA_GATEWAY_URL": "http://gateway",
        "AVA_CLUSTER_SECRET": _SECRET,
        _TURN_LIMIT: "16",
        "AVA_HOST_DB_POOL_MAX_SIZE": "20",
        "AVA_HOST_CONTROL_POOL_MAX_SIZE": "4",
    }
    monkeypatch.setattr(bootstrap, "os", SimpleNamespace(environ=env))
    requests: list[str] = []

    def dial(url: str, *, timeout: float, headers: dict[str, str]) -> httpx2.Response:
        assert timeout > 0
        requests.append(url)
        return gateway_client.get(url, headers=headers)

    monkeypatch.setattr(bootstrap, "dial_get", dial)
    bootstrap.inject_config_from_gateway()
    assert len(requests) == 1
    assert parse_qs(urlsplit(requests[0]).query) == {"role": ["runner"]}
    assert env[_TURN_LIMIT] == "0"
    assert env["AVA_HOST_DB_POOL_MAX_SIZE"] == "64"
    assert env["AVA_HOST_CONTROL_POOL_MAX_SIZE"] == "8"


@pytest.mark.parametrize("age_seconds", [1.0, 10_000.0], ids=["fresh", "stale"])
def test_runner_refreshes_an_outdated_admission_snapshot(
    gateway_snapshot: dict[str, str],
    gateway_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    age_seconds: float,
) -> None:
    env = {
        "AVA_HOME": str(tmp_path),
        "AVA_GATEWAY_URL": "http://gateway",
        "AVA_CLUSTER_SECRET": _SECRET,
    }
    snapshot = tmp_path / "run" / "bootstrap-snapshot.json"
    snapshot.parent.mkdir()
    snapshot.write_text(
        json.dumps(
            {
                "v": 1,
                "base_url": "http://gateway",
                "written_at": time.time() - age_seconds,
                "values": {_TURN_LIMIT: "16"},
            }
        )
    )
    monkeypatch.setattr(bootstrap, "os", SimpleNamespace(**{**vars(bootstrap.os), "environ": env}))
    assert bootstrap._read_config_snapshot("http://gateway") is None

    def dial(url: str, *, timeout: float, headers: dict[str, str]) -> httpx2.Response:
        return gateway_client.get(url, headers=headers)

    monkeypatch.setattr(bootstrap, "dial_get", dial)
    bootstrap.inject_config_from_gateway()
    assert env[_TURN_LIMIT] == "0"
    written = bootstrap._read_config_snapshot("http://gateway")
    assert written is not None and written[0][_TURN_LIMIT] == "0"
