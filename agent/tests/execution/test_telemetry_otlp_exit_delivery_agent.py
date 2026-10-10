"""An attachment's tail public SDK entries reach OTLP once each before exit."""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from base.telemetry import Event, event_row

_POSTS: list[tuple[str, bytes]] = []


_POSTS_LOCK = threading.Lock()


class _ReceiverHandler(http.server.BaseHTTPRequestHandler):
    """Records every POST body and answers 200 — the reachability probe and
    the OTLP exports both pass through this one handler."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        with _POSTS_LOCK:
            _POSTS.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def otlp_receiver() -> Any:
    _POSTS.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ReceiverHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


_EXTERNAL_CLI_CHILD = """
from pathlib import Path
import ava
from ava import external
from agent.extensions.registry import build_registry
from agent.state import build_agent_state

SAMPLE = {sample!r}
LEASE = {{
    "id": "lease",
    "session_id": 0,
    "agent_id": 424242,
    "machine": "external-cli-probe",
    "status": "active",
    "delta_version": 0,
    "applied_version": 0,
    "plugin_delta": [],
    "automatic": False,
    "event_delivery_protocol_version": None,
}}

external.control.require_active = lambda _db, _lease_id, _caller: dict(LEASE)
external.machine_name = lambda: "external-cli-probe"
external.process_metadata = lambda: {{"pid": 1}}
def load_snapshot(_agent_id, **_kwargs):
    state = build_agent_state(build_registry())()
    state.ava_code__cwd = str(Path(SAMPLE).parent)
    return state, {{}}, None

external.load_snapshot = load_snapshot

with external.attach("lease"):
    assert ava.files.read(SAMPLE) == "tail event"
"""


def _log_request(raw: bytes) -> Any | None:
    """Parse one OTLP request, ignoring the receiver reachability probe."""
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

    request = logs_service_pb2.ExportLogsServiceRequest()
    with contextlib.suppress(Exception):
        request.ParseFromString(raw)
        return request
    return None


def _event_body(record: Any) -> dict[str, Any] | None:
    """Return one JSON event body when the log record carries one."""
    if not record.body.string_value:
        return None
    with contextlib.suppress(Exception):
        return json.loads(record.body.string_value)
    return None


def _sent_event_bodies() -> list[dict[str, Any]]:
    """Decode event bodies received by the local OTLP collector."""
    events: list[dict[str, Any]] = []
    with _POSTS_LOCK:
        posts = list(_POSTS)
    for path, raw in posts:
        if not path.endswith("/v1/logs") or len(raw) < 4:
            continue
        request = _log_request(raw)
        if request is None:
            continue
        for resource_logs in request.resource_logs:
            for scope_logs in resource_logs.scope_logs:
                for record in scope_logs.log_records:
                    if event := _event_body(record):
                        events.append(event)
    return events


def _mirror_events(home: Path) -> list[dict[str, Any]]:
    """Read every sandbox JSONL event row."""
    return [
        json.loads(line)
        for path in sorted((home / "logs").glob("events-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _assert_external_sdk_call(
    delivered: list[dict[str, Any]], mirror: list[dict[str, Any]]
) -> None:
    """Check every public entry preserves its borrower and exact mirror identity."""
    # ava_code's files.read wrapper enters the public cwd.get helper as well.
    expected = Counter({"files.read": 1, "cwd.get": 1})
    assert Counter(event["attributes"]["fn"] for event in delivered) == expected, delivered
    mirrored = [row for row in mirror if row["event_name"] == "sdk_call"]
    assert Counter(event["attributes"]["fn"] for event in mirrored) == expected, mirrored
    mirror_ids = {row["attributes"]["fn"]: row["id"] for row in mirrored}
    for event in delivered:
        assert event["agent_id"] == 424242
        assert event["source"] == "agent:424242"
        event_for_id: dict[str, Any] = dict(event)
        event_for_id["ts"] = datetime.fromisoformat(event["ts"])
        assert mirror_ids[event["attributes"]["fn"]] == event_row(Event(**event_for_id))["id"]


def test_external_cli_tail_sdk_call_reaches_receiver_once_before_exit(
    tmp_path: Path, otlp_receiver: Any
) -> None:
    """An attachment's final SDK call is exported while the CLI is still alive."""
    sample = tmp_path / "sample.txt"
    sample.write_text("tail event", encoding="utf-8")
    home = tmp_path / "home"
    env = os.environ.copy()
    env.update(
        {
            "AVA_HOME": str(home),
            "AVA_PROCESS_PROFILE": "agent",
            "AVA_CONFIG_FETCH": "skip",
            "AVA_TELEMETRY_OTLP_ENDPOINT": f"http://127.0.0.1:{otlp_receiver.server_address[1]}",
            "AVA_TELEMETRY_OTLP_ENABLED": "true",
        }
    )
    env.pop("AVA_LOG_DIR", None)
    proc = subprocess.run(  # noqa: S603 — fixed argv: our own interpreter, inline program
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-c",
            _EXTERNAL_CLI_CHILD.format(sample=str(sample)),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    deadline = time.monotonic() + 10.0
    delivered: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        delivered = [
            event for event in _sent_event_bodies() if event.get("event_name") == "sdk_call"
        ]
        if delivered:
            break
        time.sleep(0.05)

    _assert_external_sdk_call(delivered, _mirror_events(home))
