"""Exit-seam delivery — a short-lived process's tail record reaches the OTLP
receiver (task #4320).

Under the SDK default (`shutdown_on_exit=True`) each provider registers its own
atexit shutdown at bring-up — mid-life, so later than
`shared.telemetry._drain_on_exit` (registered at import) — and atexit runs
LIFO: the provider shut down FIRST, and a record emitted inside the drain
thread's final batch window was flushed by the exit drain into a shut-down
processor (`force_flush` returns False) and stayed mirror-only (task #4314
triage: exec-child `result/write` = 0 fleet-wide in Loki while the mirror held
every line; hierarchy-job tail lines 166 → 0 mirror→Loki). The providers are
now built with `shutdown_on_exit=False` and `_drain_on_exit` is the single
ordered exit seam.

This test runs a real short-lived child against a minimal OTLP/HTTP receiver
recording every POST body, and reads both channels: the receiver's exported
batches and the child's JSONL mirror.
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
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

from shared.telemetry import Event, event_row

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


# The child: arm the pipeline, bring the OTLP providers up while alive
# (`sync()`), then emit the tail record and return — the exit drain is the
# only thing left that can carry it.
_CHILD = """
from shared import telemetry

MARKER = {marker!r}

telemetry.init_telemetry(process="exit-seam-probe")
telemetry.emit("log", "log", attributes={{"seq": "early", "marker": MARKER}})
telemetry.sync()
telemetry.emit("log", "log", attributes={{"seq": "tail", "marker": MARKER}})
"""


# The external CLI's `exec` command runs the caller's code inside this same
# attachment context. The lease and checkpoint dependencies are local fakes;
# the telemetry emitter, SDK wrapper, OTLP setup, and normal interpreter exit
# are real. `ava.files.read` is one tail sdk_call (under 0.5 s before exit).
_EXTERNAL_CLI_CHILD = """
from pathlib import Path
import ava
from ava import external
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

external.control.require_active = lambda _lease_id, _caller: dict(LEASE)
external.machine_name = lambda: "external-cli-probe"
external.process_metadata = lambda: {{"pid": 1}}
def load_snapshot(_agent_id):
    state = build_agent_state()()
    state.ava_code__cwd = str(Path(SAMPLE).parent)
    return state, {{}}, None

external.load_snapshot = load_snapshot

with external.attach("lease"):
    assert ava.files.read(SAMPLE) == "tail event"
"""


def _sent_pairs() -> set[tuple[object, object]]:
    """Decode the receiver's OTLP/HTTP log batches into (marker, seq) pairs.

    The record carries both as OTLP attributes and the event body is the
    mirror-shape JSON, so either channel identifies the record."""
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

    pairs: set[tuple[object, object]] = set()
    with _POSTS_LOCK:
        posts = list(_POSTS)
    for path, raw in posts:
        if not path.endswith("/v1/logs") or len(raw) < 4:
            continue  # the reachability probe posts a small JSON body
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
                    payload: dict[str, Any] = {}
                    if record.body.string_value:
                        with contextlib.suppress(Exception):
                            payload = json.loads(record.body.string_value).get("attributes") or {}
                    pairs.add((payload.get("marker", attrs.get("marker")), payload.get("seq")))
    return pairs


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
    """Check one exported SDK event preserves its mirror identity and borrower."""
    assert len(delivered) == 1, f"receiver saw: {delivered!r}"
    event = delivered[0]
    assert event["agent_id"] == 424242
    assert event["source"] == "agent:424242"
    assert event["attributes"]["fn"] == "files.read"
    mirrored = [row for row in mirror if row["event_name"] == "sdk_call"]
    assert len(mirrored) == 1
    event_for_id: dict[str, Any] = dict(event)
    event_for_id["ts"] = datetime.fromisoformat(event["ts"])
    assert mirrored[0]["id"] == event_row(Event(**event_for_id))["id"]


def test_tail_record_of_a_short_lived_process_reaches_the_receiver(
    tmp_path: Path, otlp_receiver: Any
) -> None:
    """The last record emitted before exit must ship while the providers are
    still alive — the exit seam, not the SDK's atexit order, carries it."""
    marker = uuid.uuid4().hex
    home = tmp_path / "home"
    env = os.environ.copy()
    env.update(
        {
            "AVA_HOME": str(home),
            "AVA_PROCESS_PROFILE": "agent",
            # The explicit endpoint + enabled flag open the export gate
            # (tests/conftest.py disables OTLP suite-wide).
            "AVA_TELEMETRY_OTLP_ENDPOINT": f"http://127.0.0.1:{otlp_receiver.server_address[1]}",
            "AVA_TELEMETRY_OTLP_ENABLED": "true",
        }
    )
    env.pop("AVA_LOG_DIR", None)
    # Fixed argv, sys.executable is trusted. -I keeps the parent's PYTHON*
    # environment out; `import shared` resolves through the checkout's
    # editable install.
    proc = subprocess.run(  # noqa: S603 — fixed argv: our own interpreter, inline program
        [sys.executable, "-I", "-X", "utf8", "-c", _CHILD.format(marker=marker)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    deadline = time.monotonic() + 10.0
    pairs: set[tuple[object, object]] = set()
    while time.monotonic() < deadline:
        pairs = _sent_pairs()
        if (marker, "tail") in pairs:
            break
        time.sleep(0.05)

    # The pre-tail record proves the receiver decoded real traffic.
    assert (marker, "early") in pairs, f"receiver saw: {sorted(pairs)!r}"
    # Red before the fix (task #4320): the exit flush landed on a shut-down
    # processor and only the JSONL mirror kept this record.
    assert (marker, "tail") in pairs, f"receiver saw: {sorted(pairs)!r}"

    # The mirror carries the same record — the local channel never lost it.
    rows = [
        json.loads(line)
        for path in sorted((home / "logs").glob("events-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    mirror_tail: list[Any] = []
    for row in rows:
        attributes = cast("dict[str, Any]", row.get("attributes") or {})
        if attributes.get("marker") == marker and attributes.get("seq") == "tail":
            mirror_tail.append(row)
    assert mirror_tail, "the JSONL mirror must hold the tail record"


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
