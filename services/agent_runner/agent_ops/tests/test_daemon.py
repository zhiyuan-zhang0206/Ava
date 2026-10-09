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
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from base.agents import ShellKillMode
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.deploy.progress_timeout import NO_PROGRESS_TIMEOUT_S
from base.lm.catalog import ModelCatalog
from ops.rpc_schemas import LaunchAgentRequest, UploadReceivePayload
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
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """spawn-launch kind -> ops.launch_agent_op."""
    dispatch_pool: ConnectionPool = _stub_pool()
    captured: dict[str, object] = {}

    from ops.rpc_schemas import SpawnedAgent

    async def _fake_launch(
        _db: object,
        _bus: object,
        body: LaunchAgentRequest,
        pool: ConnectionPool | None,
        *,
        catalog: ModelCatalog,
    ):
        captured["agent_id"] = body.agent_id
        captured["pool"] = pool
        return SpawnedAgent(id=777)

    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", _fake_launch)
    status, result = await daemon._dispatch(
        "spawn-launch-v2",
        {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 777},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "completed"
    assert result == {"id": 777}
    assert captured["agent_id"] == 777


@pytest.mark.asyncio
async def test_dispatch_shell_probe_calls_shell_probe_op(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
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
        "shell_probe",
        {"agent_id": 42},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
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
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """shell_probe without agent_id fails without invoking the op."""
    dispatch_pool: ConnectionPool = _stub_pool()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("must not dispatch on bad payload")

    monkeypatch.setattr(
        daemon.cluster,
        "shell_probe_op",
        forbidden,
    )
    status, result = await daemon._dispatch(
        "shell_probe",
        {},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "failed"
    assert "agent_id" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_shell_kill_calls_shell_kill_op(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
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
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "completed"
    assert seen == {"agent_id": 42, "session_id": 5}
    assert result == {"mode": "killed", "interrupted": False, "name": None}


@pytest.mark.asyncio
async def test_dispatch_shell_kill_reports_absent(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A shell already gone is a successful, idempotent absent result."""
    from ops.rpc_schemas import ShellKillResult

    dispatch_pool: ConnectionPool = _stub_pool()

    def absent(_agent_id: int, _session_id: int) -> ShellKillResult:
        return ShellKillResult(mode=ShellKillMode.ABSENT)

    monkeypatch.setattr(
        daemon.cluster,
        "shell_kill_op",
        absent,
    )
    status, result = await daemon._dispatch(
        "shell_kill",
        {"agent_id": 42, "session_id": 999},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "completed"
    assert result == {"mode": "absent", "interrupted": False, "name": None}


@pytest.mark.asyncio
async def test_dispatch_agent_skill_view_calls_machine_op(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
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
        "agent_skill_view",
        {"agent_id": 42},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "completed"
    assert seen == {"agent_id": 42, "pool": pool}
    assert result == {
        "commands": [{"name": "project", "description": "d", "instruction_hint": "h"}],
        "mcp_names": [],
    }


@pytest.mark.asyncio
async def test_dispatch_agent_skill_view_bad_payload_fails(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """agent_skill_view without an id is rejected before it reaches the op."""
    dispatch_pool: ConnectionPool = _stub_pool()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("must not dispatch on bad payload")

    monkeypatch.setattr(
        daemon.cluster,
        "agent_skill_view_op",
        forbidden,
    )
    status, result = await daemon._dispatch(
        "agent_skill_view",
        {},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "failed"
    assert "agent_id" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_shell_capture_calls_shell_capture_op(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
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
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
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
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
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
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "completed"
    assert seen == {"lines": 200}


@pytest.mark.asyncio
async def test_dispatch_upload_receive_calls_upload_receive_op(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """upload_receive kind -> uploads.upload_receive_op(payload)."""
    from ops.rpc_schemas import UploadReceiveResult

    dispatch_pool: ConnectionPool = _stub_pool()
    seen: dict[str, object] = {}

    def _fake_receive(payload: UploadReceivePayload, *, pool: ConnectionPool | None = None):
        assert pool is dispatch_pool
        seen["agent_id"] = payload.agent_id
        seen["name"] = payload.name
        return UploadReceiveResult(
            path=f"/home/runner/Downloads/AvaAgent-{payload.agent_id}/{payload.name}"
        )

    monkeypatch.setattr(daemon.uploads, "upload_receive_op", _fake_receive)
    status, result = await daemon._dispatch(
        "upload_receive",
        {"agent_id": 42, "name": "report.pdf"},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "completed"
    assert seen == {"agent_id": 42, "name": "report.pdf"}
    assert result == {"path": "/home/runner/Downloads/AvaAgent-42/report.pdf"}


@pytest.mark.asyncio
async def test_dispatch_upload_receive_bad_payload_fails(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """upload_receive with a malformed payload -> failed (not a crash)."""
    dispatch_pool: ConnectionPool = _stub_pool()
    status, result = await daemon._dispatch(
        "upload_receive",
        {"agent_id": "not-an-int"},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "failed"
    assert "error" in result


@pytest.mark.asyncio
async def test_dispatch_unknown_kind(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """Unknown kind returns failed; routing table is exhaustive."""
    dispatch_pool: ConnectionPool = _stub_pool()
    status, result = await daemon._dispatch(
        "bogus_kind",
        {},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "failed"
    assert "unknown kind" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_shell_capture_shell_not_found_fails(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
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
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "failed"
    assert "ShellNotFoundError" in str(result["error"])
    assert "no live shell" in str(result["error"])


# ─── _ops_route (the POST /ops handler) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_ops_route_wraps_dispatch_in_envelope(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A valid body returns 200 with {status, result} from _dispatch."""
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    dispatch_sem = asyncio.Semaphore(4)

    async def _fake_dispatch(
        kind: str,
        payload: dict[str, Any],
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
    ) -> tuple[str, dict[str, object]]:
        assert kind == "status_probe"
        return "completed", {"paused": False}

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)
    status, body, ctype = await daemon._ops_route(
        json.dumps({"kind": "status_probe"}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == 200
    assert ctype == "application/json"
    assert json.loads(body) == {"status": "completed", "result": {"paused": False}}


@pytest.mark.asyncio
async def test_ops_route_status_probe_serializes_datetime_fields(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
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
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
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
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
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
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
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

    async def _fake_spawn(
        _db: object,
        _bus: object,
        body: LaunchAgentRequest,
        pool: ConnectionPool | None,
        *,
        catalog: ModelCatalog,
    ) -> SpawnedAgent:
        calls["n"] = calls.get("n", 0) + 1
        return SpawnedAgent(id=777)

    return _fake_spawn


# ─── _dispatch_idempotent retry on closed connection (Task #1059) ──────────────


async def _noop_sleep(_seconds: float) -> None:
    """Stand-in for daemon._sleep in retry tests — no real backoff wait."""


# ─── blocking ops run off the event loop ───────────────────────────────────────
