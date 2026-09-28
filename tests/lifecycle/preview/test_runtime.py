"""Preview evidence must reject execution failures and detect daemonized survivors."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import psutil
import pytest

from scripts.preview import runtime


@pytest.mark.parametrize("fault", ["origin", "credentials", "authentication", None])
def test_browser_readiness_requires_cross_origin_auth(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    origin = "http://127.0.0.1:18069"
    headers = {
        "access-control-allow-origin": origin,
        "access-control-allow-credentials": "true",
    }
    if fault == "origin":
        headers["access-control-allow-origin"] = "http://127.0.0.1:18055"
    if fault == "credentials":
        del headers["access-control-allow-credentials"]

    def get(url: str, **kwargs: object) -> httpx.Response:
        assert url == "http://127.0.0.1:18054/api/auth/check"
        assert kwargs["headers"] == {"Origin": origin}
        return httpx.Response(
            200,
            headers=headers,
            json={"authenticated": fault != "authentication"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", get)
    if fault is None:
        runtime.check_browser_access("http://127.0.0.1:18054", origin)
    else:
        with pytest.raises(RuntimeError, match="Preview browser cannot authenticate"):
            runtime.check_browser_access("http://127.0.0.1:18054", origin)


def test_http_failure_retains_server_reason_without_retry() -> None:
    response = httpx.Response(
        503,
        json={"code": "cluster_updating", "retryable": True},
        request=httpx.Request("POST", "http://127.0.0.1:8000/api/agents"),
    )
    with pytest.raises(httpx.HTTPStatusError) as failure:
        runtime.require_success(response)
    assert failure.value.response is response
    assert "cluster_updating" in "".join(failure.value.__notes__)


@pytest.mark.parametrize("body", ["(no output)", "Traceback: error at line 3", "13", "3\nError"])
def test_timestamp_or_traceback_is_not_execution_success(body: str) -> None:
    items: list[runtime.TimelineItem] = [
        {"kind": "agent_code", "payload": "print(1 + 2)", "exec_ms": None},
        {"kind": "agent_chat", "payload": "done", "exec_ms": None},
        {
            "kind": "code_output",
            "exec_ms": 12,
            "payload": f"Code execution output [2026-09-25 13:00:00]:\n\n{body}",
        },
    ]
    assert not runtime.execution_completed(items, "done")
    items[-1]["payload"] = "Code execution output:\n\n3\n"
    assert runtime.execution_completed(items, "done")
    items[-1]["payload"] = "Code execution output [cancelled by user]:\n\n3\n"
    assert not runtime.execution_completed(items, "done")


def test_process_with_rewritten_argv_still_belongs_to_home(tmp_path: Path) -> None:
    data = tmp_path / "home/redis"
    data.mkdir(parents=True)
    # A child with no home in argv models daemonized Redis's rewritten title.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=data)
    try:
        assert child.pid in {p.pid for p in runtime.owned_processes(tmp_path)}
        assert child.pid not in {p.pid for p in runtime.owned_processes(tmp_path / "neighbor")}
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_recorded_real_execution_is_checked_exactly() -> None:
    # The envelope shape includes a timestamp, while exec_ms proves this is an
    # execution result item rather than the scripted model's final claim.
    output: runtime.TimelineItem = {
        "kind": "code_output",
        "payload": "Code execution output [13:03]:\n\n3\n",
        "exec_ms": 573,
    }
    items: list[runtime.TimelineItem] = [
        {"kind": "agent_code", "payload": "print(1 + 2)\n", "exec_ms": None},
        output,
        {"kind": "agent_chat", "payload": "done", "exec_ms": None},
    ]
    assert runtime.execution_completed(json.loads(json.dumps(items)), "done")
    output["exec_ms"] = None
    assert not runtime.execution_completed(items, "done")


def test_cleanup_observer_cannot_release_a_remaining_registry_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    registry = tmp_path / "clusters.json"
    original = json.dumps({str(home): {"gateway_home": str(home)}})
    registry.write_text(original)

    def absent(_run: Path) -> list[psutil.Process]:
        return []

    monkeypatch.setattr(runtime, "owned_processes", absent)

    with pytest.raises(RuntimeError, match="registry slot remains"):
        runtime.verify_stopped(tmp_path, home)

    assert registry.read_text() == original
    assert not (tmp_path / "cleanup.json").exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_destroy_observer_rejects_retained_checkout_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source"
    source.mkdir()
    pointer = source / ".ava_home"
    if symlink:
        pointer.symlink_to(tmp_path / "missing")
    else:
        pointer.write_text(str(home) + "\n")

    def absent(_run: Path) -> list[psutil.Process]:
        return []

    monkeypatch.setattr(runtime, "owned_processes", absent)
    with pytest.raises(RuntimeError, match="checkout binding remains"):
        runtime.verify_stopped(tmp_path, home)
    assert pointer.exists() or pointer.is_symlink()
    assert not (tmp_path / "cleanup.json").exists()


def test_readiness_observer_does_not_wait_for_a_late_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ops import roster
    from ops.service_spec import ServiceSpec
    from shared.daemon_health import DaemonProbe

    (tmp_path / "config.json").write_text(json.dumps({"frontend_url": "http://unused"}))
    sampled: list[str] = []

    def gateway_probe() -> DaemonProbe:
        sampled.append("gateway")
        if len(sampled) == 1:
            return DaemonProbe.down("not ready after start returned")
        return DaemonProbe.up("became ready later")

    def up() -> DaemonProbe:
        return DaemonProbe.up("ready")

    services = tuple(
        ServiceSpec(
            name, "", frozenset(), False, identity_probe=gateway_probe if name == "gateway" else up
        )
        for name in runtime.SERVICES
    )
    monkeypatch.setattr(roster, "build_services", lambda: services)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: pytest.fail("readiness retried"))
    with pytest.raises(RuntimeError, match="not ready after successful start"):
        runtime.check(tmp_path, tmp_path / "home")
    assert sampled == ["gateway"]
