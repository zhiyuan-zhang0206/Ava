"""Bootstrap serves the authoritative hosted turn limit without rewriting zero."""

import json
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base import config
from base.config.service_read import ConfigAuthority
from base.host.db_authority.api import AcceptanceCache
from base.host.env import bootstrap
from base.host.net.auth import bearer_header
from base.lm.catalog import ModelCatalog
from gateway.cluster.bootstrap import router

_SECRET = "bootstrap-admission-test"  # noqa: S105 — isolated test credential
_TURN_LIMIT = "MEIVA_HOST_MAX_CONCURRENT_TURNS"


@pytest.fixture
def gateway_snapshot(
    monkeypatch: pytest.MonkeyPatch, config_authority: ConfigAuthority
) -> dict[str, str]:
    aliases = {"MEIVA_DB_URL": str(config.settings.data_plane.db_url)}
    for alias in (_TURN_LIMIT, "MEIVA_HOST_DB_POOL_MAX_SIZE", "MEIVA_HOST_CONTROL_POOL_MAX_SIZE"):
        aliases.pop(alias, None)

    def read_aliases(owner: ConfigAuthority) -> dict[str, str]:
        assert owner is config_authority
        return aliases

    monkeypatch.setattr(ConfigAuthority, "read_env_aliases", read_aliases)
    monkeypatch.setattr(config.settings.daemon, "host_max_concurrent_turns", 0)
    monkeypatch.setattr(config.settings.daemon, "host_db_pool_max_size", 64)
    monkeypatch.setattr(config.settings.daemon, "host_control_pool_max_size", 8)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    return aliases


@pytest.fixture
def gateway_home(tmp_path: Path) -> Path:
    """The root the gateway process serves: its generation ledger lives here."""
    home = (tmp_path / "gateway-home").resolve()
    home.mkdir(mode=0o700)
    return home


@pytest.fixture
def runner_token(
    gateway_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_write_generation: Callable[[Path], Any],
) -> str:
    """The gateway's active write generation and this runner process's delivered
    runner API token: a runner never holds the human secret."""
    token = seed_write_generation(gateway_home).api.runner
    monkeypatch.setitem(os.environ, "MEIVA_API_TOKEN", token)
    return token


@pytest.fixture
def gateway_client(
    config_authority: ConfigAuthority, model_catalog: ModelCatalog, gateway_home: Path
) -> Iterator[TestClient]:
    """Own the isolated router's admission cache without a full Gateway lifespan."""
    app = FastAPI()
    machine_token_acceptance: AcceptanceCache = {}
    app.state.machine_token_acceptance = machine_token_acceptance
    app.state.config_authority = config_authority
    app.state.catalog = model_catalog
    app.state.gateway_process = SimpleNamespace(home=gateway_home)
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
    assert served["MEIVA_HOST_DB_POOL_MAX_SIZE"] == "64"
    assert served["MEIVA_HOST_CONTROL_POOL_MAX_SIZE"] == "8"


@pytest.mark.parametrize("secret", [None, "wrong-secret"])
def test_bootstrap_admission_preserves_auth(
    gateway_snapshot: dict[str, str], gateway_client: TestClient, secret: str | None
) -> None:
    headers = bearer_header(secret) if secret else {}
    response = gateway_client.get("/api/bootstrap", headers=headers)
    assert response.status_code == 401


def test_runner_fetch_replaces_stale_admission(
    gateway_snapshot: dict[str, str],
    gateway_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner_token: str,
) -> None:
    del runner_token
    monkeypatch.setenv("MEIVA_HOME", str(tmp_path))  # the snapshot lives under this home
    env = {
        "MEIVA_GATEWAY_URL": "http://gateway",
        _TURN_LIMIT: "16",
        "MEIVA_HOST_DB_POOL_MAX_SIZE": "20",
        "MEIVA_HOST_CONTROL_POOL_MAX_SIZE": "4",
    }
    monkeypatch.setattr(bootstrap, "os", SimpleNamespace(**{**vars(bootstrap.os), "environ": env}))
    requests: list[str] = []

    def dial(url: str, *, timeout: float, headers: dict[str, str]) -> httpx2.Response:
        assert timeout > 0
        requests.append(url)
        return gateway_client.get(url, headers=headers)

    monkeypatch.setattr(bootstrap, "dial_get", dial)
    bootstrap.inject_config_from_gateway()
    assert len(requests) == 1
    # The fetch requests no projection: bootstrap serves configuration only.
    assert urlsplit(requests[0]).query == ""
    assert env[_TURN_LIMIT] == "0"
    assert env["MEIVA_HOST_DB_POOL_MAX_SIZE"] == "64"
    assert env["MEIVA_HOST_CONTROL_POOL_MAX_SIZE"] == "8"


@pytest.mark.parametrize("age_seconds", [1.0, 10_000.0], ids=["fresh", "stale"])
def test_runner_refreshes_an_outdated_admission_snapshot(
    gateway_snapshot: dict[str, str],
    gateway_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    age_seconds: float,
    runner_token: str,
) -> None:
    del runner_token
    monkeypatch.setenv("MEIVA_HOME", str(tmp_path))
    env = {"MEIVA_GATEWAY_URL": "http://gateway"}
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
    assert bootstrap.read_config_snapshot("http://gateway") is None

    def dial(url: str, *, timeout: float, headers: dict[str, str]) -> httpx2.Response:
        return gateway_client.get(url, headers=headers)

    monkeypatch.setattr(bootstrap, "dial_get", dial)
    bootstrap.inject_config_from_gateway()
    assert env[_TURN_LIMIT] == "0"
    written = bootstrap.read_config_snapshot("http://gateway")
    assert written is not None and written[0][_TURN_LIMIT] == "0"
