"""Grafana provisioning follows the configured data plane and gateway."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx
import psycopg
import pytest
from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict

from cli.commands import _lgtm_native, _observatory_urls
from shared import cluster
from shared.config import settings


def _render(tmp_path: Path) -> dict[str, str | None]:
    native = tmp_path / "native"
    _lgtm_native._render_configs(Path(__file__).resolve().parents[2], native, tmp_path)
    return dotenv_values(native / "config/runtime.env")


@pytest.mark.parametrize("observatory", ["", "http://observatory.test"])
@pytest.mark.parametrize(
    ("db_url", "expected"),
    [
        ("postgresql://reader:synthetic-secret@127.0.0.1:5433/ava", "127.0.0.1:5433"),
        ("postgresql://reader:synthetic-secret@127.0.0.1:20027/ava", "127.0.0.1:20027"),
        ("postgresql://reader:synthetic-secret@db.test:25432/ava", "db.test:25432"),
        ("postgresql://reader:synthetic-secret@[::1]:20027/ava", "[::1]:20027"),
        ("postgresql:///ava?host=/tmp/ava-test&port=20027", "127.0.0.1:20027"),
        ("postgresql://reader@db.test/ava?port=25432", "db.test:25432"),
        ("postgresql://reader@db.test/ava", "db.test:5432"),
    ],
)
def test_rendered_pg_endpoint_follows_connection_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    observatory: str,
    db_url: str,
    expected: str,
) -> None:
    monkeypatch.setattr(settings.observability, "observability_url", observatory)
    monkeypatch.setattr(settings.data_plane, "db_url", db_url)
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://gateway.test:20016")
    values = _render(tmp_path)
    assert values["AVA_PG_URL"] == expected
    assert "synthetic-secret" not in (tmp_path / "native/config/runtime.env").read_text()
    captured = capsys.readouterr()
    assert "synthetic-secret" not in captured.out + captured.err
    assert values["AVA_TELEMETRY_LOKI_URL"] == (
        "http://observatory.test:3100" if observatory else "http://127.0.0.1:3100"
    )


def test_registered_pooler_is_rendered_as_direct_postgres(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = str(tmp_path / "home")
    record = cluster.ClusterRecord(
        gateway_home=home,
        created_at="test",
        ports=cast(
            "cluster.ClusterPorts",
            {"gateway": 20016, "postgres": 20027, "redis": 20028, "pgbouncer": 20029},
        ),
    )
    monkeypatch.setattr(cluster, "load_registry", lambda: {home: record})
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://reader@127.0.0.1:20029/ava")
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", True)
    assert _render(tmp_path)["AVA_PG_URL"] == "127.0.0.1:20027"


@pytest.mark.parametrize(
    ("observatory", "gateway_url", "port", "expected"),
    [
        ("", "", 8000, "http://127.0.0.1:8000/api/alerts"),
        ("", "http://gateway.test:20016", 20016, "http://127.0.0.1:20016/api/alerts"),
        (
            "http://observatory.test",
            "http://gateway.test:20016",
            8000,
            "http://gateway.test:20016/api/alerts",
        ),
        (
            "http://observatory.test",
            "https://gateway.test/ava/",
            8000,
            "https://gateway.test/ava/api/alerts",
        ),
        ("http://observatory.test", "http://[::1]:20016", 8000, "http://[::1]:20016/api/alerts"),
        ("http://observatory.test", "", 20016, "http://10.0.0.10:20016/api/alerts"),
    ],
)
def test_webhook_uses_local_bind_or_reachable_gateway(
    monkeypatch: pytest.MonkeyPatch,
    observatory: str,
    gateway_url: str,
    port: int,
    expected: str,
) -> None:
    monkeypatch.setattr(settings.observability, "observability_url", observatory)
    monkeypatch.setattr(settings.gateway, "gateway_url", gateway_url)
    monkeypatch.setattr(settings.gateway, "gateway_port", port)
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.10")
    assert _observatory_urls._alerts_webhook_url() == expected


def test_webhook_rejects_url_credentials_without_reporting_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings.observability, "observability_url", "http://observatory.test")
    monkeypatch.setattr(
        settings.gateway, "gateway_url", "https://user:synthetic-secret@gateway.test"
    )
    with pytest.raises(ValueError, match="credential-free") as error:
        _observatory_urls._alerts_webhook_url()
    captured = capsys.readouterr()
    assert "synthetic-secret" not in str(error.value) + captured.out + captured.err


@pytest.mark.parametrize("observatory", ["", "http://observatory.test"])
def test_rendered_endpoints_reach_private_postgres_and_http_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, observatory: str
) -> None:
    """Verify the rendered destinations on real ephemeral TCP listeners."""
    pg = conninfo_to_dict(settings.data_plane.db_url)
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(self.path)
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_port
            monkeypatch.setattr(settings.observability, "observability_url", observatory)
            monkeypatch.setattr(settings.gateway, "gateway_port", port)
            monkeypatch.setattr(settings.gateway, "gateway_url", f"http://127.0.0.1:{port}")
            rendered = _render(tmp_path)
            # Assert identity before dialing: even the regressed renderer must
            # never send this private test to the operator's default ports.
            assert rendered["AVA_PG_URL"] == f"127.0.0.1:{pg['port']}"
            assert rendered["AVA_ALERTS_WEBHOOK_URL"] == f"http://127.0.0.1:{port}/api/alerts"
            target = urlsplit("postgresql://" + str(rendered["AVA_PG_URL"]))
            with psycopg.connect(
                settings.data_plane.db_url, host=target.hostname, port=target.port
            ) as conn:
                assert conn.execute("SELECT 42").fetchone() == (42,)
            with httpx.Client(trust_env=False, timeout=3) as client:
                assert client.post(str(rendered["AVA_ALERTS_WEBHOOK_URL"])).status_code == 204
            assert received == ["/api/alerts"]
        finally:
            server.shutdown()
            thread.join(timeout=3)
