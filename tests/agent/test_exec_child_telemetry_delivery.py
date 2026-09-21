"""Exec-child telemetry delivery — the result/write record reaches the OTLP
receiver before the child exits (task #4312).

`agent.exec_child._run` writes the result envelope last, so its
`[exec envelope] result write` event is the child's final record. It must ride
the child's own telemetry finalize (task #4312): an exec child defers its OTLP
bring-up, and a deferred hold cannot complete once the interpreter is
finalizing — `_ensure()` refuses to construct the providers — so without the
in-life finalize the record stays in the JSONL mirror only (the fleet's
`result/write = 0` in Loki while the mirror holds every line).

Each test runs a real child against a minimal OTLP/HTTP receiver recording
every POST body, and reads both channels: the JSONL mirror and the exported
batches.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

from agent.graph._exec_protocol import (
    make_request_path,
    make_result_path,
    read_result,
    write_request,
)

# Fixed test identity — the child never dials a real DB/Redis here.
_AGENT_ID = 424242


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
        # Empty ExportLogsServiceResponse / ExportMetricsServiceResponse.
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


def _endpoint(server: Any) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _spawn(
    tmp_path: Path, code: str, endpoint: str, *, corrupt_request: bool = False
) -> tuple[subprocess.CompletedProcess[str], Path]:
    exec_dir = tmp_path / "exec"
    request_path = make_request_path(exec_dir, agent_id=_AGENT_ID)
    result_path = make_result_path(exec_dir, agent_id=_AGENT_ID)
    if corrupt_request:
        request_path.write_text("corrupt request body", encoding="utf-8")
    else:
        write_request(request_path, code=code, agent_id=_AGENT_ID, timeout_s=60.0, state=None)
    env = os.environ.copy()
    env.update(
        {
            "AVA_HOME": str(tmp_path / "home"),
            "AVA_AGENT_ID": str(_AGENT_ID),
            "AVA_PROCESS_PROFILE": "agent",
            "AVA_EXEC_REQUEST_FILE": str(request_path),
            "AVA_EXEC_RESULT_FILE": str(result_path),
            # The explicit endpoint opens the export gate; the enabled flag
            # answers the suite-wide disable (tests/conftest.py); the watchdog
            # margin keeps the watchdog out of the way.
            "AVA_TELEMETRY_OTLP_ENDPOINT": endpoint,
            "AVA_TELEMETRY_OTLP_ENABLED": "true",
            "AVA_EXEC_WATCHDOG_MARGIN_S": "0.5",
        }
    )
    env.pop("AVA_LOG_DIR", None)
    # Fixed argv, sys.executable is trusted.
    proc = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-m", "agent.exec_child"],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
    return proc, result_path


def _mirror_records(tmp_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((tmp_path / "home" / "logs").glob("events-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def _mirror_keys(tmp_path: Path) -> set[tuple[str, object, object]]:
    """The mirror's `(event_name, envelope, op)` keys — the same shape
    `_sent_keys` builds from the receiver, read from the JSONL mirror."""
    keys: set[tuple[str, object, object]] = set()
    for record in _mirror_records(tmp_path):
        attributes = cast("dict[str, Any]", record.get("attributes") or {})
        keys.add((str(record.get("event_name")), attributes.get("envelope"), attributes.get("op")))
    return keys


def _sent_keys() -> set[tuple[str, object, object]]:
    """Decode the receiver's OTLP/HTTP log batches into envelope keys.

    `(event_name, envelope, op)`: the OTLP attribute list carries the indexed
    dimensions (`event_name` here); the event payload rides the record body as
    the mirror-shape JSON (`shared/telemetry_otlp_logs`), so `envelope`/`op`
    come from `body["attributes"]`.
    """
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

    keys: set[tuple[str, object, object]] = set()
    with _POSTS_LOCK:
        posts = list(_POSTS)
    for path, raw in posts:
        if not path.endswith("/v1/logs") or len(raw) < 4:
            continue  # the reachability probe posts a 2-byte JSON body
        request = logs_service_pb2.ExportLogsServiceRequest()
        with contextlib.suppress(Exception):
            request.ParseFromString(raw)
        for resource_logs in request.resource_logs:
            for scope_logs in resource_logs.scope_logs:
                for record in scope_logs.log_records:
                    attrs: dict[str, Any] = {}
                    for kv in record.attributes:
                        if kv.value.HasField("string_value"):
                            attrs[kv.key] = kv.value.string_value
                        elif kv.value.HasField("int_value"):
                            attrs[kv.key] = kv.value.int_value
                    payload: dict[str, Any] = {}
                    if record.body.string_value:
                        with contextlib.suppress(Exception):
                            payload = json.loads(record.body.string_value).get("attributes") or {}
                    keys.add(
                        (
                            str(attrs.get("event_name")),
                            payload.get("envelope"),
                            payload.get("op"),
                        )
                    )
    return keys


def _poll_sent(expected: set[tuple[str, object, object]]) -> set[tuple[str, object, object]]:
    """The child exports synchronously before exiting; poll anyway so a slow
    handler thread can never race the assertions."""
    deadline = time.monotonic() + 10.0
    while True:
        keys = _sent_keys()
        if expected <= keys or time.monotonic() > deadline:
            return keys
        time.sleep(0.05)


def test_done_child_delivers_result_write_record(tmp_path: Path, otlp_receiver: Any) -> None:
    """The done child's final record (result/write) is exported before exit.

    Red before the fix (task #4312): the record reached only the JSONL mirror —
    the exit path cannot complete a deferred hold once the interpreter is
    finalizing — and Loki never saw a result/write event.
    """
    proc, result_path = _spawn(tmp_path, "print('delivery ran')", _endpoint(otlp_receiver))
    assert proc.returncode == 0, proc.stderr
    assert read_result(result_path).kind == "done"

    keys = _poll_sent(
        {
            ("exec_child_boot", None, None),
            ("exec_envelope", "request", "read"),
            ("exec_envelope", "result", "write"),
        }
    )
    # The pre-envelope records prove the receiver decoded real traffic.
    assert ("exec_child_boot", None, None) in keys, f"receiver saw: {sorted(keys)}"
    assert ("exec_envelope", "request", "read") in keys, f"receiver saw: {sorted(keys)}"
    assert ("exec_envelope", "result", "write") in keys, f"receiver saw: {sorted(keys)}"

    # The mirror carries the same record — the local channel never lost it.
    mirror_keys = _mirror_keys(tmp_path)
    assert ("exec_envelope", "result", "write") in mirror_keys, f"mirror saw: {sorted(mirror_keys)}"


def test_crashed_child_delivers_its_records(tmp_path: Path, otlp_receiver: Any) -> None:
    """A user-code crash still ships the child's records (task #4312).

    Before the fix the crashed kind skipped the child's finalize entirely, so
    the deferred hold died with the process and zero child-side records
    reached Loki for crashed execs.
    """
    proc, result_path = _spawn(
        tmp_path, "raise ValueError('delivery crash')", _endpoint(otlp_receiver)
    )
    assert proc.returncode == 0, proc.stderr
    assert read_result(result_path).kind == "crashed"

    keys = _poll_sent({("exec_envelope", "result", "write")})
    assert ("exec_envelope", "result", "write") in keys, f"receiver saw: {sorted(keys)}"


def test_boot_crash_child_delivers_the_crash_envelope_record(
    tmp_path: Path, otlp_receiver: Any
) -> None:
    """main()'s boot-crash envelope gets the same last-mile delivery."""
    proc, result_path = _spawn(
        tmp_path, "print('never runs')", _endpoint(otlp_receiver), corrupt_request=True
    )
    assert proc.returncode == 0, proc.stderr
    payload = read_result(result_path)
    assert payload.kind == "crashed"
    assert payload.code_reached is False  # boot crash: the code never ran

    keys = _poll_sent({("exec_envelope", "result", "write")})
    assert ("exec_envelope", "result", "write") in keys, f"receiver saw: {sorted(keys)}"
