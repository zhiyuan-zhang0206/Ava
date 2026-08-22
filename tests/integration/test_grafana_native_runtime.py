"""Grafana navigation contract against the real, native Grafana binary.

The regular test shards skip this module because the repository does not vendor
Grafana.  CI downloads the checksum-pinned 13.1.3 release and points the two
``AVA_TEST_GRAFANA_*`` variables at it.  No Docker daemon is involved.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "deploy/lgtm/docker-compose.yml"
RUNTIME_ENV_PATH = REPO_ROOT / "deploy/lgtm/config/grafana/runtime.env"
TRACE_ID = "70d53b9c44efa6d116f9b26a950e3309"


def _grafana_distribution() -> tuple[Path, Path]:
    binary = os.environ.get("AVA_TEST_GRAFANA_BIN")
    home = os.environ.get("AVA_TEST_GRAFANA_HOME")
    if binary is None or home is None:
        pytest.skip(
            "set AVA_TEST_GRAFANA_BIN and AVA_TEST_GRAFANA_HOME to run the native Grafana contract"
        )
    assert binary is not None and home is not None
    return Path(binary), Path(home)


def _grafana_environment() -> dict[str, str]:
    compose: dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    raw: dict[str, object] = compose["services"]["grafana"]["environment"]
    environment = {str(key): str(value) for key, value in raw.items()}
    for line in RUNTIME_ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            environment[key] = value
    return environment


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(
    url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, str, bytes]:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args: object, **_kwargs: object) -> None:
            return None

    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(  # noqa: S310 - loopback URL assembled by this test
        url, data=data, headers=headers or {}
    )
    try:
        response = opener.open(request, timeout=5)
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Location", ""), error.read()
    with response:
        return response.status, response.headers.get("Location", ""), response.read()


def _wait_until_healthy(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, _ = process.communicate()
            pytest.fail(f"Grafana exited {process.returncode} before becoming healthy:\n{stdout}")
        try:
            status, _, body = _request(f"{base_url}/api/health")
            if status == 200 and json.loads(body)["database"] == "ok":
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    pytest.fail("Grafana did not become healthy within 30 seconds")


def _explore_url(base_url: str) -> str:
    left = {
        "datasource": "tempo",
        "queries": [
            {
                "datasource": {"type": "tempo", "uid": "tempo"},
                "query": TRACE_ID,
                "queryType": "traceql",
                "refId": "A",
            }
        ],
        "range": {"from": "now-1h", "to": "now"},
    }
    return f"{base_url}/explore?{urllib.parse.urlencode({'left': json.dumps(left, separators=(',', ':'))})}"


def test_shipped_grafana_runtime_contract() -> None:
    """Keep security, concurrency, and capacity settings transport-neutral."""
    compose: dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    grafana: dict[str, Any] = compose["services"]["grafana"]
    environment = _grafana_environment()

    assert grafana["env_file"] == ["./config/grafana/runtime.env"]
    assert environment["GF_USERS_VIEWERS_CAN_EDIT"] == "true"
    assert environment["GF_DATABASE_QUERY_RETRIES"] == "5"
    assert environment["GF_LIVE_MAX_CONNECTIONS"] == "0"
    assert float(grafana["cpus"]) >= 2
    assert grafana["mem_limit"] == "2g"
    assert grafana["memswap_limit"] == "2g"


def test_default_dashboard_refresh_is_load_bounded() -> None:
    dashboard_dir = REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards"
    expected = {
        "ava-host-dataplane.json",
        "ava-ops-main.json",
        "ava-ops-plugins.json",
        "ava-overview.json",
    }
    actual = {
        path.name: json.loads(path.read_text(encoding="utf-8"))["refresh"]
        for path in dashboard_dir.glob("ava-*.json")
    }
    assert actual == dict.fromkeys(expected, "5m")


def test_anonymous_viewer_can_open_tempo_trace_in_explore(tmp_path: Path) -> None:
    """Regression: a dashboard Trace link must not redirect Viewer to Home."""
    binary, home = _grafana_distribution()
    shipped = _grafana_environment()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}/grafana"
    admin_user = "native-contract-admin"
    admin_password = f"native-contract-{TRACE_ID}"
    admin_headers = {
        "Authorization": "Basic "
        + base64.b64encode(f"{admin_user}:{admin_password}".encode()).decode()
    }
    env = os.environ.copy()
    env.update(shipped)
    env.update(
        {
            "GF_SERVER_HTTP_ADDR": "127.0.0.1",
            "GF_SERVER_HTTP_PORT": str(port),
            "GF_SERVER_ROOT_URL": f"{base_url}/",
            "GF_PATHS_DATA": str(tmp_path / "data"),
            "GF_PATHS_LOGS": str(tmp_path / "logs"),
            "GF_PATHS_PLUGINS": str(tmp_path / "plugins"),
            "GF_PATHS_PROVISIONING": str(tmp_path / "provisioning"),
            "GF_LOG_LEVEL": "error",
            "GF_LOG_MODE": "console",
            "GF_SECURITY_ADMIN_USER": admin_user,
            "GF_SECURITY_ADMIN_PASSWORD": admin_password,
        }
    )
    for directory in ("data", "logs", "plugins", "provisioning"):
        (tmp_path / directory).mkdir()

    process = subprocess.Popen(  # noqa: S603 - checksum-pinned test binary
        [str(binary), "server", "--homepath", str(home)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_until_healthy(base_url, process)
        settings_status, _, settings_body = _request(f"{base_url}/api/frontend/settings")
        assert settings_status == 200
        settings = json.loads(settings_body)
        assert settings["viewersCanEdit"] is True
        assert settings["liveEnabled"] is False
        admin_status, _, admin_body = _request(
            f"{base_url}/api/admin/settings",
            headers=admin_headers,
        )
        assert admin_status == 200
        admin_settings = json.loads(admin_body)
        assert admin_settings["database"]["query_retries"] == "5"

        # Import the exact shipped dashboard into this disposable instance so
        # its Grafana-13 JSON schema and the user-facing subpath are both real.
        dashboard = json.loads(
            (
                REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
            ).read_text(encoding="utf-8")
        )
        import_status, _, import_body = _request(
            f"{base_url}/api/dashboards/db",
            data=json.dumps({"dashboard": dashboard, "overwrite": True}).encode(),
            headers={**admin_headers, "Content-Type": "application/json"},
        )
        assert import_status == 200, (import_status, import_body[:500])
        dashboard_status, dashboard_location, _ = _request(f"{base_url}/d/ava-ops-main/ava-ops")
        assert dashboard_status == 200
        assert dashboard_location == ""

        explore_url = _explore_url(base_url)
        status, location, body = _request(explore_url)

        assert status == 200, (status, location, body[:500])
        assert location == ""
        parsed_left = json.loads(
            urllib.parse.parse_qs(urllib.parse.urlsplit(explore_url).query)["left"][0]
        )
        assert parsed_left["datasource"] == "tempo"
        assert parsed_left["queries"][0]["datasource"]["uid"] == "tempo"
        assert parsed_left["queries"][0]["queryType"] == "traceql"
        assert parsed_left["queries"][0]["query"] == TRACE_ID

        # viewers_can_edit grants Explore and temporary panel edits only.  It
        # must not accidentally grant permission to persist a dashboard.
        save_status, _, _ = _request(
            f"{base_url}/api/dashboards/db",
            data=b'{"dashboard":{"title":"must-not-save"},"overwrite":false}',
            headers={"Content-Type": "application/json"},
        )
        assert save_status == 403
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
