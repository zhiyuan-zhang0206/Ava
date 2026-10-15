from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from ops.cluster.rpc import ClusterOpFailed, dispatch_to_machine


class _OpsStub:
    def __init__(self, *, status: str = "completed", fail_first: int = 0) -> None:
        self.received: list[dict[str, object]] = []
        self._status = status
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
            def log_message(self, format: str, *args: object) -> None:
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
    db_conn: psycopg.Connection, ops_stub: _OpsStub, *, database_gate: ProcessDbGate
) -> None:
    _register(db_conn, "runner-x", ops_stub.url)

    result = await dispatch_to_machine(
        Database.from_settings(gate=database_gate), "runner-x", "status_probe", {"foo": "bar"}
    )

    assert ops_stub.received == [{"kind": "status_probe", "payload": {"foo": "bar"}}]
    assert result == {"echo": {"kind": "status_probe", "payload": {"foo": "bar"}}, "ran": True}


async def test_dispatch_failed_status_raises_cluster_op_failed(
    db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    with _OpsStub(status="failed") as stub:
        _register(db_conn, "runner-y", stub.url)
        with pytest.raises(ClusterOpFailed):
            await dispatch_to_machine(
                Database.from_settings(gate=database_gate), "runner-y", "status_probe", {}
            )


async def test_dispatch_retries_transient_503_over_real_wire(
    db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    with _OpsStub(fail_first=1) as stub:
        _register(db_conn, "runner-z", stub.url)
        result = await dispatch_to_machine(
            Database.from_settings(gate=database_gate),
            "runner-z",
            "status_probe",
            {"ping": 1},
            retries=2,
        )

    assert result == {
        "echo": {"kind": "status_probe", "payload": {"ping": 1}},
        "ran": True,
    }
    assert len(stub.received) == 2
