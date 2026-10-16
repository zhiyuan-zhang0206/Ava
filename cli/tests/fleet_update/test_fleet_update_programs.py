from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER
from cli import fleet_update

_ROSTER = {
    "name": "runner-a",
    "online": True,
    "identity_mismatch": False,
    "head_sha": "target-sha",
    "running_sha": "target-sha",
    "serve_agent_runner": True,
}


@dataclass
class Gateway:
    url: str = ""
    secret: str = field(default_factory=lambda: uuid.uuid4().hex)
    calls: list[tuple[str, str, dict[str, str], object]] = field(
        default_factory=list[tuple[str, str, dict[str, str], object]]
    )

    def respond(self, method: str, path: str) -> tuple[int, object]:
        if (method, path) == ("GET", "/api/cluster/roster?fresh=true"):
            return 200, [{**_ROSTER, "not_in_public_summary": "discard"}]
        if (method, path) == ("POST", "/api/keyed/v1/agents"):
            return 201, {"id": 17}
        if (method, path) == ("GET", "/api/agents/17/timeline"):
            return 200, {
                "items": [
                    {"kind": "agent_code", "payload": "print(1 + 2)"},
                    {"kind": "code_output", "payload": "stdout\n\n3\n"},
                ]
            }
        if method == "POST" and path in {
            "/api/agents/17/messages",
            "/api/agents/17/terminate",
        }:
            return 200, {}
        return 404, {"error": "unexpected request"}


@pytest.fixture
def gateway() -> Iterator[Gateway]:
    state = Gateway()

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            headers = {key.lower(): value for key, value in self.headers.items()}
            state.calls.append((self.command, self.path, headers, body))
            if headers.get("authorization") != f"Bearer {state.secret}":
                status, response = 401, {"error": "wrong credential"}
            else:
                status, response = state.respond(self.command, self.path)
            raw = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            self.handle_request()

        def do_POST(self) -> None:
            self.handle_request()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


def run_program(tmp_path: Path, gateway: Gateway, *args: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        f"AVA_GATEWAY_URL={gateway.url}\nAVA_CLUSTER_SECRET={gateway.secret}\nAVA_TIMEZONE=UTC\n"
    )
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "AVA_HOME": str(home),
        "AVA_CONFIG_FETCH": "skip",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-m", "cli.commands.management.fleet_gateway", *args],
        cwd=Path(fleet_update.__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_roster_program_uses_current_configuration_and_gateway_api(
    tmp_path: Path, gateway: Gateway
) -> None:
    proc = run_program(tmp_path, gateway, "roster")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout) == [_ROSTER]
    assert len(gateway.calls) == 1
    method, path, headers, body = gateway.calls[0]
    assert (method, path, body) == ("GET", "/api/cluster/roster?fresh=true", None)
    assert headers["authorization"] == f"Bearer {gateway.secret}"
    assert gateway.secret not in proc.stdout + proc.stderr


def test_smoke_program_spawns_executes_and_terminates_with_current_gateway_api(
    tmp_path: Path, gateway: Gateway
) -> None:
    proc = run_program(tmp_path, gateway, "smoke", "runner-a", "10")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout) == {
        "completed": True,
        "spawn": 201,
        "agent": 17,
        "terminate": 200,
    }
    assert [(method, path) for method, path, _, _ in gateway.calls] == [
        ("POST", "/api/keyed/v1/agents"),
        ("POST", "/api/agents/17/messages"),
        ("GET", "/api/agents/17/timeline"),
        ("POST", "/api/agents/17/terminate"),
    ]
    assert all(
        headers["authorization"] == f"Bearer {gateway.secret}" for _, _, headers, _ in gateway.calls
    )
    _, _, spawn_headers, spawn_body = gateway.calls[0]
    assert spawn_body == {"machine": "runner-a"}
    assert spawn_headers[SCOPE_HEADER.lower()] == PRINCIPAL_SCOPE
    assert len(spawn_headers["idempotency-key"]) == 32
    assert gateway.calls[1][3] == {
        "content": "Update smoke test. Run exactly this Python code with your code tool, once: print(1 + 2)",
        "source": "user",
    }
    assert gateway.secret not in proc.stdout + proc.stderr


def test_gateway_transport_sends_the_executed_module_source() -> None:
    owner = Path(__file__).resolve().parents[2] / "commands" / "management" / "fleet_gateway.py"
    assert owner.read_text(encoding="utf-8") == fleet_update.gateway_program()
