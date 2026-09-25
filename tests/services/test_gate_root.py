"""Gate as a native root child, using only isolated pytest home and sockets.

This exercises Supervisor custody and the real Gate process. It is not a macOS
helper or full deployed-root acceptance proof.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from ops.roster import build_services
from services.ava_root.client import owned_process
from services.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.ava_root.server import ControlServer
from services.ava_root.supervisor import Supervisor, SupervisorConfig
from services.healthchecks import gate
from shared.config import settings
from shared.daemon_health import DaemonProbe
from tests.services.test_gate import _FakeApp, _FakeGateway, _request, _Servers
from tests.services.test_gate import servers as servers


async def _ready(probe: Callable[[], DaemonProbe]) -> DaemonProbe:
    deadline = time.monotonic() + 15
    result = DaemonProbe.down("no observation yet")
    while time.monotonic() < deadline:
        result = await asyncio.to_thread(probe)
        if result.alive:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError(f"Gate never became ready: {result.detail}")


async def test_gate_native_child_readiness_restart_and_stop(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    monkeypatch.setattr(settings.services, "frontend_healthcheck_url", f"http://127.0.0.1:{port}")
    monkeypatch.setattr("shared.paths.root_run_dir", lambda: short_tmp)
    spec = next(spec for spec in build_services() if spec.session == "gate")
    assert spec.identity_probe is not None
    assert spec.capabilities == frozenset({"gateway"})
    assert not spec.requires_db
    unit = UnitManifest(
        "gate",
        (sys.executable, "-m", "services.gate.daemon", "--port", str(port)),
        RestartPolicy.ALWAYS,
        "root",
        (("AVA_HOME_OVERRIDE", "1"), ("AVA_TELEMETRY_OTLP_ENABLED", "false")),
    )
    owner = Supervisor(
        UnitRegistry([unit]), run_dir=short_tmp, config=SupervisorConfig(stop_timeout_s=3)
    )
    server = ControlServer(short_tmp / "ava-root.sock", owner.dispatch)
    await server.start()
    try:
        await owner.start()
        await _ready(spec.identity_probe)
        original = await asyncio.to_thread(owned_process, "gate")
        assert original is not None
        assert psutil.Process(original.pid).ppid() == psutil.Process().pid
        await owner.restart("gate")
        await _ready(spec.identity_probe)
        replacement = await asyncio.to_thread(owned_process, "gate")
        assert replacement is not None and replacement != original
        assert not original.live()
        await owner.down("gate")
        assert not replacement.live()
        assert not list((short_tmp / "custody").iterdir())
        result = await asyncio.to_thread(spec.identity_probe)
        assert result.verdict.value == "down", result.detail
        # An unrelated listener on the same port cannot become this stopped unit.
        with socket.socket() as foreign:
            foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            foreign.bind(("127.0.0.1", port))
            foreign.listen()
            result = await asyncio.to_thread(spec.identity_probe)
            assert result.verdict.value == "port-taken", result.detail
    finally:
        await owner.shutdown()
        await server.close()


def test_gate_protocol_rejects_another_homes_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    def response(*_args: object, **_kwargs: object) -> io.BytesIO:
        return io.BytesIO(b'{"name":"gate","home":"/foreign"}')

    monkeypatch.setattr(gate.urllib.request, "urlopen", response)
    assert gate.probe().verdict.value == "port-taken"


def test_gate_health_is_independent_of_auth_and_application(servers: _Servers) -> None:
    import os

    from shared.paths import ava_home

    _FakeGateway.down = True
    status, body, headers = _request(servers["gate"] + "/__ava/healthz")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert json.loads(body) == {"name": "gate", "home": str(ava_home()), "pid": os.getpid()}
    assert _FakeGateway.requests == 0
    assert _FakeApp.requests == 0


def test_gate_health_rejects_post(servers: _Servers) -> None:
    status, _body, headers = _request(servers["gate"] + "/__ava/healthz", method="POST")
    assert status == 405
    assert headers["Allow"] == "GET"
    assert _FakeGateway.requests == 0
