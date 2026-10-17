from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import psutil
import pytest

from base.config import settings
from base.daemon.health import DaemonProbe
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.client import RootClient, native_identity
from base.paths import root_run_dir
from services.entrypoints.gate.tests.test_gate import FakeApp, FakeGateway, Servers, gate_request
from services.entrypoints.gate.tests.test_gate import servers as servers
from services.supervision.healthchecks import gate
from services.supervision.meiva_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.meiva_root.server import ControlServer
from services.supervision.meiva_root.supervisor import Supervisor, SupervisorConfig
from services.supervision.roster import build_services


def _gate_process(home: Path) -> OwnedProcess:
    response = RootClient(root_run_dir(home) / "meiva-root.sock", timeout=2.0).status()
    body = response.get("result")
    assert response.get("ok") is True and isinstance(body, dict)
    units = cast("list[object]", cast("dict[str, object]", body).get("units"))
    rows = [
        cast("dict[str, object]", item)
        for item in units
        if isinstance(item, dict) and cast("dict[str, object]", item).get("id") == "gate"
    ]
    assert len(rows) == 1 and rows[0].get("state") == "running"
    return native_identity(rows[0])


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
    run_dir = root_run_dir(short_tmp)
    spec = next(spec for spec in build_services() if spec.session == "gate")
    assert spec.identity_probe is not None
    assert spec.capabilities == frozenset({"gateway"})
    assert not spec.requires_db
    unit = UnitManifest(
        "gate",
        (sys.executable, "-m", "services.entrypoints.gate.daemon", "--port", str(port)),
        RestartPolicy.ALWAYS,
        "root",
        (("MEIVA_TELEMETRY_OTLP_ENABLED", "false"),),
    )
    owner = Supervisor(
        UnitRegistry([unit]), run_dir=run_dir, config=SupervisorConfig(stop_timeout_s=3)
    )
    server = ControlServer(run_dir / "meiva-root.sock", owner.dispatch)
    await server.start()
    try:
        await owner.start()
        await _ready(spec.identity_probe)
        original = await asyncio.to_thread(_gate_process, short_tmp)
        assert psutil.Process(original.pid).ppid() == psutil.Process().pid
        await owner.restart("gate")
        await _ready(spec.identity_probe)
        replacement = await asyncio.to_thread(_gate_process, short_tmp)
        assert replacement != original
        assert not original.live()
        await owner.down("gate")
        assert not replacement.live()
        assert not (run_dir / "custody").exists()
        result = await asyncio.to_thread(spec.identity_probe)
        assert result.verdict.value == "down", result.detail
        with socket.socket() as foreign:
            foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            foreign.bind(("127.0.0.1", port))
            foreign.listen()
            result = await asyncio.to_thread(spec.identity_probe)
            assert result.verdict.value == "down", result.detail
    finally:
        await owner.shutdown()
        await server.close()


def test_gate_protocol_rejects_another_homes_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    def response(*_args: object, **_kwargs: object) -> io.BytesIO:
        return io.BytesIO(b'{"name":"gate","home":"/foreign"}')

    monkeypatch.setattr(gate.urllib.request, "urlopen", response)
    assert gate.probe().verdict.value == "port-taken"


def test_gate_health_is_independent_of_auth_and_application(servers: Servers) -> None:
    import os

    from base.paths import meiva_home

    FakeGateway.down = True
    status, body, headers = gate_request(servers["gate"] + "/__meiva/healthz")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert json.loads(body) == {"name": "gate", "home": str(meiva_home()), "pid": os.getpid()}
    assert FakeGateway.requests == 0
    assert FakeApp.requests == 0


def test_gate_health_rejects_post(servers: Servers) -> None:
    status, _body, headers = gate_request(servers["gate"] + "/__meiva/healthz", method="POST")
    assert status == 405
    assert headers["Allow"] == "GET"
    assert FakeGateway.requests == 0
