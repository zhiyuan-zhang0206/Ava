"""CLI dispatch drains its first telemetry batch before interpreter shutdown."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

_CHILD = Path(__file__).with_name("bootstrap") / "child_telemetry_exit.py"


@pytest.fixture
def receiver() -> Generator[tuple[http.server.ThreadingHTTPServer, list[bytes]]]:
    bodies: list[bytes] = []

    class Receiver(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/v1/logs" and not body.startswith(b"{"):
                bodies.append(body)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server, bodies
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("mode", ["success", "exception", "empty", "unavailable"])
def test_cli_first_event_reaches_otlp_before_exit(
    tmp_path: Path, mode: str, receiver: tuple[http.server.ThreadingHTTPServer, list[bytes]]
) -> None:
    server, bodies = receiver
    if mode == "unavailable":
        server.shutdown()
        server.server_close()
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.update(
        AVA_HOME=str(tmp_path / "home"),
        AVA_CONFIG_FETCH="skip",
        AVA_TELEMETRY_OTLP_ENABLED="true",
        AVA_TELEMETRY_OTLP_ENDPOINT=f"http://127.0.0.1:{server.server_address[1]}",
    )
    child = subprocess.run(  # noqa: S603 — fixed program and this checkout's interpreter
        [sys.executable, "-I", str(_CHILD), mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    # The child holds the command open for several default flush intervals with a 60 s pipeline
    # timer: any batch written meanwhile came from a timer, not from the drain after the command.
    assert "early_batch_writes=0" in child.stdout, child.stdout + child.stderr
    rows = _mirror_rows(tmp_path / "home")
    disabled = [row for row in rows if row["event_name"] == "otlp_backend_disabled"]
    names = _log_names(bodies)
    if mode == "empty":
        assert not bodies
        assert not (tmp_path / "home" / "logs").exists()
    elif mode == "unavailable":
        _assert_unavailable(bodies, rows, disabled)
    else:
        assert "health_probe_ran" in names, child.stderr
        assert not disabled, disabled


def _mirror_rows(home: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in (home / "logs").glob("events-*.jsonl")
        for line in path.read_text().splitlines()
    ]


def _log_names(bodies: list[bytes]) -> list[str]:
    names: list[str] = []
    for body in bodies:
        request = logs_service_pb2.ExportLogsServiceRequest.FromString(body)
        for resource in request.resource_logs:
            for scope in resource.scope_logs:
                for record in scope.log_records:
                    names.append(json.loads(record.body.string_value)["event_name"])
    return names


def _assert_unavailable(
    bodies: list[bytes], rows: list[dict[str, Any]], disabled: list[dict[str, Any]]
) -> None:
    assert not bodies
    assert any(row["event_name"] == "health_probe_ran" for row in rows)
    assert disabled and all(
        row["attributes"]["reason"] == "endpoint not answering" for row in disabled
    )
