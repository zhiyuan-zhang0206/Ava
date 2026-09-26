"""Preview evidence must reject execution failures and detect daemonized survivors."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from scripts.preview import runtime


def test_configure_allows_the_direct_app_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dotenv import dotenv_values

    from ops import roster
    from shared import cluster

    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "run.json").write_text(json.dumps({"profile": {"AVA_OS_JOBS_ENABLED": "0"}}))
    record = cluster.ClusterRecord(
        ports=cast(cluster.ClusterPorts, {"gateway": 18054, "frontend": 18055, "app": 18069}),
        gateway_home=str(home),
        created_at="2026-09-26T00:00:00Z",
    )

    def get_record(_home: str) -> cluster.ClusterRecord:
        return record

    monkeypatch.setattr(cluster, "get_record", get_record)
    monkeypatch.setattr(roster, "build_services", list)
    runtime.configure(tmp_path, home)
    assert dotenv_values(home / ".env")["AVA_GATEWAY_CORS_ALLOWED_ORIGINS"] == (
        "http://127.0.0.1:18069,http://localhost:18069"
    )
    assert json.loads((tmp_path / "config.json").read_text())["frontend_url"] == (
        "http://127.0.0.1:18069"
    )


@pytest.mark.parametrize("fault", ["origin", "credentials", "authentication", None])
def test_browser_readiness_requires_cross_origin_auth(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import httpx

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
        with pytest.raises(RuntimeError, match="Unexpected process"):
            runtime.stop_incomplete_install(tmp_path)
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


def test_incomplete_install_stops_native_redis_with_no_registry(tmp_path: Path) -> None:
    import shutil
    import time

    redis = shutil.which("redis-server")
    if redis is None:
        pytest.skip("Native Redis binary is unavailable")
    data = tmp_path / "home/redis"
    data.mkdir(parents=True)
    socket = data / "redis.sock"
    with (tmp_path / "redis.log").open("w") as output:
        child = subprocess.Popen(  # noqa: S603 — local Redis, private dir/socket, no TCP port
            [redis, "--port", "0", "--unixsocket", socket.name, "--save", "", "--appendonly", "no"],
            cwd=data,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 5
            while not socket.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert socket.exists(), (tmp_path / "redis.log").read_text()
            runtime.stop_incomplete_install(tmp_path)
            assert child.wait(timeout=2) == 0
            assert not runtime.owned_processes(tmp_path)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
