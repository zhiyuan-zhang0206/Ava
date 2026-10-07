"""Unit tests for services/agent_runner/agent_ops/daemon.py — the agent-runner ops server.

Covers:
- _dispatch routing for each op kind (kind, payload) -> (status, result)
- wire-error proxying (AvaAgentError -> failed result carrying reason)
- _ops_route: body parsing, {status, result} envelope, malformed-body 400,
  required semaphore binding
- concurrency cap (Semaphore) across concurrent /ops requests
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime
from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from base.agents import ShellKillMode, TerminateResult
from base.db import Database
from base.deploy.progress_timeout import NO_PROGRESS_TIMEOUT_S
from services.agent_runner.agent_ops import daemon, health

_db = Database.from_settings


_REPO = Path(__file__).resolve().parents[4]


def _stub_pool() -> ConnectionPool:
    """A closed real pool for mocked arms; no connection is borrowed."""
    return ConnectionPool(open=False)


def test_ops_components_degrade_after_no_progress_bound_plus_margin() -> None:
    """The health response degrades 5 minutes after rollout progress stops."""
    now = 10_000.0
    wedge_after_s = NO_PROGRESS_TIMEOUT_S + 300.0
    still_safe = health.ops_components(
        {"config_read": ("config_read", now - wedge_after_s)},
        now=now,
    )
    active_ops = {"config_read": ("config_read", now - wedge_after_s - 2)}

    wedged = health.ops_components(
        active_ops,
        now=now,
    )

    assert [record["status"] for record in still_safe] == ["ok", "ok"]
    assert wedged == [
        {"name": "loop", "status": "ok", "progress": "serving /ops"},
        {
            "name": "ops",
            "status": "degraded",
            "progress": "1 active",
            "detail": f"config_read running for {wedge_after_s + 2:.0f}s",
        },
    ]
    assert health.saturation(active_ops, 4) == 0.25


def test_ops_components_report_free_and_no_active_workers() -> None:
    components = health.ops_components({})

    assert components[1] == {"name": "ops", "status": "ok", "progress": "0 active"}


# ─── _dispatch routing ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_spawn_launch_calls_launch_agent_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spawn-launch kind -> ops.launch_agent_op."""
    dispatch_pool: ConnectionPool = _stub_pool()
    captured: dict[str, object] = {}

    from ops.rpc_schemas import SpawnedAgent

    async def _fake_launch(_db: object, _bus: object, body, pool):  # type: ignore[no-untyped-def]
        captured["agent_id"] = body.agent_id  # pyright: ignore[reportUnknownMemberType]
        captured["pool"] = pool
        return SpawnedAgent(id=777)

    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", _fake_launch)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch(
        "spawn-launch", {"agent_id": 777}, active_ops={}, workers=set(), pool=dispatch_pool
    )
    assert status == "completed"
    assert result == {"id": 777}
    assert captured["agent_id"] == 777


@pytest.mark.asyncio
async def test_dispatch_shell_probe_calls_shell_probe_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_probe kind -> cluster.shell_probe_op(agent_id), serialized."""
    from ops.rpc_schemas import ShellInfo, ShellProbeResult

    dispatch_pool: ConnectionPool = _stub_pool()
    seen: dict[str, object] = {}

    def _fake_probe(agent_id: int) -> ShellProbeResult:
        seen["agent_id"] = agent_id
        return ShellProbeResult(shells=[ShellInfo(id=5, name="build", uptime_seconds=42)])

    monkeypatch.setattr(daemon.cluster, "shell_probe_op", _fake_probe)
    status, result = await daemon._dispatch(
        "shell_probe", {"agent_id": 42}, active_ops={}, workers=set(), pool=dispatch_pool
    )
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
    dispatch_pool: ConnectionPool = _stub_pool()
    monkeypatch.setattr(
        daemon.cluster,
        "shell_probe_op",
        lambda *_a, **_kw: pytest.fail("must not dispatch on bad payload"),  # pyright: ignore[reportUnknownArgumentType]
    )
    status, result = await daemon._dispatch(
        "shell_probe", {}, active_ops={}, workers=set(), pool=dispatch_pool
    )
    assert status == "failed"
    assert "agent_id" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_shell_kill_calls_shell_kill_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_kill resolves and kills one host-local persistent session."""
    from ops.rpc_schemas import ShellKillResult

    dispatch_pool: ConnectionPool = _stub_pool()
    seen: dict[str, int] = {}

    def _fake_kill(agent_id: int, session_id: int) -> ShellKillResult:
        seen.update(agent_id=agent_id, session_id=session_id)
        return ShellKillResult(mode=ShellKillMode.KILLED)

    monkeypatch.setattr(daemon.cluster, "shell_kill_op", _fake_kill)
    status, result = await daemon._dispatch(
        "shell_kill",
        {"agent_id": 42, "session_id": 5},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
    )
    assert status == "completed"
    assert seen == {"agent_id": 42, "session_id": 5}
    assert result == {"mode": "killed", "interrupted": False, "name": None}


@pytest.mark.asyncio
async def test_dispatch_shell_kill_reports_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell already gone is a successful, idempotent absent result."""
    from ops.rpc_schemas import ShellKillResult

    dispatch_pool: ConnectionPool = _stub_pool()
    monkeypatch.setattr(
        daemon.cluster,
        "shell_kill_op",
        lambda _agent_id, _session_id: ShellKillResult(mode=ShellKillMode.ABSENT),  # pyright: ignore[reportUnknownArgumentType]
    )
    status, result = await daemon._dispatch(
        "shell_kill",
        {"agent_id": 42, "session_id": 999},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
    )
    assert status == "completed"
    assert result == {"mode": "absent", "interrupted": False, "name": None}


@pytest.mark.asyncio
async def test_dispatch_agent_skill_view_calls_machine_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent_skill_view kind -> cluster.agent_skill_view_op(agent_id, pool)."""
    from ops.rpc_schemas import AgentSkillViewResult, OpsCommandItem

    pool = _stub_pool()
    dispatch_pool: ConnectionPool = pool
    seen: dict[str, object] = {}

    def _fake_view(agent_id: int, received_pool: object) -> AgentSkillViewResult:
        seen["agent_id"] = agent_id
        seen["pool"] = received_pool
        return AgentSkillViewResult(
            commands=[OpsCommandItem(name="project", description="d", instruction_hint="h")]
        )

    monkeypatch.setattr(daemon.cluster, "agent_skill_view_op", _fake_view)
    status, result = await daemon._dispatch(
        "agent_skill_view", {"agent_id": 42}, active_ops={}, workers=set(), pool=dispatch_pool
    )
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
    dispatch_pool: ConnectionPool = _stub_pool()
    monkeypatch.setattr(
        daemon.cluster,
        "agent_skill_view_op",
        lambda *_a, **_kw: pytest.fail("must not dispatch on bad payload"),  # pyright: ignore[reportUnknownArgumentType]
    )
    status, result = await daemon._dispatch(
        "agent_skill_view", {}, active_ops={}, workers=set(), pool=dispatch_pool
    )
    assert status == "failed"
    assert "agent_id" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_shell_capture_calls_shell_capture_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_capture kind -> cluster.shell_capture_op(agent_id, session_id, lines)."""
    from ops.rpc_schemas import ShellCaptureResult

    dispatch_pool: ConnectionPool = _stub_pool()
    seen: dict[str, object] = {}

    def _fake_capture(agent_id: int, session_id: int, lines: int = 200) -> ShellCaptureResult:
        seen["agent_id"] = agent_id
        seen["session_id"] = session_id
        seen["lines"] = lines
        return ShellCaptureResult(session_name="ava-agent-42-shell-3-build", lines=["a", "b"])

    monkeypatch.setattr(daemon.cluster, "shell_capture_op", _fake_capture)
    status, result = await daemon._dispatch(
        "shell_capture",
        {"agent_id": 42, "session_id": 3, "lines": 500},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
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

    dispatch_pool: ConnectionPool = _stub_pool()
    seen: dict[str, object] = {}

    def _fake_capture(agent_id: int, session_id: int, lines: int = 200) -> ShellCaptureResult:
        seen["lines"] = lines
        return ShellCaptureResult(session_name="n", lines=[])

    monkeypatch.setattr(daemon.cluster, "shell_capture_op", _fake_capture)
    status, _ = await daemon._dispatch(
        "shell_capture",
        {"agent_id": 1, "session_id": 2},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
    )
    assert status == "completed"
    assert seen == {"lines": 200}


@pytest.mark.asyncio
async def test_dispatch_upload_receive_calls_upload_receive_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upload_receive kind -> uploads.upload_receive_op(payload)."""
    from ops.rpc_schemas import UploadReceiveResult

    dispatch_pool: ConnectionPool = _stub_pool()
    seen: dict[str, object] = {}

    def _fake_receive(payload):
        seen["agent_id"] = payload.agent_id  # pyright: ignore[reportUnknownMemberType]
        seen["name"] = payload.name  # pyright: ignore[reportUnknownMemberType]
        return UploadReceiveResult(
            path=f"/home/runner/Downloads/AvaAgent-{payload.agent_id}/{payload.name}"  # pyright: ignore[reportUnknownMemberType]
        )

    monkeypatch.setattr(daemon.uploads, "upload_receive_op", _fake_receive)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch(
        "upload_receive",
        {"agent_id": 42, "name": "report.pdf"},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
    )
    assert status == "completed"
    assert seen == {"agent_id": 42, "name": "report.pdf"}
    assert result == {"path": "/home/runner/Downloads/AvaAgent-42/report.pdf"}


@pytest.mark.asyncio
async def test_dispatch_upload_receive_bad_payload_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upload_receive with a malformed payload -> failed (not a crash)."""
    dispatch_pool: ConnectionPool = _stub_pool()
    status, result = await daemon._dispatch(
        "upload_receive",
        {"agent_id": "not-an-int"},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
    )
    assert status == "failed"
    assert "error" in result


@pytest.mark.asyncio
async def test_dispatch_lifecycle_calls_lifecycle_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """lifecycle kind -> ops.lifecycle_op with parsed path."""
    dispatch_pool: ConnectionPool = _stub_pool()
    captured: dict[str, object] = {}

    async def _fake_lifecycle(  # type: ignore[no-untyped-def]
        _db: object,
        _bus: object,
        path,
        body,
        pool,
        *,
        trigger_inbound_id=None,
        trigger_inbound_kind=None,
    ):
        from ops.rpc_schemas import TerminateAgentResponse

        captured["path"] = path
        captured["body"] = body
        captured["trigger_inbound_id"] = trigger_inbound_id
        captured["trigger_inbound_kind"] = trigger_inbound_kind
        return TerminateAgentResponse(status=TerminateResult.ENQUEUED)

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _fake_lifecycle)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch(
        "lifecycle",
        {
            "path": "/api/agents/42/resurrect-if-pending-work-v2",
            "body": {"resurrected_by": "system"},
            "trigger_inbound_id": 123,
            "trigger_inbound_kind": "chat",
        },
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
    )
    assert status == "completed"
    # _dispatch serializes the lifecycle response model to a JSON dict for the wire.
    assert result == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}
    assert captured["path"] == "/api/agents/42/resurrect-if-pending-work-v2"
    assert captured["body"] == {"resurrected_by": "system"}
    assert captured["trigger_inbound_id"] == 123
    assert captured["trigger_inbound_kind"] == "chat"


@pytest.mark.asyncio
async def test_dispatch_lifecycle_missing_path_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """lifecycle payload without 'path' returns failed without invoking ops."""
    dispatch_pool: ConnectionPool = _stub_pool()

    async def _should_not_be_called(_db: object, _bus: object, *_a, **_kw):  # type: ignore[no-untyped-def]
        raise AssertionError("lifecycle_op should not be invoked on missing path")

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _should_not_be_called)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch(
        "lifecycle", {}, active_ops={}, workers=set(), pool=dispatch_pool
    )
    assert status == "failed"
    # LifecyclePayload validation rejects a missing 'path' before lifecycle_op runs.
    assert "path" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_unknown_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown kind returns failed; routing table is exhaustive."""
    dispatch_pool: ConnectionPool = _stub_pool()
    status, result = await daemon._dispatch(
        "bogus_kind", {}, active_ops={}, workers=set(), pool=dispatch_pool
    )
    assert status == "failed"
    assert "unknown kind" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_unparseable_lifecycle_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """ops.lifecycle_op raising ValueError lands as failed result, not a crash."""
    dispatch_pool: ConnectionPool = _stub_pool()

    async def _raises(  # type: ignore[no-untyped-def]
        _db: object,
        _bus: object,
        path,
        body,
        pool,
        *,
        trigger_inbound_id=None,
        trigger_inbound_kind=None,
    ):
        raise ValueError(f"lifecycle path not recognized: {path!r}")

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _raises)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch(
        "lifecycle",
        {"path": "/garbage", "body": {}},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
    )
    assert status == "failed"
    assert "not recognized" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_shell_capture_shell_not_found_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_capture_op raising ShellNotFoundError (the session died) lands as a
    plain failed result, not a crash for _ops_route's catch-all to log."""
    from ops.cluster_status import ShellNotFoundError

    dispatch_pool: ConnectionPool = _stub_pool()

    def _raises(agent_id: int, session_id: int, lines: int = 200) -> object:
        raise ShellNotFoundError(f"agent {agent_id} has no live shell {session_id} on this host")

    monkeypatch.setattr(daemon.cluster, "shell_capture_op", _raises)
    status, result = await daemon._dispatch(
        "shell_capture",
        {"agent_id": 42, "session_id": 9},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
    )
    assert status == "failed"
    assert "ShellNotFoundError" in str(result["error"])
    assert "no live shell" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_resurrect_refusal_fails_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resurrection refusal is a durable verdict, returned in the wire form
    the caller classifies (`ResurrectRefused: <reason>`), not a dispatch crash."""
    from base.agents import ResurrectRefused

    dispatch_pool: ConnectionPool = _stub_pool()

    async def _raises(  # type: ignore[no-untyped-def]
        _db: object,
        _bus: object,
        path,
        body,
        pool,
        *,
        trigger_inbound_id=None,
        trigger_inbound_kind=None,
    ):
        raise ResurrectRefused("runtime_cutover_required")

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _raises)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch(
        "lifecycle",
        {"path": "/api/agents/7/resurrect-explicit-v2", "body": {}},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
    )
    assert (status, result) == ("failed", {"error": "ResurrectRefused: runtime_cutover_required"})


@pytest.mark.asyncio
async def test_dispatch_wire_error_carries_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """AvaAgentError raised by an op is converted to a failed result with reason field
    so the gateway's _raise_proxied_wire_error_from_payload can re-emit."""
    dispatch_pool: ConnectionPool = _stub_pool()

    from base.agents import AgentNotFound

    async def _raises(  # type: ignore[no-untyped-def]
        _db: object,
        _bus: object,
        path,
        body,
        pool,
        *,
        trigger_inbound_id=None,
        trigger_inbound_kind=None,
    ):
        raise AgentNotFound("agent 999 does not exist")

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _raises)  # pyright: ignore[reportUnknownArgumentType]
    status, result = await daemon._dispatch(
        "lifecycle",
        {"path": "/api/agents/999/terminate", "body": {}},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
    )
    assert status == "failed"
    assert "AgentNotFound" in str(result["error"])
    assert result.get("reason") == "agent_not_found"


def test_dispatch_requires_daemon_pool() -> None:
    """An unbound daemon dispatch cannot silently borrow ambient startup state."""
    with pytest.raises(TypeError, match="pool"):
        inspect.signature(daemon._dispatch).bind("status_probe", {}, active_ops={}, workers=set())


def test_dispatch_idempotent_requires_daemon_pool() -> None:
    """Idempotency cannot run without its invocation's pool binding."""
    with pytest.raises(TypeError, match="pool"):
        inspect.signature(daemon._dispatch_idempotent).bind(
            "spawn-launch", {"agent_id": 1}, "key-4", active_ops={}, workers=set()
        )


@pytest.mark.asyncio
async def test_dispatch_status_probe_passes_the_daemon_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The steady-state probe reuses the daemon's already-open central DB pool."""
    from ops.cluster_status import ClusterStatus

    pool = _stub_pool()
    seen: list[object] = []
    dispatch_pool: ConnectionPool = pool

    def _status(_db: Database, probe_pool: object) -> ClusterStatus:
        seen.append(probe_pool)
        return ClusterStatus(
            machine_name="win",
            serve_gateway=False,
            serve_agent_runner=True,
            paused=False,
        )

    monkeypatch.setattr(daemon.cluster, "cluster_status_op", _status)

    status, result = await daemon._dispatch(
        "status_probe", {}, active_ops={}, workers=set(), pool=dispatch_pool
    )

    assert status == "completed"
    assert result["machine_name"] == "win"
    assert seen == [pool]


# ─── _ops_route (the POST /ops handler) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_ops_route_wraps_dispatch_in_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid body returns 200 with {status, result} from _dispatch."""
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    dispatch_sem = asyncio.Semaphore(4)

    async def _fake_dispatch(
        kind,
        payload,
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
    ):  # type: ignore[no-untyped-def]
        assert kind == "status_probe"
        return "completed", {"paused": False}

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
    status, body, ctype = await daemon._ops_route(
        json.dumps({"kind": "status_probe"}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
    )
    assert status == 200
    assert ctype == "application/json"
    assert json.loads(body) == {"status": "completed", "result": {"paused": False}}


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
    from datetime import UTC

    from ops.cluster_status import ClusterStatus

    dispatch_sem = asyncio.Semaphore(1)
    dispatch_pool: ConnectionPool = _stub_pool()
    created = datetime(2026, 6, 11, 8, 30, 0, tzinfo=UTC)

    def _status(_db: Database, probe_pool: object) -> ClusterStatus:
        assert probe_pool is dispatch_pool
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

    monkeypatch.setattr(daemon.cluster, "cluster_status_op", _status)
    status, body, ctype = await daemon._ops_route(
        json.dumps({"kind": "status_probe"}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
    )
    assert status == 200
    assert ctype == "application/json"
    parsed = json.loads(body)
    assert parsed["status"] == "completed"
    shell = parsed["result"]["agent_groups"][0]["shells"][0]
    assert shell["name"] == "ava-agent-7"
    assert shell["created_at"] == "2026-06-11T08:30:00Z"


@pytest.mark.asyncio
async def test_ops_route_completes_with_a_db_down_degraded_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB-down, including an unreachable paused row, remains HTTP 200 completed."""
    from ops.cluster_status import ClusterStatus

    pool = _stub_pool()
    dispatch_sem = asyncio.Semaphore(1)
    dispatch_pool: ConnectionPool = pool

    def _degraded_status(_db: Database, probe_pool: object) -> ClusterStatus:
        assert probe_pool is pool
        return ClusterStatus(
            machine_name="win",
            serve_gateway=False,
            serve_agent_runner=True,
            paused=False,
            resource=None,
        )

    monkeypatch.setattr(daemon.cluster, "cluster_status_op", _degraded_status)

    status, body, ctype = await daemon._ops_route(
        json.dumps({"kind": "status_probe"}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
    )

    assert status == 200
    assert ctype == "application/json"
    parsed = json.loads(body)
    assert parsed["status"] == "completed"
    assert parsed["result"]["paused"] is False
    assert "current_orchestration" not in parsed["result"]
    assert "last_updater_outcome" not in parsed["result"]
    assert parsed["result"]["resource"] is None


# ─── main top-level crash handling ─────────────────────────────────────────────


# ─── boot self-registration ────────────────────────────────────────────────────


# ─── idempotency-key dedup (Task #961) ────────────────────────────────────────


@pytest.fixture
def ops_pool() -> object:
    """A real ConnectionPool on the session test DB (the dedup path writes the
    shared `api_idempotency` table, method='ops' rows; `_stub_pool` is a
    non-DB stand-in and cannot serve it)."""
    from psycopg_pool import ConnectionPool

    from base.config import settings

    pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield pool
    finally:
        pool.close()


def _fake_spawn_factory(calls: dict[str, int]) -> object:
    """A launch_agent_op stand-in that counts executions and returns id 777."""
    from ops.rpc_schemas import SpawnedAgent

    async def _fake_spawn(_db, _bus, body, pool):  # type: ignore[no-untyped-def]
        calls["n"] = calls.get("n", 0) + 1
        return SpawnedAgent(id=777)

    return _fake_spawn


# ─── _dispatch_idempotent retry on closed connection (Task #1059) ──────────────


async def _noop_sleep(_seconds: float) -> None:
    """Stand-in for daemon._sleep in retry tests — no real backoff wait."""


# ─── blocking ops run off the event loop ───────────────────────────────────────
