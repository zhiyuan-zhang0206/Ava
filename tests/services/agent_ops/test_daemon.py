"""Unit tests for services/agent_ops/daemon.py — the agent-runner ops server.

Covers:
- _dispatch routing for each op kind (kind, payload) -> (status, result)
- wire-error proxying (AvaAgentError -> failed result carrying reason)
- _ops_route: body parsing, {status, result} envelope, malformed-body 400,
  semaphore-uninitialized guard
- concurrency cap (Semaphore) across concurrent /ops requests
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime
from pathlib import Path

import psycopg
import pytest

from services.agent_ops import daemon, health
from shared.deploy_timing import NO_PROGRESS_TIMEOUT_S

_REPO = Path(__file__).resolve().parents[3]


def _stub_pool() -> object:
    """Minimal stand-in for ConnectionPool used by _db_pool — ops are mocked
    so the pool's actual API is never exercised."""
    return object()


def test_ops_components_degrade_after_no_progress_bound_plus_margin() -> None:
    """The health response degrades 5 minutes after rollout progress stops."""
    now = 10_000.0
    wedge_after_s = NO_PROGRESS_TIMEOUT_S + 300.0
    still_safe = health.ops_components(
        now - wedge_after_s,
        {"cluster_fetch": ("cluster_fetch", now - wedge_after_s)},
        now=now,
    )
    active_ops = {"cluster_fetch": ("cluster_fetch", now - wedge_after_s - 2)}

    wedged = health.ops_components(
        now - wedge_after_s - 1,
        active_ops,
        now=now,
    )

    assert [record["status"] for record in still_safe] == ["ok", "ok", "ok"]
    assert wedged == [
        {"name": "loop", "status": "ok", "progress": "serving /ops"},
        {
            "name": "update-lock",
            "status": "degraded",
            "progress": f"held {wedge_after_s + 1:.0f}s",
            "detail": f"held for {wedge_after_s + 1:.0f}s",
        },
        {
            "name": "ops",
            "status": "degraded",
            "progress": "1 active",
            "detail": f"cluster_fetch running for {wedge_after_s + 2:.0f}s",
        },
    ]
    assert health.saturation(active_ops, 4) == 0.25


def test_ops_components_report_free_and_no_active_workers() -> None:
    components = health.ops_components(None, {})

    assert components[1] == {"name": "update-lock", "status": "ok", "progress": "free"}
    assert components[2] == {"name": "ops", "status": "ok", "progress": "0 active"}


# ─── _dispatch routing ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_spawn_launch_calls_launch_agent_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spawn-launch kind -> ops.launch_agent_op."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    captured: dict[str, object] = {}

    from ops.rpc_schemas import SpawnedAgent

    async def _fake_launch(body, pool):  # type: ignore[no-untyped-def]
        captured["agent_id"] = body.agent_id  # pyright: ignore[reportUnknownMemberType]
        captured["pool"] = pool
        return SpawnedAgent(id=777)

    monkeypatch.setattr(daemon.ops_lifecycle, "launch_agent_op", _fake_launch)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch("spawn-launch", {"agent_id": 777})
    assert status == "completed"
    assert result == {"id": 777}
    assert captured["agent_id"] == 777


@pytest.mark.asyncio
async def test_dispatch_cluster_update_forwards_restart_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cluster_update with restart_only payload -> ops.cluster_update_op(restart_only=True)."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    seen: dict[str, bool] = {}

    def _fake_update(
        *,
        restart_only: bool = False,
        target_sha: str | None = None,
        mode: str = "smooth",
        force_reap: bool = False,
    ) -> dict[str, str]:
        seen["restart_only"] = restart_only
        return {"session": "ava-updater", "log": "/x"}

    monkeypatch.setattr(daemon.ops_cluster, "cluster_update_op", _fake_update)
    status, _ = await daemon._dispatch("cluster_update", {"restart_only": True})
    assert status == "completed"
    assert seen["restart_only"] is True


@pytest.mark.asyncio
async def test_dispatch_cluster_update_defaults_to_full_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cluster_update with empty payload -> ops.cluster_update_op(restart_only=False)."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    seen: dict[str, bool] = {}

    def _fake_update(
        *,
        restart_only: bool = False,
        target_sha: str | None = None,
        mode: str = "smooth",
        force_reap: bool = False,
    ) -> dict[str, str]:
        seen["restart_only"] = restart_only
        return {"session": "ava-updater", "log": "/x"}

    monkeypatch.setattr(daemon.ops_cluster, "cluster_update_op", _fake_update)
    await daemon._dispatch("cluster_update", {})
    assert seen["restart_only"] is False


@pytest.mark.asyncio
async def test_dispatch_cluster_update_forwards_target_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cluster_update payload.target_sha -> ops.cluster_update_op(target_sha=...) so the
    host force-checks-out the pinned rollout commit."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    seen: dict[str, object] = {}

    def _fake_update(
        *,
        restart_only: bool = False,
        target_sha: str | None = None,
        mode: str = "smooth",
        force_reap: bool = False,
    ) -> dict[str, str]:
        seen["target_sha"] = target_sha
        return {"session": "ava-updater", "log": "/x"}

    monkeypatch.setattr(daemon.ops_cluster, "cluster_update_op", _fake_update)
    status, _ = await daemon._dispatch("cluster_update", {"target_sha": "PINNEDSHA"})
    assert status == "completed"
    assert seen["target_sha"] == "PINNEDSHA"


@pytest.mark.asyncio
async def test_dispatch_cluster_update_rejects_non_str_target_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed target_sha (not a str) fails the op rather than silently coercing."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    monkeypatch.setattr(
        daemon.ops_cluster,
        "cluster_update_op",
        lambda **_k: pytest.fail("must not dispatch on bad payload"),  # pyright: ignore[reportUnknownArgumentType]
    )
    status, result = await daemon._dispatch("cluster_update", {"target_sha": 123})
    assert status == "failed"
    # ClusterUpdatePayload validation rejects a non-str target_sha (caught, failed).
    assert "target_sha" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_routes_cluster_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """cluster_resume kind -> ops.cluster_resume_op (compensating unpause)."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    seen: list[tuple[str, datetime]] = []

    def _resume(holder: str, acquired: datetime) -> dict[str, bool]:
        seen.append((holder, acquired))
        return {"resumed": True}

    monkeypatch.setattr(
        daemon.ops_cluster,
        "cluster_resume_op",
        _resume,
    )
    status, result = await daemon._dispatch(
        "cluster_resume",
        {"deploy_holder": "g:pid1", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
    )
    assert status == "completed"
    assert result == {"resumed": True}
    assert seen[0][0] == "g:pid1"


@pytest.mark.asyncio
async def test_first_adoption_empty_resume_uses_only_the_legacy_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old in-memory sender can resume a host it just updated to new code."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    called: list[bool] = []
    monkeypatch.setattr(
        daemon.ops_cluster,
        "cluster_resume_legacy_op",
        lambda: called.append(True) or {"resumed": True},
    )

    def _refuse_exact(_holder: str, _acquired: datetime) -> None:
        pytest.fail("empty legacy payload cannot enter exact resume")

    monkeypatch.setattr(daemon.ops_cluster, "cluster_resume_op", _refuse_exact)

    status, result = await daemon._dispatch("cluster_resume", {})
    assert status == "completed"
    assert result == {"resumed": True}
    assert called == [True]


@pytest.mark.asyncio
async def test_dispatch_shell_probe_calls_shell_probe_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_probe kind -> ops_cluster.shell_probe_op(agent_id), serialized."""
    from ops.rpc_schemas import ShellInfo, ShellProbeResult

    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    seen: dict[str, object] = {}

    def _fake_probe(agent_id: int) -> ShellProbeResult:
        seen["agent_id"] = agent_id
        return ShellProbeResult(shells=[ShellInfo(id=5, name="build", uptime_seconds=42)])

    monkeypatch.setattr(daemon.ops_cluster, "shell_probe_op", _fake_probe)
    status, result = await daemon._dispatch("shell_probe", {"agent_id": 42})
    assert status == "completed"
    assert seen == {"agent_id": 42}
    assert result == {
        "shells": [
            {
                "id": 5,
                "name": "build",
                "created_at": None,
                "uptime_seconds": 42,
                "expires_at": None,
            }
        ]
    }


@pytest.mark.asyncio
async def test_dispatch_shell_probe_bad_payload_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_probe without agent_id fails without invoking the op."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    monkeypatch.setattr(
        daemon.ops_cluster,
        "shell_probe_op",
        lambda *_a, **_kw: pytest.fail("must not dispatch on bad payload"),  # pyright: ignore[reportUnknownArgumentType]
    )
    status, result = await daemon._dispatch("shell_probe", {})
    assert status == "failed"
    assert "agent_id" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_shell_kill_calls_shell_kill_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_kill resolves and kills one host-local persistent session."""
    from ops.rpc_schemas import ShellKillResult

    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    seen: dict[str, int] = {}

    def _fake_kill(agent_id: int, session_id: int) -> ShellKillResult:
        seen.update(agent_id=agent_id, session_id=session_id)
        return ShellKillResult(mode="killed")

    monkeypatch.setattr(daemon.ops_cluster, "shell_kill_op", _fake_kill)
    status, result = await daemon._dispatch("shell_kill", {"agent_id": 42, "session_id": 5})
    assert status == "completed"
    assert seen == {"agent_id": 42, "session_id": 5}
    assert result == {"mode": "killed", "interrupted": False, "name": None}


@pytest.mark.asyncio
async def test_dispatch_shell_kill_reports_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell already gone is a successful, idempotent absent result."""
    from ops.rpc_schemas import ShellKillResult

    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    monkeypatch.setattr(
        daemon.ops_cluster,
        "shell_kill_op",
        lambda _agent_id, _session_id: ShellKillResult(mode="absent"),  # pyright: ignore[reportUnknownArgumentType]
    )
    status, result = await daemon._dispatch("shell_kill", {"agent_id": 42, "session_id": 999})
    assert status == "completed"
    assert result == {"mode": "absent", "interrupted": False, "name": None}


@pytest.mark.asyncio
async def test_dispatch_agent_skill_view_calls_machine_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent_skill_view kind -> ops_cluster.agent_skill_view_op(agent_id, pool)."""
    from ops.rpc_schemas import AgentSkillViewResult, OpsCommandItem

    pool = _stub_pool()
    monkeypatch.setattr(daemon, "_db_pool", pool)
    seen: dict[str, object] = {}

    def _fake_view(agent_id: int, received_pool: object) -> AgentSkillViewResult:
        seen["agent_id"] = agent_id
        seen["pool"] = received_pool
        return AgentSkillViewResult(
            commands=[OpsCommandItem(name="project", description="d", instruction_hint="h")]
        )

    monkeypatch.setattr(daemon.ops_cluster, "agent_skill_view_op", _fake_view)
    status, result = await daemon._dispatch("agent_skill_view", {"agent_id": 42})
    assert status == "completed"
    assert seen == {"agent_id": 42, "pool": pool}
    assert result == {
        "commands": [{"name": "project", "description": "d", "instruction_hint": "h"}],
        "mcp_names": [],
    }


@pytest.mark.asyncio
async def test_dispatch_agent_skill_view_bad_payload_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent_skill_view without an id is rejected before it reaches the op."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    monkeypatch.setattr(
        daemon.ops_cluster,
        "agent_skill_view_op",
        lambda *_a, **_kw: pytest.fail("must not dispatch on bad payload"),  # pyright: ignore[reportUnknownArgumentType]
    )
    status, result = await daemon._dispatch("agent_skill_view", {})
    assert status == "failed"
    assert "agent_id" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_shell_capture_calls_shell_capture_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_capture kind -> ops_cluster.shell_capture_op(agent_id, session_id, lines)."""
    from ops.rpc_schemas import ShellCaptureResult

    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    seen: dict[str, object] = {}

    def _fake_capture(agent_id: int, session_id: int, lines: int = 200) -> ShellCaptureResult:
        seen["agent_id"] = agent_id
        seen["session_id"] = session_id
        seen["lines"] = lines
        return ShellCaptureResult(session_name="ava-agent-42-shell-3-build", lines=["a", "b"])

    monkeypatch.setattr(daemon.ops_cluster, "shell_capture_op", _fake_capture)
    status, result = await daemon._dispatch(
        "shell_capture", {"agent_id": 42, "session_id": 3, "lines": 500}
    )
    assert status == "completed"
    assert seen == {"agent_id": 42, "session_id": 3, "lines": 500}
    assert result == {
        "session_name": "ava-agent-42-shell-3-build",
        "lines": ["a", "b"],
        "created_at": None,
        "uptime_seconds": 0,
    }


@pytest.mark.asyncio
async def test_dispatch_shell_capture_defaults_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_capture without lines defaults to 200."""
    from ops.rpc_schemas import ShellCaptureResult

    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    seen: dict[str, object] = {}

    def _fake_capture(agent_id: int, session_id: int, lines: int = 200) -> ShellCaptureResult:
        seen["lines"] = lines
        return ShellCaptureResult(session_name="n", lines=[])

    monkeypatch.setattr(daemon.ops_cluster, "shell_capture_op", _fake_capture)
    status, _ = await daemon._dispatch("shell_capture", {"agent_id": 1, "session_id": 2})
    assert status == "completed"
    assert seen == {"lines": 200}


@pytest.mark.asyncio
async def test_dispatch_upload_receive_calls_upload_receive_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upload_receive kind -> ops_uploads.upload_receive_op(payload)."""
    from ops.rpc_schemas import UploadReceiveResult

    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    seen: dict[str, object] = {}

    def _fake_receive(payload):
        seen["agent_id"] = payload.agent_id  # pyright: ignore[reportUnknownMemberType]
        seen["name"] = payload.name  # pyright: ignore[reportUnknownMemberType]
        return UploadReceiveResult(
            path=f"/home/runner/Downloads/AvaAgent-{payload.agent_id}/{payload.name}"  # pyright: ignore[reportUnknownMemberType]
        )

    monkeypatch.setattr(daemon.ops_uploads, "upload_receive_op", _fake_receive)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch(
        "upload_receive", {"agent_id": 42, "name": "report.pdf"}
    )
    assert status == "completed"
    assert seen == {"agent_id": 42, "name": "report.pdf"}
    assert result == {"path": "/home/runner/Downloads/AvaAgent-42/report.pdf"}


@pytest.mark.asyncio
async def test_dispatch_upload_receive_bad_payload_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upload_receive with a malformed payload -> failed (not a crash)."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    status, result = await daemon._dispatch("upload_receive", {"agent_id": "not-an-int"})
    assert status == "failed"
    assert "error" in result


@pytest.mark.asyncio
async def test_dispatch_lifecycle_calls_lifecycle_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """lifecycle kind -> ops.lifecycle_op with parsed path."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    captured: dict[str, object] = {}

    async def _fake_lifecycle(  # type: ignore[no-untyped-def]
        path, body, pool, *, trigger_inbound_id=None, trigger_inbound_kind=None
    ):
        from ops.rpc_schemas import TerminateAgentResponse

        captured["path"] = path
        captured["body"] = body
        captured["trigger_inbound_id"] = trigger_inbound_id
        captured["trigger_inbound_kind"] = trigger_inbound_kind
        return TerminateAgentResponse(status="enqueued")

    monkeypatch.setattr(daemon.ops_lifecycle, "lifecycle_op", _fake_lifecycle)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch(
        "lifecycle",
        {
            "path": "/api/agents/42/resurrect-if-pending-work-v2",
            "body": {"resurrected_by": "system"},
            "trigger_inbound_id": 123,
            "trigger_inbound_kind": "chat",
        },
    )
    assert status == "completed"
    # _dispatch serializes the lifecycle response model to a JSON dict for the wire.
    assert result == {"status": "enqueued"}
    assert captured["path"] == "/api/agents/42/resurrect-if-pending-work-v2"
    assert captured["body"] == {"resurrected_by": "system"}
    assert captured["trigger_inbound_id"] == 123
    assert captured["trigger_inbound_kind"] == "chat"


@pytest.mark.asyncio
async def test_dispatch_lifecycle_missing_path_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """lifecycle payload without 'path' returns failed without invoking ops."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())

    async def _should_not_be_called(*_a, **_kw):  # type: ignore[no-untyped-def]
        raise AssertionError("lifecycle_op should not be invoked on missing path")

    monkeypatch.setattr(daemon.ops_lifecycle, "lifecycle_op", _should_not_be_called)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch("lifecycle", {})
    assert status == "failed"
    # LifecyclePayload validation rejects a missing 'path' before lifecycle_op runs.
    assert "path" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_unknown_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown kind returns failed; routing table is exhaustive."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    status, result = await daemon._dispatch("bogus_kind", {})
    assert status == "failed"
    assert "unknown kind" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_unparseable_lifecycle_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """ops.lifecycle_op raising ValueError lands as failed result, not a crash."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())

    async def _raises(  # type: ignore[no-untyped-def]
        path, body, pool, *, trigger_inbound_id=None, trigger_inbound_kind=None
    ):
        raise ValueError(f"lifecycle path not recognized: {path!r}")

    monkeypatch.setattr(daemon.ops_lifecycle, "lifecycle_op", _raises)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch("lifecycle", {"path": "/garbage", "body": {}})
    assert status == "failed"
    assert "not recognized" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_shell_capture_shell_not_found_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_capture_op raising ShellNotFoundError (the session died) lands as a
    plain failed result, not a crash for _ops_route's catch-all to log."""
    from ops.cluster_status import ShellNotFoundError

    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())

    def _raises(agent_id: int, session_id: int, lines: int = 200) -> object:
        raise ShellNotFoundError(f"agent {agent_id} has no live shell {session_id} on this host")

    monkeypatch.setattr(daemon.ops_cluster, "shell_capture_op", _raises)
    status, result = await daemon._dispatch("shell_capture", {"agent_id": 42, "session_id": 9})
    assert status == "failed"
    assert "ShellNotFoundError" in str(result["error"])
    assert "no live shell" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_wire_error_carries_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """AvaAgentError raised by an op is converted to a failed result with reason field
    so the gateway's _raise_proxied_wire_error_from_payload can re-emit."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())

    from shared.agents import AgentNotFound

    async def _raises(  # type: ignore[no-untyped-def]
        path, body, pool, *, trigger_inbound_id=None, trigger_inbound_kind=None
    ):
        raise AgentNotFound("agent 999 does not exist")

    monkeypatch.setattr(daemon.ops_lifecycle, "lifecycle_op", _raises)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch(
        "lifecycle", {"path": "/api/agents/999/terminate", "body": {}}
    )
    assert status == "failed"
    assert "AgentNotFound" in str(result["error"])
    assert result.get("reason") == "agent_not_found"


@pytest.mark.asyncio
async def test_dispatch_no_db_pool_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """_db_pool unset (e.g. test bypasses _main) returns failed, not an attribute error."""
    monkeypatch.setattr(daemon, "_db_pool", None)
    status, result = await daemon._dispatch("status_probe", {})
    assert status == "failed"
    assert "_db_pool" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_status_probe_passes_the_daemon_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The steady-state probe reuses the daemon's already-open central DB pool."""
    from ops.cluster import ClusterStatus

    pool = _stub_pool()
    seen: list[object] = []
    monkeypatch.setattr(daemon, "_db_pool", pool)

    def _status(probe_pool: object) -> ClusterStatus:
        seen.append(probe_pool)
        return ClusterStatus(
            machine_name="win",
            serve_gateway=False,
            serve_agent_runner=True,
            paused=False,
        )

    monkeypatch.setattr(daemon.ops_cluster, "cluster_status_op", _status)

    status, result = await daemon._dispatch("status_probe", {})

    assert status == "completed"
    assert result["machine_name"] == "win"
    assert seen == [pool]


# ─── _ops_route (the POST /ops handler) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_ops_route_wraps_dispatch_in_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid body returns 200 with {status, result} from _dispatch."""
    daemon._dispatch_sem = asyncio.Semaphore(4)

    async def _fake_dispatch(kind, payload):  # type: ignore[no-untyped-def]
        assert kind == "status_probe"
        return "completed", {"paused": False}

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
    status, body, ctype = await daemon._ops_route(json.dumps({"kind": "status_probe"}).encode())
    assert status == 200
    assert ctype == "application/json"
    assert json.loads(body) == {"status": "completed", "result": {"paused": False}}
    daemon._dispatch_sem = None


@pytest.mark.asyncio
async def test_ops_route_status_probe_serializes_datetime_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status_probe through the real _dispatch must survive _ops_route's json.dumps
    even when ClusterStatus carries datetime values nested inside it.

    Regression: a python-mode model_dump left datetime objects in the result and
    every status_probe crashed with 'Object of type datetime is not JSON
    serializable', so the gateway's status page lost all agent-runner info.
    The live datetime rides in `agent_groups` (typed dict[str, object]) so the
    test keeps guarding the wire serialization even though the current
    `_group_agent_sessions` pre-serializes its shells to JSON-mode dicts.
    """
    from datetime import UTC, datetime

    from ops.cluster import ClusterStatus

    daemon._dispatch_sem = asyncio.Semaphore(1)
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    created = datetime(2026, 6, 11, 8, 30, 0, tzinfo=UTC)

    def _status(probe_pool: object) -> ClusterStatus:
        assert probe_pool is daemon._db_pool
        return ClusterStatus(
            machine_name="runner-1",
            serve_gateway=False,
            serve_agent_runner=True,
            paused=False,
            agent_count=1,
            session_count=1,
            agent_groups=[
                {
                    "agent_id": 7,
                    "label": "",
                    "shells": [
                        {"name": "ava-agent-7", "created_at": created, "uptime_seconds": 120}
                    ],
                }
            ],
        )

    monkeypatch.setattr(daemon.ops_cluster, "cluster_status_op", _status)
    status, body, ctype = await daemon._ops_route(json.dumps({"kind": "status_probe"}).encode())
    assert status == 200
    assert ctype == "application/json"
    parsed = json.loads(body)
    assert parsed["status"] == "completed"
    shell = parsed["result"]["agent_groups"][0]["shells"][0]
    assert shell["name"] == "ava-agent-7"
    assert shell["created_at"] == "2026-06-11T08:30:00Z"
    daemon._dispatch_sem = None


@pytest.mark.asyncio
async def test_ops_route_completes_with_a_db_down_degraded_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB-down, including an unreachable paused row, remains HTTP 200 completed."""
    from ops.cluster import ClusterStatus

    pool = _stub_pool()
    daemon._dispatch_sem = asyncio.Semaphore(1)
    monkeypatch.setattr(daemon, "_db_pool", pool)

    def _degraded_status(probe_pool: object) -> ClusterStatus:
        assert probe_pool is pool
        return ClusterStatus(
            machine_name="win",
            serve_gateway=False,
            serve_agent_runner=True,
            paused=False,
            current_orchestration=None,
            last_updater_outcome=None,
            resource=None,
        )

    monkeypatch.setattr(daemon.ops_cluster, "cluster_status_op", _degraded_status)

    status, body, ctype = await daemon._ops_route(json.dumps({"kind": "status_probe"}).encode())

    assert status == 200
    assert ctype == "application/json"
    parsed = json.loads(body)
    assert parsed["status"] == "completed"
    assert parsed["result"]["paused"] is False
    assert parsed["result"]["current_orchestration"] is None
    assert parsed["result"]["last_updater_outcome"] is None
    assert parsed["result"]["resource"] is None
    daemon._dispatch_sem = None


@pytest.mark.asyncio
async def test_ops_route_non_json_values_degrade_to_str(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-JSON-native value in a dispatch result degrades to its str() instead
    of raising in _ops_route's json.dumps and 500-ing the /ops control plane.

    The Pydantic arms dump with mode="json"; default=str on the final dumps is
    the last-resort fallback for plain-dict results and any future op.
    """
    from datetime import UTC, datetime

    daemon._dispatch_sem = asyncio.Semaphore(1)

    async def _fake_dispatch(kind, payload):  # type: ignore[no-untyped-def]
        return "completed", {"at": datetime(2026, 6, 11, 8, 30, 0, tzinfo=UTC)}

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
    status, body, ctype = await daemon._ops_route(json.dumps({"kind": "status_probe"}).encode())
    assert status == 200
    assert ctype == "application/json"
    parsed = json.loads(body)
    assert parsed["status"] == "completed"
    assert parsed["result"]["at"] == "2026-06-11 08:30:00+00:00"
    daemon._dispatch_sem = None


@pytest.mark.asyncio
async def test_ops_route_failed_status_is_still_http_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 'failed' dispatch is a semantic outcome the gateway re-raises, not an
    HTTP error — the envelope carries it at HTTP 200."""
    daemon._dispatch_sem = asyncio.Semaphore(1)

    async def _fake_dispatch(kind, payload):  # type: ignore[no-untyped-def]
        return "failed", {"error": "boom"}

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
    status, body, _ = await daemon._ops_route(
        json.dumps({"kind": "spawn-launch", "payload": {"agent_id": 1}}).encode()
    )
    assert status == 200
    assert json.loads(body) == {"status": "failed", "result": {"error": "boom"}}
    daemon._dispatch_sem = None


@pytest.mark.asyncio
async def test_ops_route_malformed_body_400() -> None:
    """Non-JSON body and a body missing `kind` both return 400 without dispatching."""
    daemon._dispatch_sem = asyncio.Semaphore(1)
    status, body, _ = await daemon._ops_route(b"not json")
    assert status == 400
    assert "invalid JSON" in json.loads(body)["error"]
    status, body, _ = await daemon._ops_route(json.dumps({"payload": {}}).encode())
    assert status == 400
    assert "kind" in json.loads(body)["error"]
    daemon._dispatch_sem = None


@pytest.mark.asyncio
async def test_ops_route_crash_becomes_failed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash inside _dispatch is caught and returned as a failed result (HTTP 200),
    never leaks as a 500 the gateway can't interpret."""
    daemon._dispatch_sem = asyncio.Semaphore(1)

    async def _boom(kind, payload):  # type: ignore[no-untyped-def]
        raise RuntimeError("kaboom")

    monkeypatch.setattr(daemon, "_dispatch", _boom)  # pyright: ignore[reportUnknownArgumentType]
    status, body, _ = await daemon._ops_route(
        json.dumps({"kind": "spawn-launch", "payload": {"agent_id": 1}}).encode()
    )
    assert status == 200
    parsed = json.loads(body)
    assert parsed["status"] == "failed"
    assert "kaboom" in parsed["result"]["error"]
    daemon._dispatch_sem = None


@pytest.mark.asyncio
async def test_ops_route_raises_when_sem_uninitialized() -> None:
    """Calling _ops_route before _main initializes the semaphore raises RuntimeError."""
    daemon._dispatch_sem = None
    with pytest.raises(RuntimeError, match="_dispatch_sem not initialized"):
        await daemon._ops_route(json.dumps({"kind": "spawn-launch"}).encode())


@pytest.mark.asyncio
async def test_ops_route_semaphore_caps_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent /ops requests run at most ops_concurrency dispatches in parallel."""
    cap = 3
    daemon._dispatch_sem = asyncio.Semaphore(cap)
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def _fake_dispatch(kind, payload):  # type: ignore[no-untyped-def]
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.05)
        finally:
            async with lock:
                in_flight -= 1
        return "completed", {}

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
    bodies = [
        json.dumps({"kind": "spawn-launch", "payload": {"agent_id": i}}).encode() for i in range(20)
    ]
    results = await asyncio.gather(*[daemon._ops_route(b) for b in bodies])
    assert peak <= cap, f"peak concurrency {peak} exceeded cap {cap}"
    assert all(status == 200 for status, _, _ in results)
    daemon._dispatch_sem = None


# ─── main top-level crash handling ─────────────────────────────────────────────


def test_main_logs_and_exits_nonzero_on_an_uncaught_crash(tmp_path: Path) -> None:
    """A crash escaping `_main` still reaches the log with its traceback and still
    leaves a non-zero code for the supervisor.

    Driven in a subprocess because `main` now ends in `_hard_exit` and never
    returns — the price of skipping the interpreter teardown that a wedged arm
    hangs in (see `_hard_exit`). The contract it used to keep by re-raising is the
    same one asserted here, just observed from outside: logged, and rc != 0."""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(_REPO)!r})
        from services.agent_ops import daemon

        async def _boom():
            raise RuntimeError("db pool exploded mid-loop")

        daemon.init_gateway_process = lambda **kw: None
        daemon.install_graceful_shutdown = lambda *a, **kw: None
        daemon._main = _boom
        daemon.main()
    """)
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )

    assert done.returncode != 0
    combined = done.stdout + done.stderr
    assert "crashed" in combined and "db pool exploded" in combined


def test_ops_bind_host_all_interfaces_with_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a cluster secret, /ops binds 0.0.0.0 — no single-vs-multi-host branch.
    The gateway dials it over the network (or its own loopback self-dial), and the
    surface is always authenticated with the cluster-secret bearer, so reachability
    is not trust."""
    monkeypatch.setattr(daemon.settings.data_plane, "cluster_secret", "s3cret")
    assert daemon._ops_bind_host() == "0.0.0.0"  # noqa: S104


def test_ops_bind_host_loopback_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """A no-secret cluster serves /ops unauthenticated, so it must bind loopback
    only — an unauthenticated control surface is never LAN-reachable."""
    monkeypatch.setattr(daemon.settings.data_plane, "cluster_secret", "")
    assert daemon._ops_bind_host() == "127.0.0.1"


def test_ops_auth_token_is_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a secret, the token /ops requires is the cluster secret — regardless
    of bind posture (the gateway presents it on the loopback self-dial too)."""
    monkeypatch.setattr(daemon.settings.data_plane, "cluster_secret", "s3cret")
    assert daemon._ops_auth_token() == "s3cret"


def test_ops_auth_token_is_none_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-secret cluster has no token to require: /ops serves unauthenticated
    (on loopback — see `_ops_bind_host`)."""
    monkeypatch.setattr(daemon.settings.data_plane, "cluster_secret", "")
    assert daemon._ops_auth_token() is None


# ─── boot self-registration ────────────────────────────────────────────────────


def test_register_boot_announces_this_unit_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon registers its OWN unit once it is serving — the same
    `register_self` write `ava start` makes, so the stop latch is cleared and
    `up_since_at` restamped by the process whose liveness the row stands for.

    The URL comes from the shared `unit_dial_url()` with this unit's capability
    set, so the daemon cannot advertise a different address than `ava start` did.
    """
    from shared.machine import reset_identity, set_identity

    calls: list[str | None] = []
    monkeypatch.setattr("shared.machines.register_self", lambda *, url=None: calls.append(url))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.2")
    monkeypatch.setattr(
        "shared.daemon_health.health_port",
        lambda name: 8600 if name == "ops" else 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    set_identity(name="wsl", role="agent-runner")
    try:
        daemon._register_boot()
    finally:
        reset_identity()

    assert calls == ["http://10.0.0.2:8600"]


def test_register_boot_failure_does_not_stop_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed registration refresh leaves dispatch entirely correct, so it is
    logged and swallowed. Exiting here would hand the watchdog a respawn loop and
    take the host dark for the gateway — the outage this call exists to prevent.
    """
    from shared.machine import reset_identity, set_identity

    def _boom(*, url: str | None = None) -> None:
        raise RuntimeError("central postgres unreachable")

    monkeypatch.setattr("shared.machines.register_self", _boom)
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.2")
    monkeypatch.setattr(
        "shared.daemon_health.health_port",
        lambda name: 8600 if name == "ops" else 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    logged: list[str] = []
    monkeypatch.setattr(daemon._log, "exception", lambda msg, *_a, **_k: logged.append(msg))  # pyright: ignore[reportUnknownArgumentType]

    set_identity(name="wsl", role="agent-runner")
    try:
        daemon._register_boot()  # must not raise
    finally:
        reset_identity()

    assert logged and "boot registration failed" in logged[0]


def test_register_boot_unstops_a_host_that_came_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this call fixes, end to end against the real tables.

    A host that announced `ava stop` carries a `stopped_at` latch that only a
    `register_self` clears. When it comes back some other way — an OS-scheduled
    autostart, a watchdog respawn, a rollout's restart leg — nothing used to clear
    it, so the roster reported the host stopped while its ops daemon served every
    op, and the update fan-out (which filters on that same latch) dropped it.
    Registering at the daemon's own boot reconciles both.
    """
    import psycopg

    from shared import machines
    from shared.config import settings
    from shared.machine import reset_identity, set_identity

    monkeypatch.setattr("shared.machines.ava_home", lambda: "~/.ava")
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.9")
    monkeypatch.setattr(
        "shared.daemon_health.health_port",
        lambda name: 8600 if name == "ops" else 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
        cur.execute("TRUNCATE machine_units")
        conn.commit()

    set_identity(name="came-back", role="agent-runner")
    try:
        machines.register_self(url="http://10.0.0.9:8600")
        machines.mark_stopping("came-back", "~/.ava")
        assert machines.list_agent_runners() == []  # dropped from the fan-out
        assert machines.list_stopped_agent_runners() == [("came-back", "http://10.0.0.9:8600")]

        daemon._register_boot()  # the daemon comes up on its own

        assert machines.list_agent_runners() == [("came-back", "http://10.0.0.9:8600")]
        assert machines.list_stopped_agent_runners() == []
    finally:
        reset_identity()


# ─── idempotency-key dedup (Task #961) ────────────────────────────────────────


@pytest.fixture
def ops_pool() -> object:
    """A real ConnectionPool on the session test DB (the dedup path writes the
    shared `api_idempotency` table, method='ops' rows; `_stub_pool` is a
    non-DB stand-in and cannot serve it)."""
    from psycopg_pool import ConnectionPool

    from shared.config import settings

    pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield pool
    finally:
        pool.close()


def _fake_spawn_factory(calls: dict[str, int]) -> object:
    """A launch_agent_op stand-in that counts executions and returns id 777."""
    from ops.rpc_schemas import SpawnedAgent

    async def _fake_spawn(body, pool):  # type: ignore[no-untyped-def]
        calls["n"] = calls.get("n", 0) + 1
        return SpawnedAgent(id=777)

    return _fake_spawn


@pytest.mark.asyncio
async def test_idempotent_dispatch_first_run_executes_and_stores(
    monkeypatch: pytest.MonkeyPatch, ops_pool: object
) -> None:
    """The first dispatch with a key executes the op and stores its outcome in
    the shared api_idempotency table (method='ops' rows: path=kind,
    op_status + result)."""
    monkeypatch.setattr(daemon, "_db_pool", ops_pool)
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.ops_lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    status, result = await daemon._dispatch_idempotent(
        "spawn-launch",
        {"agent_id": 777},
        "key-1",
        ops_pool,  # type: ignore[arg-type]
    )

    assert status == "completed"
    assert result == {"id": 777}
    assert calls["n"] == 1
    with ops_pool.connection() as conn, conn.cursor() as cur:  # type: ignore[union-attr]
        cur.execute(  # pyright: ignore[reportUnknownMemberType]
            "SELECT path, op_status, response_body FROM api_idempotency "
            "WHERE key = %s AND method = 'ops'",
            ("key-1",),
        )
        row = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
    assert row == ("spawn-launch", "completed", {"id": 777})


@pytest.mark.asyncio
async def test_idempotent_dispatch_replays_without_reexecuting(
    monkeypatch: pytest.MonkeyPatch, ops_pool: object
) -> None:
    """A second dispatch with the same key replays the stored outcome — the op
    is NOT re-executed. This is what makes the gateway's retry of a non-
    idempotent op (spawn) safe: a lost response cannot create a twin agent."""
    monkeypatch.setattr(daemon, "_db_pool", ops_pool)
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.ops_lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    first = await daemon._dispatch_idempotent(
        "spawn-launch",
        {"agent_id": 777},
        "key-2",
        ops_pool,  # type: ignore[arg-type]
    )
    second = await daemon._dispatch_idempotent(
        "spawn-launch",
        {"agent_id": 777},
        "key-2",
        ops_pool,  # type: ignore[arg-type]
    )

    assert first == ("completed", {"id": 777})
    assert second == ("completed", {"id": 777})
    assert calls["n"] == 1  # executed exactly once across both dispatches


@pytest.mark.asyncio
async def test_idempotent_dispatch_same_key_waits_for_slow_running_owner(
    monkeypatch: pytest.MonkeyPatch, ops_pool: object
) -> None:
    """A cluster update retry waits for its still-running owner and replays it."""
    monkeypatch.setattr(daemon, "_db_pool", ops_pool)
    monkeypatch.setattr(daemon, "_DEDUP_WAIT_STEP_S", 0.01)
    monkeypatch.setattr(daemon, "_DEDUP_WAIT_ATTEMPTS", 2)
    calls: dict[str, int] = {}
    started = asyncio.Event()

    async def _slow_dispatch(
        kind: str, payload: dict[str, object]
    ) -> tuple[str, dict[str, object]]:
        calls["n"] = calls.get("n", 0) + 1
        started.set()
        await asyncio.sleep(0.3)
        return "completed", {"session": "ava-updater", "log": "/x"}

    monkeypatch.setattr(daemon, "_dispatch", _slow_dispatch)
    owner = asyncio.create_task(
        daemon._dispatch_idempotent("cluster_update", {}, "slow-cluster-update", ops_pool)  # type: ignore[arg-type]
    )
    await started.wait()
    duplicate = await daemon._dispatch_idempotent(
        "cluster_update",
        {},
        "slow-cluster-update",
        ops_pool,  # type: ignore[arg-type]
    )
    first = await owner

    assert duplicate == first == ("completed", {"session": "ava-updater", "log": "/x"})
    assert calls == {"n": 1}


@pytest.mark.asyncio
async def test_idempotent_dispatch_waiter_fails_after_expected_duration(
    monkeypatch: pytest.MonkeyPatch, ops_pool: object
) -> None:
    """An overdue cluster-update owner fails loudly instead of claiming completion."""
    monkeypatch.setattr(daemon, "_db_pool", ops_pool)
    monkeypatch.setattr(daemon, "_DEDUP_WAIT_STEP_S", 0.01)
    monkeypatch.setitem(daemon._DEDUP_EXPECTED_DURATION_S, "cluster_update", 0.02)
    calls: dict[str, int] = {}
    started = asyncio.Event()

    async def _slow_dispatch(
        kind: str, payload: dict[str, object]
    ) -> tuple[str, dict[str, object]]:
        calls["n"] = calls.get("n", 0) + 1
        started.set()
        await asyncio.sleep(0.3)
        return "completed", {"session": "ava-updater", "log": "/x"}

    monkeypatch.setattr(daemon, "_dispatch", _slow_dispatch)
    owner = asyncio.create_task(
        daemon._dispatch_idempotent("cluster_update", {}, "stuck-cluster-update", ops_pool)  # type: ignore[arg-type]
    )
    await started.wait()
    status, result = await daemon._dispatch_idempotent(
        "cluster_update",
        {},
        "stuck-cluster-update",
        ops_pool,  # type: ignore[arg-type]
    )
    await owner

    assert status == "failed"
    error = str(result["error"])
    assert "cluster_update" in error
    assert "running for" in error
    assert "stuck" in error
    assert "never completed" not in error
    assert calls == {"n": 1}


@pytest.mark.asyncio
async def test_idempotent_dispatch_distinct_keys_execute_twice(
    monkeypatch: pytest.MonkeyPatch, ops_pool: object
) -> None:
    """Different keys are different logical ops — each executes."""
    monkeypatch.setattr(daemon, "_db_pool", ops_pool)
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.ops_lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    await daemon._dispatch_idempotent(
        "spawn-launch",
        {"agent_id": 777},
        "key-a",
        ops_pool,  # type: ignore[arg-type]
    )
    await daemon._dispatch_idempotent(
        "spawn-launch",
        {"agent_id": 777},
        "key-b",
        ops_pool,  # type: ignore[arg-type]
    )

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_idempotent_dispatch_failed_outcome_is_stored_and_replayed(
    monkeypatch: pytest.MonkeyPatch, ops_pool: object
) -> None:
    """A business-failed outcome is stored like a success and replayed on a
    same-key retry — a deterministic business failure must not re-run the op."""
    monkeypatch.setattr(daemon, "_db_pool", ops_pool)

    async def _fake_lifecycle(  # type: ignore[no-untyped-def]
        path, body, pool, *, trigger_inbound_id=None, trigger_inbound_kind=None
    ):
        raise ValueError("unparseable lifecycle path")

    monkeypatch.setattr(daemon.ops_lifecycle, "lifecycle_op", _fake_lifecycle)  # pyright: ignore[reportUnknownArgumentType]

    first = await daemon._dispatch_idempotent(
        "lifecycle",
        {"path": "garbage"},
        "key-3",
        ops_pool,  # type: ignore[arg-type]
    )
    second = await daemon._dispatch_idempotent(
        "lifecycle",
        {"path": "garbage"},
        "key-3",
        ops_pool,  # type: ignore[arg-type]
    )

    assert first[0] == "failed"
    assert second == first  # replayed, not re-executed


@pytest.mark.asyncio
async def test_ops_route_dedupes_by_envelope_key(
    monkeypatch: pytest.MonkeyPatch, ops_pool: object
) -> None:
    """End-to-end through _ops_route: an envelope carrying idempotency_key goes
    through the dedup path — two identical POSTs execute the op once."""
    monkeypatch.setattr(daemon, "_db_pool", ops_pool)
    monkeypatch.setattr(daemon, "_dispatch_sem", asyncio.Semaphore(4))
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.ops_lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    body = json.dumps(
        {"kind": "spawn-launch", "payload": {"agent_id": 1}, "idempotency_key": "route-key"}
    ).encode()

    code1, payload1, _ = await daemon._ops_route(body)
    code2, payload2, _ = await daemon._ops_route(body)

    assert code1 == 200 and code2 == 200
    assert json.loads(payload1) == {"status": "completed", "result": {"id": 777}}
    assert json.loads(payload2) == {"status": "completed", "result": {"id": 777}}
    assert calls["n"] == 1
    daemon._dispatch_sem = None


@pytest.mark.asyncio
async def test_ops_route_without_key_does_not_dedupe(
    monkeypatch: pytest.MonkeyPatch, ops_pool: object
) -> None:
    """An envelope WITHOUT idempotency_key takes the plain _dispatch path — no
    dedup row is written (idempotent ops have nothing to dedupe)."""
    monkeypatch.setattr(daemon, "_db_pool", ops_pool)
    monkeypatch.setattr(daemon, "_dispatch_sem", asyncio.Semaphore(4))
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.ops_lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    body = json.dumps({"kind": "spawn-launch", "payload": {"agent_id": 1}}).encode()
    await daemon._ops_route(body)
    await daemon._ops_route(body)

    assert calls["n"] == 2  # no key → no dedup → both execute
    with ops_pool.connection() as conn, conn.cursor() as cur:  # type: ignore[union-attr]
        cur.execute("SELECT count(*) FROM api_idempotency WHERE method = 'ops'")  # pyright: ignore[reportUnknownMemberType]
        assert cur.fetchone()[0] == 0  # pyright: ignore[reportUnknownMemberType]
    daemon._dispatch_sem = None


# ─── _dispatch_idempotent retry on closed connection (Task #1059) ──────────────


@pytest.mark.asyncio
async def test_dispatch_idempotent_retries_operational_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass that dies on a closed connection is re-run; the outcome is returned."""
    calls: list[int] = []

    async def _flaky_pass(kind, payload, key, pool):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) < 3:
            raise psycopg.OperationalError("the connection is closed")
        return "completed", {"ok": True}

    monkeypatch.setattr(daemon, "_dispatch_idempotent_pass", _flaky_pass)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(daemon, "_sleep", _noop_sleep)
    status, result = await daemon._dispatch_idempotent(
        "spawn-launch",
        {"agent_id": 1},
        "key-1",
        _stub_pool(),  # type: ignore[arg-type]
    )
    assert status == "completed"
    assert result == {"ok": True}
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_dispatch_idempotent_gives_up_after_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistently dying connection raises after the bounded attempts."""
    calls: list[int] = []

    async def _always_dead(kind, payload, key, pool):  # type: ignore[no-untyped-def]
        calls.append(1)
        raise psycopg.OperationalError("the connection is closed")

    monkeypatch.setattr(daemon, "_dispatch_idempotent_pass", _always_dead)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(daemon, "_sleep", _noop_sleep)
    with pytest.raises(psycopg.OperationalError):
        await daemon._dispatch_idempotent("spawn-launch", {"agent_id": 1}, "key-2", _stub_pool())  # type: ignore[arg-type]
    assert len(calls) == daemon._DISPATCH_RETRY_ATTEMPTS


@pytest.mark.asyncio
async def test_dispatch_idempotent_propagates_non_operational_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any non-OperationalError propagates on the first pass — no retry."""
    calls: list[int] = []

    async def _boom(kind, payload, key, pool):  # type: ignore[no-untyped-def]
        calls.append(1)
        raise ValueError("not a connection problem")

    monkeypatch.setattr(daemon, "_dispatch_idempotent_pass", _boom)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(daemon, "_sleep", _noop_sleep)
    with pytest.raises(ValueError):
        await daemon._dispatch_idempotent("spawn-launch", {"agent_id": 1}, "key-3", _stub_pool())  # type: ignore[arg-type]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dispatch_idempotent_no_pool_fails_cleanly() -> None:
    """Without a pool the wrapper returns failed without retrying (the pass
    would crash on `pool.connection()`)."""
    status, result = await daemon._dispatch_idempotent(
        "spawn-launch", {"agent_id": 1}, "key-4", None
    )
    assert status == "failed"
    assert isinstance(result["error"], str)
    assert "not initialized" in result["error"]


async def _noop_sleep(_seconds: float) -> None:
    """Stand-in for daemon._sleep in retry tests — no real backoff wait."""


# ─── blocking ops run off the event loop ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a_blocking_op_does_not_freeze_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-08-12 incident in one assertion. A `cluster_update` on the Windows
    runner stopped returning inside its spawn; because the arm ran inline on the
    loop it took the whole daemon with it — 2 h 03 m with not one line logged, every
    controller stopped, and the stranded-pause self-heal that exists for exactly
    that situation unable to run.

    The op here blocks until the test releases it. What must stay true is that the
    loop keeps turning meanwhile: other coroutines run, other ops dispatch, and the
    health endpoint's `await` still gets its turn."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    started = threading.Event()
    release = threading.Event()

    def _wedged(**_kw: object) -> dict[str, str]:
        started.set()
        release.wait(timeout=30)
        return {"session": "ava-updater", "log": "x"}

    monkeypatch.setattr(daemon.ops_cluster, "cluster_update_op", _wedged)

    task = asyncio.ensure_future(daemon._dispatch("cluster_update", {}))
    await asyncio.to_thread(started.wait, 10)

    # The loop is still ours: this only completes if nothing is holding it.
    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0)
        ticks += 1
    assert ticks == 5, "the event loop stopped turning while an op was blocked"

    release.set()
    status, _result = await task
    assert status == "completed"


@pytest.mark.asyncio
async def test_a_second_cluster_update_is_refused_not_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running the ops off the loop removes the serialization they used to get for
    free: `ops_concurrency` is 8, so two `cluster_update` POSTs could now interleave
    through `spawn_update`'s check-then-spawn window — the shape that tore the
    schtasks XML on win (2026-08-11, #1181).

    Refused rather than queued: a caller that waits behind a stuck update learns
    nothing for as long as it is stuck, and `ClusterUpdateInProgress` is a verdict
    its callers already handle."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def _wedged(**_kw: object) -> dict[str, str]:
        calls.append(1)
        started.set()
        release.wait(timeout=30)
        return {"session": "ava-updater", "log": "x"}

    monkeypatch.setattr(daemon.ops_cluster, "cluster_update_op", _wedged)

    first = asyncio.ensure_future(daemon._dispatch("cluster_update", {}))
    await asyncio.to_thread(started.wait, 10)

    status, result = await daemon._dispatch("cluster_update", {})
    assert status == "failed"
    assert "ClusterUpdateInProgress" in str(result["error"])

    release.set()
    assert (await first)[0] == "completed"
    assert len(calls) == 1, "the refused dispatch must not have executed the op"


@pytest.mark.asyncio
async def test_an_unrelated_op_still_dispatches_while_an_update_is_stuck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property the whole change is for, and the reason the lock covers only
    `cluster_update`: the compensating `cluster_resume` the gateway sends — and the
    host's own stranded-pause self-heal — must be able to land while an update is
    wedged. Under the old inline dispatch they could not, which is why win sat
    paused for the full two hours."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    started = threading.Event()
    release = threading.Event()

    def _wedged(**_kw: object) -> dict[str, str]:
        started.set()
        release.wait(timeout=30)
        return {"session": "ava-updater", "log": "x"}

    monkeypatch.setattr(daemon.ops_cluster, "cluster_update_op", _wedged)

    def _resume(_holder: str, _acquired: datetime) -> dict[str, bool]:
        return {"resumed": True}

    monkeypatch.setattr(daemon.ops_cluster, "cluster_resume_op", _resume)

    stuck = asyncio.ensure_future(daemon._dispatch("cluster_update", {}))
    await asyncio.to_thread(started.wait, 10)

    status, result = await daemon._dispatch(
        "cluster_resume",
        {"deploy_holder": "g:pid1", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
    )
    assert (status, result) == ("completed", {"resumed": True})

    release.set()
    await stuck


@pytest.mark.asyncio
async def test_two_config_writes_cannot_interleave(monkeypatch: pytest.MonkeyPatch) -> None:
    """`config_write` is a read-modify-write: `write_fields` walks the requested
    keys through dotenv's `set_key`, rewriting the whole `.env` once per key. The
    event loop used to serialize that for free; in a worker thread with
    `ops_concurrency`=8 two of them can interleave, and the later writer lands
    carrying the earlier one's snapshot — the earlier one's fields gone, silently,
    in the only on-disk copy of a cluster's secrets.

    Asserted as non-overlap rather than on a file, so it pins the guarantee (these
    two never run at once) instead of one writer's implementation."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    inside = 0
    overlapped = False

    class _Result:
        def model_dump(self, **_kw: object) -> dict[str, object]:
            return {"ok": True}

    def _slow_write(*_a: object, **_kw: object) -> _Result:
        nonlocal inside, overlapped
        inside += 1
        if inside > 1:
            overlapped = True
        time.sleep(0.05)
        inside -= 1
        return _Result()

    monkeypatch.setattr(daemon.ops_config, "config_write_op", _slow_write)
    monkeypatch.setattr(daemon.ops_inventory, "inventory_write_op", _slow_write)

    await asyncio.gather(
        daemon._dispatch("config_write", {"overrides": {}}),
        daemon._dispatch("config_write", {"overrides": {}}),
        daemon._dispatch("inventory_write", {"plugins": {}, "mcp_servers": {}}),
    )

    assert not overlapped, "two state writes ran concurrently — .env can lose fields"


@pytest.mark.asyncio
async def test_a_refused_update_says_how_long_the_holder_has_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal is logged as well as answered: a growing run of these is the only
    signal that a worker has wedged while the rest of the host looks healthy."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    started = threading.Event()
    release = threading.Event()

    def _wedged(**_kw: object) -> dict[str, str]:
        started.set()
        release.wait(timeout=30)
        return {"session": "ava-updater", "log": "x"}

    monkeypatch.setattr(daemon.ops_cluster, "cluster_update_op", _wedged)

    first = asyncio.ensure_future(daemon._dispatch("cluster_update", {}))
    await asyncio.to_thread(started.wait, 10)

    with caplog.at_level(logging.WARNING, logger="services.agent_ops.daemon"):
        _status, result = await daemon._dispatch("cluster_update", {})

    assert "refusing a concurrent cluster_update" in caplog.text
    assert "refused after" in str(result["detail"])
    # The wire-error enum belongs to AvaAgentError proxying; this is a dispatch
    # verdict and must not claim to be one.
    assert "reason" not in result

    release.set()
    await first


@pytest.mark.asyncio
async def test_op_arms_do_not_run_on_the_default_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`asyncio.run` closes by JOINING every default-executor thread, so an arm
    wedged there holds the interpreter's exit after everything else has cleaned up —
    bounded at `THREAD_JOIN_TIMEOUT` (300 s) on 3.12, which is still twenty times the
    supervisor's graceful window. Owning the pool is what decouples the two.

    Asserted on the thread's name rather than on a shutdown timing, because the
    name is the property that both keeps the arm off the default executor AND makes
    the stuck thread findable in a dump — which is what the refusal runbook says to
    go looking for."""
    monkeypatch.setattr(daemon, "_db_pool", _stub_pool())
    seen: list[str] = []

    def _note_thread() -> object:
        seen.append(threading.current_thread().name)

        class _R:
            def model_dump(self, **_kw: object) -> dict[str, object]:
                return {}

        return _R()

    monkeypatch.setattr(daemon.ops_config, "config_read_op", _note_thread)

    await daemon._dispatch("config_read", {})

    assert seen and seen[0].startswith("ava-ops-arm"), f"arm ran on {seen!r}"


def test_shutting_the_pool_down_does_not_wait_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exit path drops the pool with `wait=False`. A join here would reinstate
    exactly the stall the own-pool change removes."""
    calls: list[bool] = []

    class _Pool:
        def shutdown(self, wait: bool = True) -> None:
            calls.append(wait)

    monkeypatch.setattr(daemon, "_op_executor", _Pool())
    daemon._shutdown_op_pool()

    assert calls == [False]
    assert daemon._op_executor is None


def test_a_wedged_arm_does_not_hold_the_process_exit(tmp_path: Path) -> None:
    """The empirical one, and the only shape that can catch this.

    `shutdown(wait=False)` looks like it releases the daemon and does not:
    `concurrent.futures.thread._python_exit` — registered via
    `threading._register_atexit` — joins every worker still in `_threads_queues` with
    NO bound, and `wait=False` does not remove a running thread from that mapping;
    3.9+ also forces those workers non-daemon, so there is no way around it from the
    executor side. Measured before this fix: own pool + wedged worker +
    `shutdown(wait=False)` + `sys.exit(0)` was still alive minutes later.

    Nothing in-process can assert that: the failure IS the interpreter refusing to
    stop. So this drives the daemon's own `_op_thread_pool` / `_shutdown_op_pool` /
    `_hard_exit` in a real subprocess with a genuinely stuck arm, and asserts the
    process is gone. It fails by timing out against the pre-fix code.
    """
    ready = tmp_path / "wedged"
    script = textwrap.dedent(f"""
        import pathlib, sys, time
        sys.path.insert(0, {str(_REPO)!r})
        from services.agent_ops import daemon

        pool = daemon._op_thread_pool()
        pool.submit(lambda: (pathlib.Path({str(ready)!r}).write_text("1"), time.sleep(3600)))
        while not pathlib.Path({str(ready)!r}).exists():
            time.sleep(0.01)
        daemon._shutdown_op_pool()
        daemon._hard_exit(0)
    """)
    started = time.monotonic()
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,  # the pre-fix code never returns; the timeout IS the failure
        check=False,
    )
    elapsed = time.monotonic() - started

    assert done.returncode == 0, done.stderr[-2000:]
    # Generous: the point is "seconds, not never", not a latency budget.
    assert elapsed < 20, f"exit took {elapsed:.1f}s with an arm still wedged"


def test_the_exit_code_survives_the_hard_exit(tmp_path: Path) -> None:
    """`_hard_exit` replaced a `raise` on the crash path, so the code a supervisor
    reads has to still distinguish a crash from a clean stop."""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(_REPO)!r})
        from services.agent_ops import daemon
        daemon._hard_exit(1)
    """)
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 1
