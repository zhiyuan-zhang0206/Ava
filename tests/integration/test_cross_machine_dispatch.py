"""End-to-end cross-machine dispatch: a real registered machine row + a real
HTTP POST to a stub ops server.

`gateway/cluster_rpc.py:dispatch_to_machine` is the wire path the gateway uses to
reach a runner that lives on another host (HTTP-uniform spawn: the gateway POSTs
`{kind, payload}` to the runner's `{ops_url}/ops`). `tests/gateway/test_cluster_rpc.py`
covers its error classification but mock-patches `lookup_machine_url` and uses an
httpx MockTransport — so nothing there proves the URL it dials actually comes from
a `register_self`-written `machines` row, nor that a real socket round-trip works.

This is the split-deployment validation the multihost rig exercises manually: a
machine registers an ops URL, the gateway resolves it from the table and POSTs to
it for real. Here the "remote runner" is a local stub HTTP server on a free port.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest

from ops.cluster_rpc import ClusterOpFailed, dispatch_to_machine


class _OpsStub:
    """A minimal stand-in for a runner's ops daemon: serves POST /ops, captures
    the request body, and replies with a configurable {status, result} envelope."""

    def __init__(self, *, status: str = "completed", fail_first: int = 0) -> None:
        self.received: list[dict[str, object]] = []
        self._status = status
        # `fail_first` requests answer 503 before the stub starts completing —
        # the retry-loop shape: a transient server-side failure that a retry
        # heals over the real wire.
        self._fail_first = fail_first
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(
                self, format: str, *args: object
            ) -> None:  # match base signature; silence access log
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/ops":
                    stub.received.append(body)
                    if stub._fail_first > 0:
                        stub._fail_first -= 1
                        payload = b'{"status":"failed","result":{}}'
                        self.send_response(503)
                    else:
                        payload = json.dumps(
                            {"status": stub._status, "result": {"echo": body, "ran": True}}
                        ).encode()
                        self.send_response(200)
                else:
                    payload = b'{"status":"failed","result":{}}'
                    self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler

    def __enter__(self) -> _OpsStub:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def _register(conn: psycopg.Connection, name: str, ops_url: str) -> None:
    """Write a runner machine row the way register_self does (role TEXT[], the
    ops URL the gateway dials)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, role, gateway_url) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET role = EXCLUDED.role, "
            "gateway_url = EXCLUDED.gateway_url",
            (name, ["agent-runner"], ops_url),
        )
    conn.commit()


@pytest.fixture
def ops_stub() -> Iterator[_OpsStub]:
    with _OpsStub() as stub:
        yield stub


async def test_dispatch_resolves_registered_url_and_round_trips(
    db_conn: psycopg.Connection, ops_stub: _OpsStub
) -> None:
    """The real path: register a runner's ops URL, then dispatch_to_machine
    resolves it from the machines table and POSTs `{kind, payload}` to `/ops`,
    returning the runner's result."""
    _register(db_conn, "runner-x", ops_stub.url)

    result = await dispatch_to_machine("runner-x", "status_probe", {"foo": "bar"})

    assert ops_stub.received == [{"kind": "status_probe", "payload": {"foo": "bar"}}]
    assert result == {"echo": {"kind": "status_probe", "payload": {"foo": "bar"}}, "ran": True}


async def test_dispatch_failed_status_raises_cluster_op_failed(
    db_conn: psycopg.Connection,
) -> None:
    """A runner that ran the op but reports status=failed surfaces as
    ClusterOpFailed (the gateway re-raises the runner's error), over the real wire."""
    with _OpsStub(status="failed") as stub:
        _register(db_conn, "runner-y", stub.url)
        with pytest.raises(ClusterOpFailed):
            await dispatch_to_machine("runner-y", "status_probe", {})


async def test_dispatch_retries_transient_503_over_real_wire(
    db_conn: psycopg.Connection,
) -> None:
    """The retry loop over a real socket: an ops server that answers 503 on the
    first dial (server mid-restart) is re-dialed and the op completes on the
    retry — a transient failure no longer drops the op."""
    with _OpsStub(fail_first=1) as stub:
        _register(db_conn, "runner-z", stub.url)
        result = await dispatch_to_machine("runner-z", "status_probe", {"ping": 1}, retries=2)

    assert result == {
        "echo": {"kind": "status_probe", "payload": {"ping": 1}},
        "ran": True,
    }
    assert len(stub.received) == 2  # first dial 503'd, retry completed
