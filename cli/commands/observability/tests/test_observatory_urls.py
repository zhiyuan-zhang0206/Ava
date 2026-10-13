"""Grafana provisioning follows the configured data plane and gateway."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import psycopg
import pytest
from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict

from base import cluster
from base.cluster.dataplane import direct_url
from base.config import settings
from base.packages import dashboard_supply
from base.telemetry import EventPipeline
from cli.commands.observability import lgtm_native, observatory_urls
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


@pytest.fixture
def _dashboard_supply_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Endpoint rendering does not dial its synthetic connection inputs.

    The optional dashboard supplier retains its existing unavailable fallback;
    the separate native endpoint test still dials the actual private database.
    """

    def unavailable(_database: object) -> None:
        raise RuntimeError("native dashboard supply is unavailable in this rendering test")

    monkeypatch.setattr(dashboard_supply, "render_dashboard_json", unavailable)


def _render(
    tmp_path: Path,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> dict[str, str | None]:
    native = tmp_path / "native"
    lgtm_native.render_configs(
        Path(__file__).resolve().parents[4],
        native,
        tmp_path,
        database_factory=operator_database,
        producer=producer,
        machine=command_runtime.machine,
    )
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
    _dashboard_supply_unavailable: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    observatory: str,
    db_url: str,
    expected: str,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    monkeypatch.setattr(settings.observability, "observability_url", observatory)
    monkeypatch.setattr(settings.data_plane, "db_url", db_url)
    command_runtime.config.set_field("gateway_url", "http://gateway.test:20016")
    values = _render(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    assert values["AVA_PG_URL"] == expected
    assert "synthetic-secret" not in (tmp_path / "native/config/runtime.env").read_text()
    captured = capsys.readouterr()
    assert "synthetic-secret" not in captured.out + captured.err
    assert values["AVA_TELEMETRY_LOKI_URL"] == (
        "http://observatory.test:3100" if observatory else "http://127.0.0.1:3100"
    )


def test_registered_pooler_is_rendered_as_direct_postgres(
    _dashboard_supply_unavailable: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
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

    def _record(_home: Path) -> cluster.ClusterRecord:
        return record

    monkeypatch.setattr(cluster, "get_record", _record)
    monkeypatch.setattr(direct_url, "get_record", _record)
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://reader@127.0.0.1:20029/ava")
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", True)
    assert (
        _render(
            tmp_path,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        )["AVA_PG_URL"]
        == "127.0.0.1:20027"
    )


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
    _dashboard_supply_unavailable: None,
    monkeypatch: pytest.MonkeyPatch,
    observatory: str,
    gateway_url: str,
    port: int,
    expected: str,
    *,
    command_runtime: CommandRuntime,
) -> None:
    monkeypatch.setattr(settings.observability, "observability_url", observatory)
    monkeypatch.setattr(settings.gateway, "gateway_url", gateway_url)
    monkeypatch.setattr(settings.gateway, "gateway_port", port)
    command_runtime.machine.set(host="10.0.0.10")
    assert observatory_urls.alerts_webhook_url(machine=command_runtime.machine) == expected


def test_webhook_rejects_url_credentials_without_reporting_them(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    monkeypatch.setattr(settings.observability, "observability_url", "http://observatory.test")
    monkeypatch.setattr(
        settings.gateway, "gateway_url", "https://user:synthetic-secret@gateway.test"
    )
    with pytest.raises(ValueError, match="credential-free") as error:
        observatory_urls.alerts_webhook_url(machine=command_runtime.machine)
    captured = capsys.readouterr()
    assert "synthetic-secret" not in str(error.value) + captured.out + captured.err


@pytest.mark.parametrize("path", ["$AVA_REVIEW_LITERAL", "$(touch${IFS}webhook-command-ran)"])
def test_real_grafana_launcher_preserves_webhook_url_literal(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path: str,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """The actual launcher sources runtime.env without expanding URL path code."""
    base = f"https://gateway.test:20016/tenant/{path}"
    monkeypatch.setattr(settings.observability, "observability_url", "http://observatory.test")
    monkeypatch.setattr(settings.gateway, "gateway_url", base)
    native = tmp_path / "lgtm/native"
    lgtm_native.render_configs(
        Path(__file__).resolve().parents[4],
        native,
        tmp_path,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
    )
    executable = native / "grafana-home/bin/grafana"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        "print(json.dumps(os.environ['AVA_ALERTS_WEBHOOK_URL']))\n"
    )
    executable.chmod(0o755)
    result = subprocess.run(
        ["bash", str(native / "grafana/run.sh")],
        cwd=tmp_path,
        env={**os.environ, "AVA_REVIEW_LITERAL": "expanded"},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "webhook-command-ran").exists()
    assert json.loads(result.stdout) == f"{base}/api/alerts"


@pytest.mark.parametrize("observatory", ["", "http://observatory.test"])
def test_rendered_endpoints_reach_private_postgres_and_http_listener(
    db_conn: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    observatory: str,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
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
            rendered = _render(
                tmp_path,
                operator_database=operator_database,
                producer=operator_pipeline,
                command_runtime=command_runtime,
            )
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
