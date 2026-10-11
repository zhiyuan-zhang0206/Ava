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

_CHILD = r"""
import argparse
import concurrent.futures.thread
import sys
from cli import main, parsers
from base import telemetry
from base.telemetry import emitter, event_store
from base.telemetry.metrics import observed_metrics

# Isolate data persistence; receiver and OTel providers are real. A long batch
# interval makes first initialization at exit deterministic without sleeps.
emitter._FLUSH_INTERVAL_S = 60
event_store.store_events = lambda *args: None
observed_metrics.project_events = lambda *args: None

def dispatch(args):
    if sys.argv[1] != "empty":
        telemetry.init_telemetry(process="cli-health-probe", pipeline=args.producer())
        telemetry.emit("telemetry", "health_probe_ran", attributes={"unhealthy_checks": 0})
    if sys.argv[1] == "exception":
        raise RuntimeError("handler failure")
    return 7

parser = argparse.ArgumentParser()
parser.set_defaults(func=dispatch)
parsers.build_parser = lambda *, retained_children: parser
try:
    result = main.main([])
except RuntimeError as exc:
    assert str(exc) == "handler failure"
else:
    assert result == 7
"""


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
        [sys.executable, "-I", "-c", _CHILD, mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 0, child.stderr
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
