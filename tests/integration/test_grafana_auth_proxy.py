"""Pinned runtime proof of the shipped Grafana auth-proxy service contract.

The disposable container consumes the compose service's actual image, GF_*
environment, and provisioning bind. Docker must work in CI; local machines
without it skip explicitly.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import cast
from urllib.parse import urljoin

import httpx
import pytest
import yaml
from websockets.sync.client import connect
from websockets.typing import Origin

_COMPOSE_PATH = Path(__file__).resolve().parents[2] / "deploy/lgtm/docker-compose.yml"
_PUBLIC_ORIGIN = Origin("http://gateway.test:8000")
_VIEWER_HEADERS = {
    "X-Ava-Grafana-User": "ava-cluster-viewer",
    "X-Ava-Grafana-Role": "Viewer",
}
_log = logging.getLogger(__name__)


def _docker_or_skip() -> str:
    docker = shutil.which("docker")
    if docker is None:
        if os.environ.get("CI"):
            pytest.fail("CI must provide Docker for the pinned Grafana runtime proof")
        pytest.skip("Docker is unavailable on this development machine")
    probe = subprocess.run(  # noqa: S603 — resolved absolute Docker CLI
        [docker, "info"], check=False, capture_output=True, text=True, timeout=30
    )
    if probe.returncode != 0:
        if os.environ.get("CI"):
            pytest.fail(f"CI Docker daemon unavailable: {probe.stderr[-500:]}")
        pytest.skip("Docker daemon is unavailable on this development machine")
    return docker


def _run_docker(docker: str, *args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — resolved absolute Docker CLI, fixed arguments
        [docker, *args], check=False, capture_output=True, text=True, timeout=timeout
    )


def _shipped_service() -> tuple[str, dict[str, str], str]:
    compose = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    service = compose["services"]["grafana"]
    image = service["image"]
    raw_environment = service["environment"]
    environment = {
        key: str(value) for key, value in raw_environment.items() if key.startswith("GF_")
    }
    assert environment["GF_SERVER_ROOT_URL"].startswith("${GRAFANA_ROOT_URL:?")
    environment["GF_SERVER_ROOT_URL"] = f"{_PUBLIC_ORIGIN}/grafana/"
    assert not any("${" in value for value in environment.values())
    provisioning = next(
        volume for volume in service["volumes"] if "/etc/grafana/provisioning" in volume
    )
    source, target, mode = provisioning.split(":")
    source_path = (_COMPOSE_PATH.parent / source).resolve()
    assert source_path.is_dir() and target == "/etc/grafana/provisioning" and mode == "ro"
    return image, environment, f"{source_path}:{target}:{mode}"


def _published_port(docker: str, name: str) -> int:
    mapping = _run_docker(docker, "port", name, "3000/tcp", timeout=30)
    if mapping.returncode != 0:
        pytest.fail(f"could not inspect isolated Grafana port: {mapping.stderr[-500:]}")
    return int(mapping.stdout.strip().rsplit(":", 1)[1])


def _wait_for_grafana(client: httpx.Client, root: str) -> dict[str, object]:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            response = client.get(f"{root}/api/health")
            if response.status_code == 200 and isinstance(response.json(), dict):
                return response.json()
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(1)
    pytest.fail("shipped Grafana service did not become ready within 90 seconds")


def _assert_login_closed(client: httpx.Client, root: str) -> None:
    assert client.get(f"{root}/api/user").status_code == 401
    basic = base64.b64encode(b"admin:admin").decode()
    assert (
        client.get(f"{root}/api/user", headers={"Authorization": f"Basic {basic}"}).status_code
        == 401
    )
    login = client.post(f"{root}/login", json={"user": "admin", "password": "admin"})
    assert login.status_code >= 400
    assert "set-cookie" not in login.headers
    assert client.get(f"{root}/api/user").status_code == 401


def _assert_viewer(client: httpx.Client, root: str) -> tuple[dict[str, object], list[str]]:
    response = client.get(f"{root}/api/user", headers=_VIEWER_HEADERS)
    assert response.status_code == 200 and "set-cookie" not in response.headers
    user = response.json()
    assert user["login"] == "ava-cluster-viewer" and user["isGrafanaAdmin"] is False
    orgs_response = client.get(f"{root}/api/user/orgs", headers=_VIEWER_HEADERS)
    assert orgs_response.status_code == 200
    orgs: object = orgs_response.json()
    assert isinstance(orgs, list) and orgs
    roles: list[str] = []
    for raw_org in cast(list[object], orgs):
        assert isinstance(raw_org, dict)
        org = cast(dict[str, object], raw_org)
        assert org.get("role") == "Viewer"
        roles.append("Viewer")
    assert client.get(f"{root}/api/search?limit=1", headers=_VIEWER_HEADERS).status_code == 200
    assert client.get(f"{root}/api/datasources", headers=_VIEWER_HEADERS).status_code == 200
    datasource_query = client.post(
        f"{root}/api/ds/query",
        headers=_VIEWER_HEADERS,
        json={"queries": [], "from": "0", "to": "1"},
    )
    assert datasource_query.status_code == 200
    assert datasource_query.json() == {"results": {}}
    mutation = client.post(
        f"{root}/api/dashboards/db",
        headers=_VIEWER_HEADERS,
        json={"dashboard": {"title": "must-not-write"}},
    )
    assert mutation.status_code == 403
    return user, roles


def _assert_embeddable_ui(client: httpx.Client, root: str) -> str:
    page = client.get(f"{root}/", headers=_VIEWER_HEADERS)
    assert page.status_code == 200
    assert "set-cookie" not in page.headers and "x-frame-options" not in page.headers
    assert "content-security-policy" not in page.headers
    match = re.search(r'(?:src|href)="([^"]*public/build/[^"]+\.(?:js|css))"', page.text)
    assert match is not None
    asset_url = urljoin(f"{root}/", match.group(1))
    assert httpx.URL(asset_url).path.startswith("/grafana/public/build/")
    assert client.get(asset_url, headers=_VIEWER_HEADERS).status_code == 200
    return httpx.URL(asset_url).path


def _assert_live(port: int) -> None:
    with connect(
        f"ws://127.0.0.1:{port}/grafana/api/live/ws?orgId=1",
        origin=_PUBLIC_ORIGIN,
        additional_headers=_VIEWER_HEADERS,
        proxy=None,
        open_timeout=10,
        close_timeout=5,
    ):
        pass


def test_real_grafana_fixed_viewer_auth_proxy() -> None:
    docker = _docker_or_skip()
    image, environment, provisioning = _shipped_service()
    name = f"ava-grafana-auth-{uuid.uuid4().hex[:12]}"
    args = ["run", "--detach", "--name", name, "--publish", "127.0.0.1::3000"]
    for key, value in environment.items():
        args.extend(("--env", f"{key}={value}"))
    args.extend(("--volume", provisioning, image))
    started = _run_docker(docker, *args)
    if started.returncode != 0:
        pytest.fail(f"failed to start shipped Grafana image: {started.stderr[-1000:]}")
    try:
        port = _published_port(docker, name)
        root = f"http://127.0.0.1:{port}/grafana"
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            health = _wait_for_grafana(client, root)
            assert health.get("version") == image.rpartition(":")[2]
            _assert_login_closed(client, root)
            user, roles = _assert_viewer(client, root)
            asset_path = _assert_embeddable_ui(client, root)
        _assert_live(port)
        image_id = _run_docker(docker, "inspect", "--format={{.Image}}", name).stdout.strip()
        proof = {
            "auth_proxy_login": user["login"],
            "compose_service": "grafana",
            "grafana_admin": user["isGrafanaAdmin"],
            "grafana_version": health["version"],
            "image": image,
            "image_id": image_id,
            "live_websocket": "handshake-ok",
            "org_roles": sorted(set(roles)),
            "subpath_asset": asset_path,
        }
        proof_path = Path("tmp/grafana-integration-proof.json")
        proof_path.parent.mkdir(parents=True, exist_ok=True)
        proof_path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")
    except BaseException:
        logs = _run_docker(docker, "logs", name, timeout=30)
        _log.error("isolated Grafana logs:\n%s", logs.stdout[-5000:] + logs.stderr[-5000:])
        raise
    finally:
        _run_docker(docker, "rm", "--force", name, timeout=60)
