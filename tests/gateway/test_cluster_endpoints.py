"""`/api/cluster/*` endpoint + 503 middleware + handler unit tests.

Middleware short-circuits SDK paths only after service shutdown publishes the
paused posture; native drain keeps those dependencies available. Session
backends are recorded, while local drain uses the private test database.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import psycopg
import pytest
from fastapi.testclient import TestClient

import gateway.app as gateway_app
from gateway import loki_events
from gateway._cors import cors_allowed_origins
from gateway.app import app
from ops import cluster as cluster_mod
from ops import cluster_deploy, cluster_pause, cluster_status
from ops import update_check as update_check_mod
from shared.cluster_lock import DeployLease, RecoveryClaim


@pytest.fixture
def fake_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A tmp file standing in for the paused posture row, controllable by the tests.

    R1 (Task #1021) moved `is_paused` to the host_deploy_state posture row; the
    readers' bound names are shimmed to the file's existence so this suite keeps
    simulating the pause with a file. The fixture is about "the process agrees on
    one paused signal", not which storage (the old-signal sweep, PR5, retired the
    real flag file).

    The gateway middleware reads the posture through the async
    `gateway.app._cluster_is_paused` (pool + to_thread + TTL cache, audit
    P1-1), so the middleware's bound name is shimmed with an async stand-in;
    the status/cluster routers still read the sync `is_paused`."""
    flag = tmp_path / "cluster_paused"

    async def _paused(_request: object) -> bool:
        return flag.exists()

    def _snapshot_paused(_state: object = None) -> bool:
        return flag.exists()

    monkeypatch.setattr("gateway.app._cluster_is_paused", _paused)
    monkeypatch.setattr("gateway.routers.cluster.cluster_is_paused", flag.exists)
    monkeypatch.setattr("gateway.routers.status.cluster_is_paused", flag.exists)
    monkeypatch.setattr("ops.cluster_pause.is_paused", _snapshot_paused)
    return flag


@pytest.fixture(autouse=True)
def _pin_session_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the session-name composer for gateway cluster tests so composed
    session names are deterministic regardless of the dev host config.
    Patches ``session_name`` directly so ``machine_name()``
    stay untouched (other gateway tests call them via ``set_machine_identity``
    + ``status_snapshot()`` and assert the injected value).
    Produces names like ``ava-test-agent-host``, ``ava-test-updater`` etc.

    ``session_name`` is patched at its source (``shared.cluster``) because the
    naming scheme is one fact for the whole process — the four ``ops.cluster*``
    modules reach it through that module precisely so one setattr pins it for all
    of them. The spawn tests patch ``shared.session_backend.get_backend`` to a
    recording fake, so the composed names are what the assertions read.
    """
    monkeypatch.setattr("shared.cluster.session_name", lambda svc: f"ava-test-{svc}")  # pyright: ignore[reportUnknownArgumentType]


# ─── middleware 503 mode ──────────────────────────────────────────────────────


class TestPauseMiddleware:
    def test_expired_pause_cache_uses_one_control_pool_read(self) -> None:
        """Concurrent middleware requests share the expired-cache DB read."""
        entered = threading.Event()
        release = threading.Event()
        reads = 0

        class Cursor:
            def __enter__(self) -> Cursor:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def execute(self, _query: str, _params: tuple[str]) -> None:
                nonlocal reads
                reads += 1
                entered.set()
                assert release.wait(timeout=2)

            def fetchone(self) -> tuple[str]:
                return ("running",)

        class Connection:
            def __enter__(self) -> Connection:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def cursor(self) -> Cursor:
                return Cursor()

        class ControlPool:
            def connection(self) -> Connection:
                return Connection()

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(control_db_pool=ControlPool()))
        )

        async def run() -> None:
            gateway_app._pause_cache[0] = None
            gateway_app._pause_inflight[0] = None
            first = asyncio.create_task(gateway_app._cluster_is_paused(cast(Any, request)))
            assert await asyncio.to_thread(entered.wait, 2)
            second = asyncio.create_task(gateway_app._cluster_is_paused(cast(Any, request)))
            await asyncio.sleep(0)
            release.set()
            assert await first is False
            assert await second is False

        try:
            asyncio.run(run())
        finally:
            gateway_app._pause_cache[0] = None
            gateway_app._pause_inflight[0] = None
        assert reads == 1

    def test_sdk_path_returns_503_when_paused(self, fake_flag: Path) -> None:
        fake_flag.write_text("")
        with TestClient(app) as client:
            r = client.get("/api/agents")
        assert r.status_code == 503
        assert "updating" in r.json()["detail"]
        assert r.headers.get("Retry-After") == "30"

    def test_paused_503_carries_cors_headers(self, fake_flag: Path) -> None:
        """A 503 short-circuited by the pause middleware still carries the CORS
        headers — CORSMiddleware is the OUTERMOST middleware, so a browser
        caller sees the real 503 instead of "Failed to fetch" (#187)."""
        fake_flag.write_text("")
        allowed_origin = cors_allowed_origins()[0]
        with TestClient(app) as client:
            r = client.get("/api/agents", headers={"Origin": allowed_origin})
        assert r.status_code == 503
        assert r.headers["access-control-allow-origin"] == allowed_origin
        assert r.headers["access-control-allow-credentials"] == "true"

    def test_cluster_status_bypasses_503(self, fake_flag: Path, set_machine_identity) -> None:
        """`/api/cluster/status` still returns 200 while paused — it is the observability + control path."""
        fake_flag.write_text("")
        set_machine_identity(role="gateway", name="test-mc")
        with TestClient(app) as client:
            r = client.get("/api/cluster/status")
        assert r.status_code == 200
        body = r.json()
        assert body["paused"] is True
        assert body["machine_name"] == "test-mc"
        assert body["serve_gateway"] is True
        assert body["serve_agent_runner"] is False
        # The bypass status carries current_orchestration so a paused consumer can
        # tell a live rollout from a stranded pause (None here — no live session).
        assert body["current_orchestration"] is None

    def test_no_503_when_unpaused(self, fake_flag: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Flag absent → middleware does not block; /api/agents follows normal business path."""
        # flag absent by default
        with TestClient(app) as client:
            r = client.get("/api/agents")
        # just not 503 (business logic may return 200 / other, but must not be our paused middleware)
        assert r.status_code != 503

    def test_alerts_webhook_bypasses_503(self, fake_flag: Path) -> None:
        """The alert webhook must land during a rollout window — a
        503 there exhausts Grafana's retries and the alert is lost. The 401
        (no webhook token configured in this test env) proves the request
        reached the router instead of the pause short-circuit."""
        fake_flag.write_text("")
        with TestClient(app) as client:
            r = client.post(
                "/api/alerts",
                json={"status": "firing", "alerts": []},
            )
        assert r.status_code == 401

    def test_alerts_panel_still_503_when_paused(self, fake_flag: Path) -> None:
        """Only the webhook bypasses the pause — the read/stream paths stay
        503 like every other path (read-only, not needed mid-rollout)."""
        fake_flag.write_text("")
        with TestClient(app) as client:
            r = client.get("/api/alerts")
        assert r.status_code == 503

    def test_openapi_disabled(self, fake_flag: Path) -> None:
        """FastAPI metadata (`/openapi.json` / `/docs` / `/redoc`) is turned off
        at app construction so it can't leak the full route schema. Unpaused
        the route is FastAPI 404; paused, /api/cluster/* is the only bypass
        and metadata returns 503 along with everything else (correct — there's
        nothing to serve and no caller depends on this path)."""
        with TestClient(app) as client:
            r = client.get("/openapi.json")
        assert r.status_code == 404

    def test_other_agent_paths_still_503_when_paused(self, fake_flag: Path) -> None:
        """Only the agent SELF-REPORT paths bypass the pause — an externally
        initiated terminate stays 503 (business logic, not a drain signal)."""

    def test_alwk_does_not_claim_while_paused(
        self, fake_flag: Path, db_conn: psycopg.Connection
    ) -> None:
        """Middleware order (audit P2-1): a paused cluster 503s BEFORE dedup
        engages — a pause-window ALWK request must not leave a placeholder
        row. The old registration order ran dedup first, so every
        pause-window request claimed then 503'd (an INSERT+DELETE per
        request, and a bricked key if the process died inside the window)."""
        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO agents (label) VALUES ('pause-order') RETURNING id")
            row = cur.fetchone()
            assert row is not None
            aid = row[0]
            cur.execute("INSERT INTO agents_meta (id, status) VALUES (%s, 'running')", (aid,))
        db_conn.commit()
        fake_flag.write_text("")
        with TestClient(app) as client:
            r = client.post(
                f"/api/agents/{aid}/messages",
                json={"content": "during pause", "source": "user"},
                headers={"Idempotency-Key": "key-pause-order"},
            )
        assert r.status_code == 503
        with db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM api_idempotency WHERE key = %s", ("key-pause-order",))
            row = cur.fetchone()
            assert row is not None and row[0] == 0, "dedup must not claim while paused"
        fake_flag.write_text("")
        with TestClient(app) as client:
            r = client.post("/api/agents/123/terminate", json={"source": "user"})
        assert r.status_code == 503


# ─── handler business logic ───────────────────────────────────────────────────


class _FakeSessionBackend:
    """Record every service and orchestration session operation without spawning."""

    def __init__(self) -> None:
        self.killed: list[str] = []
        self.spawned: list[str] = []
        self.spawn_calls: list[tuple[str, str, object]] = []
        self.alive_answer: bool | list[bool] = False
        self.alive_by_name: dict[str, bool] = {}
        self.spawn_ok = True
        self.spawn_ok_by_name: dict[str, bool] = {}

    def _alive(self, name: str) -> bool:
        if name in self.alive_by_name:
            return self.alive_by_name[name]
        if isinstance(self.alive_answer, list):
            return self.alive_answer.pop(0)
        return self.alive_answer

    def has_session(self, name: str) -> bool:
        return self._alive(name)

    def kill_session(
        self, name: str, *, graceful: bool = False, expected: bool = False, **_: object
    ) -> tuple[bool, str]:
        self.killed.append(name)
        return True, "stub"

    def new_session(self, name: str, cmd: str, cwd: object, *, env: object, **_: object) -> bool:
        self.spawned.append(name)
        self.spawn_calls.append((name, cmd, cwd))
        return self.spawn_ok_by_name.get(name, self.spawn_ok)

    def list_sessions(self, prefix: str = "") -> list[str]:
        return []


@pytest.fixture
def pause_backend(monkeypatch: pytest.MonkeyPatch) -> _FakeSessionBackend:
    from ops import agent_pause

    backend = _FakeSessionBackend()
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    monkeypatch.setattr(agent_pause, "host_running", lambda: False)
    return backend


@pytest.fixture
def spawn_backend(monkeypatch: pytest.MonkeyPatch) -> _FakeSessionBackend:
    """The service session backend the orchestration spawns land on (S7).

    Patching `shared.session_backend.get_backend` redirects every launch AND
    every in-flight probe (`_has_orchestration_session`) to the fake, so the
    spawn tests below assert on the recorded command instead of a raw argv."""
    backend = _FakeSessionBackend()

    def _skip_ui_owner(**_kw: object) -> None:
        return None

    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    # Command-shape tests have no detached child to acquire the DB lease and
    # publish the UI owner. The wait/child-ownership contract is covered by the
    # dedicated lifecycle and spawn-backend suites.
    monkeypatch.setattr(cluster_deploy, "_wait_for_ui_owner", _skip_ui_owner)
    return backend


class TestPauseLocalCluster:
    @pytest.fixture(autouse=True)
    def _private_pause_owner(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from shared import pause_owner

        monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
        monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")

    def test_completed_drain_keeps_sdk_requests_and_services_available(
        self, pause_backend: _FakeSessionBackend
    ) -> None:
        """Phase A may finish while a peer still needs this gateway's SDK API."""
        from shared import maintenance

        pause_backend.alive_answer = True

        cluster_mod.pause_local_cluster()

        current = maintenance.snapshot()
        assert current is not None and current.maintenance is not None
        assert current.maintenance.phase == "drained"
        with TestClient(app) as client:
            assert client.get("/api/agents").status_code == 200
        assert not cluster_pause.is_paused()
        assert pause_backend.killed == []
        assert pause_backend.has_session("ava-test-agent-host")

    def test_idempotent_when_session_missing(self, pause_backend: _FakeSessionBackend) -> None:
        """Repeated Phase A reuses the same drain without starting services."""
        from shared import pause_owner

        cluster_mod.pause_local_cluster()
        first = pause_owner.read()
        cluster_mod.pause_local_cluster()
        assert pause_owner.read() == first
        assert first.maintenance is not None and first.maintenance.phase == "drained"
        assert pause_backend.killed == pause_backend.spawned == []


# Subject under test: these call the real ops.cluster spawn entry points (with
# subprocess.run faked), so they opt out of the autouse guard that refuses them
# suite-wide (tests/conftest.py:_guard_cluster_spawn).
@pytest.mark.real_cluster_spawn
class TestUnpauseLocalCluster:
    @pytest.fixture(autouse=True)
    def _private_pause_owner(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from shared import pause_owner

        monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
        monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")

    def test_unpause_restores_posture_and_releases_admission(
        self, pause_backend: _FakeSessionBackend
    ) -> None:
        from shared import maintenance
        from shared.host_deploy_state import set_posture

        cluster_mod.pause_local_cluster()
        set_posture("paused")
        assert cluster_pause.is_paused()

        cluster_mod.unpause_local_cluster()

        assert not cluster_pause.is_paused()
        assert not maintenance.held()
        assert pause_backend.spawned == pause_backend.killed == []

    def test_missing_pause_and_repeated_resume_do_not_start_services(
        self, pause_backend: _FakeSessionBackend
    ) -> None:
        cluster_mod.unpause_local_cluster()
        cluster_mod.unpause_local_cluster()
        assert not cluster_pause.is_paused()
        assert pause_backend.spawned == pause_backend.killed == []


@pytest.mark.real_cluster_spawn
class TestSpawnUpdate:
    """spawn_update() must launch the `ava-updater` orchestration session through
    the service session backend (get_backend(); POSIX: native process supervisor,
    Windows: winproc) — S7 moved the orchestration sessions onto it, retiring
    the last legacy branch in the cluster path."""

    @pytest.fixture(autouse=True)
    def _stub_migration_vet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """spawn_update vets the target's migrations/ from git (validate-before-kill)
        before pausing; these tests drive the spawn / pause mechanics, so default the
        vet to pass. The refusal path has its own test (test_spawn_update_refuses_*)."""
        monkeypatch.setattr("shared.migrations.validate_migrations_at_ref", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]

    def test_spawn_update_launches_the_updater_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """spawn_update lands on the session backend as `ava-updater`, with the
        in-process entry and the wrapper guard: the entry runs only after a
        successful cd, and its verdict is reported on [session-exit] rc=."""
        monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: None)
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

        result = cluster_mod.spawn_update()
        assert result["session"] == "ava-test-updater"
        assert "log" in result
        name, cmd, cwd = spawn_backend.spawn_calls[0]
        assert name == "ava-test-updater"
        assert cwd == cluster_mod._REPO_ROOT
        # The detached session runs the in-process self-update (R1-6): the entry
        # owns the force-checkout (landing from any branch / dirty state), tree
        # verification, uv sync and the graceful restart — no hand-built ladder.
        assert "python -m cli.commands._update_agent_runner" in cmd
        assert "--mode smooth" in cmd  # the drain policy is always spelled out
        assert "git pull" not in cmd
        assert "tee -a" in cmd
        # Lock the wrapper guard: the entry runs only after a successful cd, and
        # its verdict is reported on [session-exit] rc= for updater_outcome.
        assert "if cd" in cmd
        assert "rc=$?" in cmd
        assert 'echo "[session-exit] rc=$rc"' in cmd
        assert "else" in cmd
        assert "} 2>&1 | tee -a" in cmd
        # The venv is re-activated inside the command (the backend's bash -lc
        # wrapper and this prefix both re-export it after any login profile).
        assert "VIRTUAL_ENV=" in cmd

    def test_spawn_update_force_checks_out_pinned_target_sha(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """With a pinned target_sha, the inner command force-checks-out exactly it (not
        origin/main, not git pull) so every node in the rollout lands on the same commit."""
        monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: None)
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

        cluster_mod.spawn_update(target_sha="PINNEDSHA")
        cmd = spawn_backend.spawn_calls[0][1]
        # The pinned sha rides --target-sha into the in-process entry (R1-6); the
        # checkout itself happens inside the entry, not in the shell command.
        assert "python -m cli.commands._update_agent_runner" in cmd
        assert "--target-sha PINNEDSHA" in cmd
        assert "git pull" not in cmd

    def test_spawn_update_rejects_if_session_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """When the backend reports the updater session alive, spawn_update raises
        ClusterUpdateInProgress."""
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        spawn_backend.alive_answer = True
        with pytest.raises(cluster_mod.ClusterUpdateInProgress):
            cluster_mod.spawn_update()

    def test_spawn_update_pause_called_before_spawn(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """pause_local_cluster() must be called before the updater session spawn."""
        order: list[str] = []
        monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: order.append("pause"))
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

        cluster_mod.spawn_update()
        assert order == ["pause"], f"pause must precede the spawn, got {order}"
        assert spawn_backend.spawn_calls, "the updater session must have been spawned"

    def test_spawn_update_refuses_broken_layout_before_pause(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """validate-before-kill: a target whose migrations/ layout is broken is
        refused BEFORE pause_local_cluster / the updater spawn, so the cluster
        keeps serving its current code (the 2026-06-17 dup-0049 outage class)."""
        from shared.migrations import MigrationLayoutError

        events: list[str] = []
        monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: events.append("pause"))
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

        def _raise(_ref, **_kw):
            raise MigrationLayoutError("duplicate migration name: '20260719T143000_add-foo'")

        monkeypatch.setattr("shared.migrations.validate_migrations_at_ref", _raise)  # pyright: ignore[reportUnknownArgumentType]

        with pytest.raises(MigrationLayoutError, match="duplicate migration name"):
            cluster_mod.spawn_update(target_sha="POISONED")
        assert events == [], f"refusal must precede pause + spawn, got {events}"
        assert spawn_backend.spawn_calls == []

    def test_spawn_update_git_fetch_has_timeout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """The validate-before-kill best-effort `git fetch` must be bounded — it
        runs synchronously inside the ops server's async dispatch, so an unbounded
        fetch (stalled network) would block that event loop indefinitely, taking
        the compensating /api/cluster/resume down with it.

        Bounded via `run_bounded`, not `subprocess.run(timeout=)`: this is the
        exact site that leaked 66 git/ssh/sh processes on the Windows agent-runner,
        where the timeout killed Git-for-Windows' launcher stub and left the real
        git plus its ssh alive. Asserted here as "goes through run_bounded",
        because a plain `subprocess.run` with a correct timeout constant looks
        identical and is the bug.
        """
        import subprocess as _sp

        seen: dict = {}

        def fake_bounded(argv, **kwargs):  # type: ignore[no-untyped-def]
            seen["kwargs"] = kwargs
            return _sp.CompletedProcess(argv, returncode=0, stdout="", stderr="")  # pyright: ignore[reportUnknownArgumentType]

        monkeypatch.setattr(cluster_deploy, "run_bounded", fake_bounded)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: None)
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

        cluster_mod.spawn_update()
        assert seen["kwargs"]["timeout"] == cluster_mod._VALIDATE_FETCH_TIMEOUT_S
        # and non-interactive: no credential prompt, no unbounded ssh dial
        assert seen["kwargs"]["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "BatchMode=yes" in seen["kwargs"]["env"]["GIT_SSH_COMMAND"]

    def test_spawn_update_survives_git_fetch_timeout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """A `subprocess.TimeoutExpired` from the best-effort fetch must not crash
        spawn_update — it proceeds to validate_migrations_at_ref, which fails
        closed on its own if the ref turns out unreadable (unchanged contract);
        here the vet is stubbed to pass, so the whole call still succeeds."""
        import subprocess as _sp

        vetted_refs: list[str] = []

        def fake_bounded(argv, **kwargs):  # type: ignore[no-untyped-def]
            # `run_bounded` raises TimeoutExpired after killing the tree; the
            # caller's contract is unchanged by that, which is what this asserts.
            raise _sp.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])  # pyright: ignore[reportUnknownArgumentType]

        def _stub_vet(ref, **_kw):  # type: ignore[no-untyped-def]
            vetted_refs.append(ref)  # pyright: ignore[reportUnknownArgumentType]

        monkeypatch.setattr(cluster_deploy, "run_bounded", fake_bounded)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("shared.migrations.validate_migrations_at_ref", _stub_vet)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: None)
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

        result = cluster_mod.spawn_update(target_sha="PINNEDSHA")
        assert result["session"] == "ava-test-updater"
        assert vetted_refs == ["PINNEDSHA"]

    def test_post_update_returns_502_when_update_in_progress(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        """HTTP layer: an in-flight update raises on the target's ops server,
        surfacing as a failed op -> 502 carrying the failure payload."""
        from gateway.routers import cluster as cluster_router

        set_machine_identity(role="gateway", name="test-host")

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            raise cluster_router._cluster_rpc.ClusterOpFailed(
                {
                    "error": "ClusterUpdateInProgress: orchestration session 'ava-test-updater' already exists"
                }
            )

        monkeypatch.setattr(cluster_router._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post("/api/cluster/update")
        assert r.status_code == 502
        assert "ava-test-updater" in r.json()["detail"]

    def test_post_update_forwards_target_sha_local(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        """POST /api/cluster/update?target_sha=... dispatches a cluster_update op
        to this host's own ops server with the target_sha in the payload (the
        watchdog off-pin self-heal relies on this; the param was previously
        dropped at the HTTP boundary)."""
        from gateway.routers import cluster as cluster_router

        set_machine_identity(role="gateway", name="test-host")
        captured: dict[str, object] = {}

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            captured["target_machine"] = target_machine
            captured["kind"] = kind
            captured["payload"] = payload
            return {"session": "s", "log": "l"}

        monkeypatch.setattr(cluster_router._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post("/api/cluster/update", params={"target_sha": "abc1234"})
        assert r.status_code == 202
        assert captured["target_machine"] == "test-host"
        assert captured["kind"] == "cluster_update"
        assert captured["payload"] == {"target_sha": "abc1234"}

    def test_post_update_forwards_target_sha_remote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A remote target POSTs cluster_update with the target_sha in the op payload."""
        from gateway.routers import cluster as cluster_router

        captured: dict[str, object] = {}

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            captured["payload"] = payload
            return {"session": "ava-main-updater", "log": "/x"}

        monkeypatch.setattr(cluster_router._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(cluster_router, "machine_name", lambda: "cloud")
        with TestClient(app) as client:
            r = client.post(
                "/api/cluster/update", params={"target": "wsl", "target_sha": "abc1234"}
            )
        assert r.status_code == 202
        assert captured["payload"] == {"target_sha": "abc1234"}

    def test_spawn_update_log_file_created(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """spawn_update creates a log file under $AVA_HOME/logs/updater-<ts>.log."""
        monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: None)
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

        result = cluster_mod.spawn_update()
        log_path = Path(result["log"])
        assert log_path.parent == tmp_path / "logs"
        assert log_path.name.startswith("updater-")

    def test_spawn_update_rolls_back_pause_when_spawn_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """If the session backend declines to start the updater, the prior
        pause_local_cluster() is rolled back and OrchestrationSpawnFailed surfaces
        (the gateway's 503)."""
        paused: list[bool] = []
        monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: paused.append(True))
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        posture: list[str] = []
        monkeypatch.setattr("shared.host_deploy_state.set_posture", posture.append)
        spawn_backend.spawn_ok_by_name["ava-test-updater"] = False

        with pytest.raises(cluster_mod.OrchestrationSpawnFailed):
            cluster_mod.spawn_update()

        assert paused == [True], "pause_local_cluster must have been called"
        assert posture and posture[-1] == "idle", "the posture must return to idle on rollback"

    def test_spawn_update_raises_orchestration_spawn_failed_on_backend_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """A backend launch failure (the old missing-binary class) surfaces as
        OrchestrationSpawnFailed — the same fail-fast, minus the legacy dependency."""

        def _boom(*_a, **_k):
            raise RuntimeError("reparent helper failed to launch session")

        monkeypatch.setattr(spawn_backend, "new_session", _boom)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: None)
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

        with pytest.raises(cluster_mod.OrchestrationSpawnFailed):
            cluster_mod.spawn_update()

    def test_post_update_returns_503_when_ops_unreachable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        """HTTP layer: an unreachable ops server maps to 503 with detail."""
        from gateway.routers import cluster as cluster_router

        set_machine_identity(role="gateway", name="test-host")

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            raise cluster_router._cluster_rpc.ClusterOpUnreachable("connect timeout")

        monkeypatch.setattr(cluster_router._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post("/api/cluster/update")
        assert r.status_code == 503
        assert "unreachable" in r.json()["detail"]


def _patch_update_check_behind(monkeypatch: pytest.MonkeyPatch, behind: int) -> None:
    """Stub update_check() so spawn_rollout's behind==0 gate sees `behind`.

    spawn_rollout now calls update_check() (real git fetch) at its entry; the
    happy-path spawn tests must pin a non-zero `behind` so the gate lets the
    spawn through without touching the network.
    """
    monkeypatch.setattr(
        cluster_deploy,
        "update_check",
        lambda: cluster_mod.UpdateCheck(
            behind=behind,
            frontend_changed=False,
            backend_changed=behind > 0,
            needs_replay=False,
        ),
    )


# Subject under test: these call the real ops.cluster spawn entry points (with
# the session backend faked), so they opt out of the autouse guard that refuses
# them suite-wide (tests/conftest.py:_guard_cluster_spawn).
@pytest.mark.real_cluster_spawn
class TestSpawnRollout:
    """spawn_rollout() must launch the `ava-rollout` orchestration session through
    the service session backend, running `ava cluster update --local`."""

    def test_spawn_rollout_launches_the_rollout_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """spawn_rollout lands on the session backend as `ava-rollout`, with the
        in-process orchestration and the tee pipeline."""
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        _patch_update_check_behind(monkeypatch, behind=2)

        result = cluster_mod.spawn_rollout("test-origin")
        assert result["session"] == "ava-test-rollout"
        assert "log" in result
        # Rollout scope rides the 202 so the frontend "Update" button knows
        # whether to say agents will be restarted (backend change) or not
        # (UI/docs-only).
        assert result["backend_changed"] is True
        assert result["needs_replay"] is False
        name, cmd, cwd = spawn_backend.spawn_calls[0]
        assert name == "ava-test-rollout"
        assert cwd == cluster_mod._REPO_ROOT
        # `--local` so the detached session runs the in-process orchestration
        # rather than re-POSTing the now-default thin path (which would recurse).
        assert "ava cluster update --local" in cmd
        assert "tee -a" in cmd
        # Rollout drives the orchestrator; it does NOT do git pull / ava restart itself.
        assert "ava restart" not in cmd
        assert "git pull" not in cmd

    def test_spawn_rollout_hands_the_orchestration_the_log_it_will_tee_into(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """This side creates the log file and then detaches, so the orchestration has
        no way to learn the path on its own — and a record that cannot name its log
        sends an operator to pick the right `rollout-*.log` by mtime. It rides down
        as `--rollout-log`, pointing at the same file the pipeline tees into."""
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        _patch_update_check_behind(monkeypatch, behind=2)

        result = cluster_mod.spawn_rollout("test-origin")

        cmd = spawn_backend.spawn_calls[0][1]
        assert f"--rollout-log {result['log']}" in cmd
        assert f"tee -a {result['log']}" in cmd, "the recorded log must be the one being written"

    def test_spawn_rollout_does_not_pause_local_cluster(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """spawn_rollout must NOT call pause_local_cluster — `ava cluster update` manages pausing itself."""
        pause_calls: list[bool] = []
        monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: pause_calls.append(True))
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        _patch_update_check_behind(monkeypatch, behind=2)

        cluster_mod.spawn_rollout("test-origin")
        assert pause_calls == [], "spawn_rollout must not pause the local cluster"

    def test_spawn_rollout_rejects_if_rollout_session_exists(
        self, monkeypatch: pytest.MonkeyPatch, spawn_backend: _FakeSessionBackend
    ) -> None:
        """ava-rollout session alive → ClusterUpdateInProgress."""
        spawn_backend.alive_by_name["ava-test-rollout"] = True
        with pytest.raises(cluster_mod.ClusterUpdateInProgress):
            cluster_mod.spawn_rollout("test-origin")

    def test_spawn_rollout_rejects_if_updater_session_exists(
        self, monkeypatch: pytest.MonkeyPatch, spawn_backend: _FakeSessionBackend
    ) -> None:
        """A live ava-updater (per-host update) session also blocks a rollout."""
        spawn_backend.alive_by_name["ava-test-updater"] = True
        with pytest.raises(cluster_mod.ClusterUpdateInProgress):
            cluster_mod.spawn_rollout("test-origin")

    def test_spawn_rollout_log_file_created(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """spawn_rollout creates a log file under $AVA_HOME/logs/rollout-<ts>.log."""
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        _patch_update_check_behind(monkeypatch, behind=2)

        result = cluster_mod.spawn_rollout("test-origin")
        log = result["log"]
        assert isinstance(log, str)  # narrow str | bool (backend_changed rides the same dict)
        log_path = Path(log)
        assert log_path.parent == tmp_path / "logs"
        assert log_path.name.startswith("rollout-")

    def test_spawn_rollout_raises_orchestration_spawn_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """A backend spawn failure surfaces as OrchestrationSpawnFailed (no pause
        flag to roll back — rollout never paused the local host)."""
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        _patch_update_check_behind(monkeypatch, behind=2)
        spawn_backend.spawn_ok = False
        with pytest.raises(cluster_mod.OrchestrationSpawnFailed):
            cluster_mod.spawn_rollout("test-origin")

    def test_spawn_rollout_fails_fast_when_already_up_to_date(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """behind==0 → NothingToUpdate before any orchestration session is spawned.

        A no-op rollout would bounce the whole fleet for zero code change, so the
        chokepoint refuses. Asserts no session was spawned.
        """
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        _patch_update_check_behind(monkeypatch, behind=0)

        with pytest.raises(cluster_mod.NothingToUpdate, match="already up to date"):
            cluster_mod.spawn_rollout("test-origin")
        assert spawn_backend.spawn_calls == [], "behind==0 must not spawn a rollout session"

    def test_spawn_rollout_replays_bookmark_disagreement(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """A zero-distance half-deployment must reach the recovery rollout, not 422."""
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        monkeypatch.setattr(
            cluster_deploy,
            "update_check",
            lambda: cluster_mod.UpdateCheck(
                behind=0, frontend_changed=False, backend_changed=False, needs_replay=True
            ),
        )

        result = cluster_mod.spawn_rollout("test-origin")

        assert result["session"] == "ava-test-rollout"
        assert spawn_backend.spawn_calls, "a replay-required state must launch the rollout"

    def test_spawn_restart_does_not_check_behind(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """Restart pulls nothing, so it is never gated on behind.

        Stub update_check to behind==0 (and make it explode if called) and assert
        spawn_restart still spawns its session — the behind gate is rollout-only.
        """
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

        def _boom() -> object:
            raise AssertionError("restart must not consult update_check()")

        monkeypatch.setattr(cluster_deploy, "update_check", _boom)

        result = cluster_mod.spawn_restart("test-origin")
        assert result["session"] == "ava-test-cluster-restart"


# Subject under test: these call the real ops.cluster spawn entry points (with
# the session backend faked), so they opt out of the autouse guard that refuses
# them suite-wide (tests/conftest.py:_guard_cluster_spawn).
@pytest.mark.real_cluster_spawn
class TestSpawnRestart:
    """spawn_restart() launches the `ava-cluster-restart` orchestration session
    through the service session backend, running `ava cluster update --local
    --restart-only` (no pull, no recursive POST); spawn_update(restart_only=True)
    swaps the inner command to the in-process entry with `--restart-only`."""

    def test_spawn_restart_runs_ava_update_restart_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

        result = cluster_mod.spawn_restart("test-origin")
        assert result["session"] == "ava-test-cluster-restart"
        name, cmd, _cwd = spawn_backend.spawn_calls[0]
        assert name == "ava-test-cluster-restart"
        assert "ava cluster update --local --restart-only" in cmd
        assert "git pull" not in cmd

    def test_spawn_restart_rejects_its_active_same_name_even_with_force(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """`--force` skips only the cluster-wide window, never the precise local
        mutex: an active canonical restart session cannot be replaced or killed."""
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        spawn_backend.alive_by_name["ava-test-cluster-restart"] = True

        with pytest.raises(cluster_mod.ClusterUpdateInProgress, match="ava-test-cluster-restart"):
            cluster_mod.spawn_restart("test-origin", force=True)

        assert spawn_backend.spawn_calls == []
        assert spawn_backend.killed == []

    def test_spawn_restart_reuses_canonical_name_after_stale_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """A stale record reads as not alive at the backend boundary, so the
        canonical name is reusable without renaming or deleting a session."""
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        spawn_backend.alive_by_name["ava-test-cluster-restart"] = False

        result = cluster_mod.spawn_restart("test-origin")

        assert result["session"] == "ava-test-cluster-restart"
        assert [call[0] for call in spawn_backend.spawn_calls] == ["ava-test-cluster-restart"]
        assert spawn_backend.killed == []

    def test_spawn_restart_failure_leaves_cleanup_to_backend_and_can_retry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """A declined launch is not license to kill a same-name process. Once
        the backend reports no live session, a retry may reuse the same name."""
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        spawn_backend.alive_by_name["ava-test-cluster-restart"] = False
        spawn_backend.spawn_ok = False

        with pytest.raises(cluster_mod.OrchestrationSpawnFailed):
            cluster_mod.spawn_restart("test-origin")

        assert spawn_backend.killed == []
        spawn_backend.spawn_ok = True
        result = cluster_mod.spawn_restart("test-origin")
        assert result["session"] == "ava-test-cluster-restart"
        assert [call[0] for call in spawn_backend.spawn_calls] == [
            "ava-test-cluster-restart",
            "ava-test-cluster-restart",
        ]

    def test_spawn_restart_rejects_if_rollout_or_updater_alive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        # ava-updater is alive → restart must refuse (mutual exclusion).
        spawn_backend.alive_by_name["ava-test-updater"] = True
        with pytest.raises(cluster_mod.ClusterUpdateInProgress):
            cluster_mod.spawn_restart("test-origin")

    def test_spawn_update_restart_only_skips_pull(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: None)
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

        result = cluster_mod.spawn_update(restart_only=True)
        assert result["session"] == "ava-test-updater"
        cmd = spawn_backend.spawn_calls[0][1]
        # The restart-only bounce runs through the same in-process entry (R1-6):
        # no checkout / uv sync in the shell, quiesce policy spelled out, verdict
        # on [session-exit] rc= (a decline travels as RESTART_DECLINED).
        assert "python -m cli.commands._update_agent_runner --restart-only" in cmd
        assert "--mode smooth" in cmd
        assert "git pull" not in cmd
        assert "uv sync" not in cmd
        assert 'echo "[session-exit] rc=$rc"' in cmd

    def test_spawn_rollout_rejects_if_cluster_restart_alive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """A live ava-cluster-restart session must block a rollout (symmetric
        mutual exclusion — two orchestrations can't fight over the services)."""
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        _patch_update_check_behind(monkeypatch, behind=2)
        spawn_backend.alive_by_name["ava-test-cluster-restart"] = True
        with pytest.raises(cluster_mod.ClusterUpdateInProgress, match="ava-test-cluster-restart"):
            cluster_mod.spawn_rollout("test-origin")


# Subject under test: these call the real ops.cluster spawn entry points (with
# the session backend faked), so they opt out of the autouse guard that refuses
# them suite-wide (tests/conftest.py:_guard_cluster_spawn).
@pytest.mark.real_cluster_spawn
class TestSpawnSessionsResolveAvaFromVenv:
    """Every detached orchestration session must resolve `ava` from this
    checkout's `.venv/bin`, never from a login PATH.

    The regression: on the Mac mini gateway the `ava` entry point lives at
    `~/.local/bin/ava` (the converge symlink), and a detached login shell's
    PATH did not carry that directory — so `ava cluster update` in the rollout
    session died on the first line with `command not found: ava` / `[session-exit]
    rc=127` and the whole cluster rollout was a no-op.
    """

    def _cmd(
        self,
        monkeypatch: pytest.MonkeyPatch,
        spawn_backend: _FakeSessionBackend,
        spawn: Callable[[], object],
    ) -> str:
        """Run a spawn_* entry point with the session backend faked; return the
        shell command it would run."""
        monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: None)
        monkeypatch.setattr("shared.migrations.validate_migrations_at_ref", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        spawn()
        return spawn_backend.spawn_calls[0][1]

    @pytest.mark.parametrize(
        ("label", "spawn", "first_ava_call"),
        [
            ("rollout", partial(cluster_mod.spawn_rollout, "test-origin"), "ava cluster update"),
            (
                "cluster-restart",
                partial(cluster_mod.spawn_restart, "test-origin"),
                "ava cluster update --local --restart-only",
            ),
            ("update", cluster_mod.spawn_update, "python -m cli.commands._update_agent_runner"),
            (
                "update-restart-only",
                partial(cluster_mod.spawn_update, restart_only=True),
                "python -m cli.commands._update_agent_runner",
            ),
        ],
    )
    def test_venv_bin_exported_before_any_ava_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        spawn_backend: _FakeSessionBackend,
        label: str,
        spawn: Callable[[], object],
        first_ava_call: str,
    ) -> None:
        """The venv bin dir is exported onto PATH ahead of every `ava` invocation."""
        from shared.paths import repo_root

        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        _patch_update_check_behind(monkeypatch, behind=2)
        cmd = self._cmd(monkeypatch, spawn_backend, spawn)

        venv_bin = repo_root() / ".venv" / "bin"
        cmd_export = next(
            (part for part in cmd.split(" && ") if part.startswith("export PATH=")), ""
        )
        assert cmd_export.startswith(f"export PATH={venv_bin}:"), (
            f"{label} session does not put the venv first on PATH: {cmd}"
        )
        # `ava start` in spawn_update's else-branch is reached only after the
        # export, so checking the FIRST call is enough to cover both branches.
        assert cmd.index(cmd_export) < cmd.index(first_ava_call), (
            f"{label} session invokes `{first_ava_call}` before exporting the venv PATH"
        )

    def test_rollout_session_resolves_ava_without_local_bin_on_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_backend: _FakeSessionBackend
    ) -> None:
        """End-to-end: execute the rollout's own command line under a shell whose
        PATH lacks `~/.local/bin`, and it still reaches `ava`.

        This is the exact prod condition that produced rc=127. `ava` is stubbed
        into a throwaway venv bin so the shell runs for real without triggering
        an actual cluster update.
        """
        import subprocess as _sp

        import shared.runtime_interpreter as runtime_interpreter_mod

        real_run = _sp.run

        fake_repo = tmp_path / "checkout"
        fake_bin = fake_repo / ".venv" / "bin"
        fake_bin.mkdir(parents=True)
        stub = fake_bin / "ava"
        stub.write_text('#!/bin/sh\necho "STUB-AVA $*"\n')
        stub.chmod(0o755)
        monkeypatch.setattr(runtime_interpreter_mod, "runtime_venv", lambda: fake_repo / ".venv")
        monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
        _patch_update_check_behind(monkeypatch, behind=2)

        cmd = self._cmd(
            monkeypatch, spawn_backend, lambda: cluster_mod.spawn_rollout("cli:regression")
        )
        # A shell with a deliberately minimal PATH — no ~/.local/bin, the prod
        # failure. `sh -lc` (not $SHELL) keeps the assertion portable.
        run = real_run(
            ["/bin/sh", "-lc", cmd],
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert "STUB-AVA cluster update --local --origin cli:regression" in run.stdout, run.stdout
        assert "not found" not in run.stdout
        assert "[session-exit] rc=0" in run.stdout


class TestCurrentOrchestration:
    """current_orchestration() reports the in-flight orchestration from the DB
    (R1 old-signal sweep, PR5: the deployment-state lease's kind while it
    executes, and `update` while this host's updater lease is live)."""

    def _lease(self, kind: Literal["rollout", "restart", "update"]) -> DeployLease:
        return DeployLease(
            holder="m:pid1", held_for_s=60.0, expires_in_s=1740.0, note=None, kind=kind
        )

    def test_none_when_idle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
        monkeypatch.setattr("shared.host_deploy_state.read", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        assert cluster_mod.current_orchestration() is None

    def test_rollout_lease_reports_rollout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: self._lease("rollout"))
        assert cluster_mod.current_orchestration() == "rollout"

    def test_cluster_restart_lease_reports_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: self._lease("restart"))
        assert cluster_mod.current_orchestration() == "restart"

    def test_settle_hold_reads_as_idle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A settle hold executes nothing — the panel must not show an orchestration
        in flight (the hold's own display carries it)."""
        monkeypatch.setattr(
            "shared.cluster_lock.read_update_lease",
            lambda: DeployLease(
                holder="m:pid1",
                held_for_s=60.0,
                expires_in_s=1740.0,
                note="settling, waiting for: win",
                kind="rollout",  # type: ignore[arg-type]
            ),
        )
        monkeypatch.setattr("shared.host_deploy_state.read", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        assert cluster_mod.current_orchestration() is None

    def test_live_updater_lease_reports_update(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The lease-less watchdog-spawned updater: no cluster lease, but this
        host's updater lease is live -> `update` (the old session probe's
        coverage)."""

        from shared.host_deploy_state import HostDeployState

        monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
        monkeypatch.setattr(
            "shared.host_deploy_state.read",
            lambda *_a, **_k: HostDeployState(  # pyright: ignore[reportUnknownArgumentType]
                machine="test",
                posture="converging",
                updated_at=datetime.now(UTC),
                updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=600),
                paused_at=datetime.now(UTC),
            ),
        )
        assert cluster_mod.current_orchestration() == "update"

    def test_stale_converging_row_reads_as_idle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A crashed updater's expired lease is not an orchestration in flight —
        heal paths that refuse on this value still run for stranded hosts (the
        old dead-session reading)."""

        from shared.host_deploy_state import HostDeployState

        monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
        monkeypatch.setattr(
            "shared.host_deploy_state.read",
            lambda *_a, **_k: HostDeployState(  # pyright: ignore[reportUnknownArgumentType]
                machine="test",
                posture="converging",
                updated_at=datetime.now(UTC),
                updater_lease_expires_at=datetime.now(UTC) - timedelta(seconds=60),
                paused_at=datetime.now(UTC) - timedelta(seconds=60),
            ),
        )
        assert cluster_mod.current_orchestration() is None


class TestUpdateCheck:
    """update_check() classifies HEAD..origin/main into behind + frontend/backend."""

    def test_up_to_date_reports_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, ...]] = []

        def _git_ro(*args: str) -> str:
            calls.append(args)
            if args[:2] == ("rev-list", "--count"):
                return "0"
            return ""

        monkeypatch.setattr(update_check_mod, "_git_ro", _git_ro)
        monkeypatch.setattr(update_check_mod, "_get_installed_sha", lambda: "installed-sha")
        monkeypatch.setattr(update_check_mod, "_get_running_sha", lambda: "installed-sha")
        out = cluster_mod.update_check()
        assert out.behind == 0
        assert out.frontend_changed is False
        assert out.backend_changed is False
        assert out.needs_replay is False
        # No diff when already up to date (behind 0 short-circuits).
        assert not any(a[0] == "diff" for a in calls)

    def test_backend_change_classified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _git_ro(*args: str) -> str:
            if args[:2] == ("rev-list", "--count"):
                return "3"
            if args[0] == "diff":
                return "gateway/app.py\nconventions/x.md\n"
            return ""

        monkeypatch.setattr(update_check_mod, "_git_ro", _git_ro)
        monkeypatch.setattr(update_check_mod, "_get_installed_sha", lambda: "installed-sha")
        monkeypatch.setattr(update_check_mod, "_get_running_sha", lambda: "installed-sha")
        out = cluster_mod.update_check()
        assert out.behind == 3
        assert out.backend_changed is True
        assert out.frontend_changed is False

    def test_frontend_only_change_classified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _git_ro(*args: str) -> str:
            if args[:2] == ("rev-list", "--count"):
                return "1"
            if args[0] == "diff":
                return "ui/web/src/app/page.tsx\n"
            return ""

        monkeypatch.setattr(update_check_mod, "_git_ro", _git_ro)
        monkeypatch.setattr(update_check_mod, "_get_installed_sha", lambda: "installed-sha")
        monkeypatch.setattr(update_check_mod, "_get_running_sha", lambda: "installed-sha")
        out = cluster_mod.update_check()
        assert out.behind == 1
        assert out.frontend_changed is True
        assert out.backend_changed is False

    @pytest.mark.parametrize(
        ("installed_sha", "running_sha", "relation", "needs_replay"),
        [
            ("installed-old", "running-new", "behind", False),
            ("installed-new", "running-old", "ahead", True),
        ],
    )
    def test_bookmark_direction_determines_replay(
        self,
        monkeypatch: pytest.MonkeyPatch,
        installed_sha: str,
        running_sha: str,
        relation: Literal["ahead", "behind"],
        needs_replay: bool,
    ) -> None:
        """Only installation ahead of running code names an interrupted rollout."""
        rev_list: list[tuple[str, ...]] = []
        relation_args: list[tuple[str, str, Path | None]] = []

        def _relation(
            pin: str, head: str, *, repo: Path | None = None
        ) -> Literal["ahead", "behind"]:
            relation_args.append((pin, head, repo))
            return relation

        def _git_ro(*args: str) -> str:
            if args[:2] == ("rev-list", "--count"):
                rev_list.append(args)
                return "0"
            return ""

        monkeypatch.setattr(update_check_mod, "_git_ro", _git_ro)
        monkeypatch.setattr(update_check_mod, "_get_installed_sha", lambda: installed_sha)
        monkeypatch.setattr(update_check_mod, "_get_running_sha", lambda: running_sha)
        monkeypatch.setattr("shared.cluster_drift.prod_source_pin_relation", _relation)

        out = cluster_mod.update_check()

        assert out.behind == 0
        assert out.needs_replay is needs_replay
        assert relation_args == [(running_sha, installed_sha, update_check_mod._REPO_ROOT)]
        assert rev_list == [("rev-list", "--count", f"{installed_sha}..origin/main")]


class TestLockHolderLiveness:
    """`_lock_holder_is_live` parses `<machine>:pid<N>` and probes the pid locally."""

    def test_this_machine_dead_pid_is_not_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops import ops_cluster as ops_mod

        monkeypatch.setattr(ops_mod, "machine_name", lambda: "mc")
        monkeypatch.setattr(ops_mod, "process_alive", lambda _pid: False)  # pyright: ignore[reportUnknownArgumentType]
        assert ops_mod._lock_holder_is_live("mc:pid123") is False

    def test_this_machine_alive_pid_is_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops import ops_cluster as ops_mod

        monkeypatch.setattr(ops_mod, "machine_name", lambda: "mc")
        monkeypatch.setattr(ops_mod, "process_alive", lambda _pid: True)  # pyright: ignore[reportUnknownArgumentType]
        assert ops_mod._lock_holder_is_live("mc:pid123") is True

    def test_foreign_machine_holder_is_treated_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops import ops_cluster as ops_mod

        # Can't probe a remote pid — must not clobber another gateway's lock.
        monkeypatch.setattr(ops_mod, "machine_name", lambda: "mc")
        assert ops_mod._lock_holder_is_live("other:pid5") is True

    def test_unparseable_holder_is_treated_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops import ops_cluster as ops_mod

        monkeypatch.setattr(ops_mod, "machine_name", lambda: "mc")
        assert ops_mod._lock_holder_is_live("garbage") is True


class TestClusterRecoverEndpoint:
    """`POST /api/cluster/recover` — operator force-clear of a stranded pause + lock."""

    @staticmethod
    def _lease(holder: str, kind: str | None = "rollout") -> object:
        from shared.cluster_lock import DeployLease

        return DeployLease(
            holder=holder,
            held_for_s=60.0,
            expires_in_s=600.0,
            note=None,
            kind=kind,  # pyright: ignore[reportArgumentType] — test builds the literal
        )

    def test_recovers_when_no_lease_and_no_updater(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops import ops_cluster as ops_mod

        calls: list[str] = []
        monkeypatch.setattr(ops_mod, "read_update_lease", lambda: None)
        monkeypatch.setattr(ops_mod, "updater_lease_live", lambda: False)
        monkeypatch.setattr(ops_mod, "unpause_local_cluster", lambda: calls.append("unpause"))

        def _claim(
            _holder: str, _observed: DeployLease | None, *, ttl_s: float = 60.0
        ) -> RecoveryClaim:
            del ttl_s
            calls.append("claim")
            return RecoveryClaim(acquired=True, previous_holder="stale-holder")

        def _release(_holder: str) -> None:
            calls.append("release")

        monkeypatch.setattr(ops_mod, "claim_recovery_lock", _claim)
        monkeypatch.setattr(
            ops_mod,
            "release_update_lock",
            _release,
        )
        with TestClient(app) as client:
            r = client.post("/api/cluster/recover")
        assert r.status_code == 200
        assert r.json() == {"unlocked_holder": "stale-holder"}
        # Claim the exact observed generation, unpause while recovery owns the
        # lease, then release only that recovery holder.
        assert calls == ["claim", "unpause", "release"]

    def test_recovers_when_holder_is_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A held-but-stale lease (holder pid dead) is force-cleared at once — the
        value-add over the watchdog, which would wait out the TTL. The lease's
        kind alone must not refuse (the 2026-08-12 false refusal)."""
        from ops import ops_cluster as ops_mod

        calls: list[str] = []
        monkeypatch.setattr(ops_mod, "read_update_lease", lambda: self._lease("mc:pid999"))
        monkeypatch.setattr(ops_mod, "_lock_holder_is_live", lambda _h, **_kw: False)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(ops_mod, "updater_lease_live", lambda: False)
        monkeypatch.setattr(ops_mod, "unpause_local_cluster", lambda: calls.append("unpause"))

        def _claim(
            _holder: str, _observed: DeployLease | None, *, ttl_s: float = 60.0
        ) -> RecoveryClaim:
            del ttl_s
            calls.append("claim")
            return RecoveryClaim(acquired=True, previous_holder="mc:pid999")

        def _release(_holder: str) -> None:
            calls.append("release")

        monkeypatch.setattr(ops_mod, "claim_recovery_lock", _claim)
        monkeypatch.setattr(
            ops_mod,
            "release_update_lock",
            _release,
        )
        with TestClient(app) as client:
            r = client.post("/api/cluster/recover")
        assert r.status_code == 200
        assert calls == ["claim", "unpause", "release"]

    def test_refuses_409_when_updater_lease_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The lease-less watchdog-spawned updater is visible only through this
        host's updater lease — a live one must refuse, untouched."""
        from ops import ops_cluster as ops_mod

        calls: list[str] = []
        monkeypatch.setattr(ops_mod, "read_update_lease", lambda: None)
        monkeypatch.setattr(ops_mod, "updater_lease_live", lambda: True)
        monkeypatch.setattr(ops_mod, "unpause_local_cluster", lambda: calls.append("unpause"))

        def _unexpected_claim(
            _holder: str, _observed: DeployLease | None, *, ttl_s: float = 60.0
        ) -> RecoveryClaim:
            del ttl_s
            calls.append("claim")
            return RecoveryClaim(acquired=False)

        monkeypatch.setattr(ops_mod, "claim_recovery_lock", _unexpected_claim)
        with TestClient(app) as client:
            r = client.post("/api/cluster/recover")
        assert r.status_code == 409
        assert "updater lease" in r.json()["detail"]
        assert calls == []

    def test_refuses_409_when_lease_holder_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A lease whose holder process is still running (a live `ava cluster update
        --local` included) must refuse, untouched — the pid-probe is the refusal's
        gate, not the lease's kind."""
        from ops import ops_cluster as ops_mod

        calls: list[str] = []
        monkeypatch.setattr(ops_mod, "read_update_lease", lambda: self._lease("mc:pid123"))
        monkeypatch.setattr(ops_mod, "_lock_holder_is_live", lambda _h, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(ops_mod, "unpause_local_cluster", lambda: calls.append("unpause"))

        def _unexpected_claim(
            _holder: str, _observed: DeployLease | None, *, ttl_s: float = 60.0
        ) -> RecoveryClaim:
            del ttl_s
            calls.append("claim")
            return RecoveryClaim(acquired=False)

        monkeypatch.setattr(ops_mod, "claim_recovery_lock", _unexpected_claim)
        with TestClient(app) as client:
            r = client.post("/api/cluster/recover")
        assert r.status_code == 409
        assert "live process" in r.json()["detail"]
        assert "rollout" in r.json()["detail"]  # the lease's kind still names what runs
        assert calls == []


class TestClusterRolloutEndpoint:
    """`POST /api/cluster/rollout` — gateway-only whole-cluster orchestration trigger."""

    def test_returns_202_and_calls_rollout_op_on_gateway(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        from gateway.routers import cluster as cluster_router
        from ops import ops_cluster as ops_mod

        origins: list[str] = []
        options: list[dict[str, object]] = []
        published: list[tuple[str, str]] = []
        set_machine_identity(role="gateway")

        def _capture_publish(kind: str, origin: str) -> None:
            published.append((kind, origin))

        monkeypatch.setattr(
            cluster_router,
            "_publish_cluster_update_started",
            _capture_publish,
        )
        monkeypatch.setattr(
            ops_mod,
            "spawn_rollout",
            lambda origin, **kwargs: (  # pyright: ignore[reportUnknownArgumentType]
                origins.append(origin)  # pyright: ignore[reportUnknownArgumentType]
                or options.append(cast(dict[str, object], kwargs))
                or {"session": "ava-test-rollout", "log": "/var/log/rollout.log"}
            ),
        )
        with TestClient(app) as client:
            r = client.post("/api/cluster/rollout")
            assert r.status_code == 202
            # body-less POST -> the default origin "user" (the frontend button)
            assert origins == ["user"]
            assert r.json()["session"] == "ava-test-rollout"
            assert "log" in r.json()
            # explicit origin (the SDK path) rides through to the spawn
            r2 = client.post("/api/cluster/rollout", json={"origin": "agent:7"})
            assert r2.status_code == 202
            assert origins == ["user", "agent:7"]
            assert published == [("rollout", "user"), ("rollout", "agent:7")]
            dry = client.post("/api/cluster/rollout", json={"dry_run": True})
            assert dry.status_code == 202
            assert options[-1]["dry_run"] is True
            assert published == [("rollout", "user"), ("rollout", "agent:7")]

    def test_returns_400_on_agent_runner(self, set_machine_identity) -> None:
        set_machine_identity(role="agent-runner")
        with TestClient(app) as client:
            r = client.post("/api/cluster/rollout")
        assert r.status_code == 400
        assert "gateway" in r.json()["detail"]

    def test_returns_409_when_rollout_in_progress(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        from gateway.routers import cluster as cluster_router
        from ops import ops_cluster as ops_mod

        published: list[tuple[str, str]] = []
        set_machine_identity(role="gateway")

        def _capture_publish(kind: str, origin: str) -> None:
            published.append((kind, origin))

        monkeypatch.setattr(
            cluster_router,
            "_publish_cluster_update_started",
            _capture_publish,
        )

        def _raise(_origin: str, **_kw: object) -> dict[str, str]:
            raise cluster_mod.ClusterUpdateInProgress(
                "orchestration session 'ava-test-rollout' already exists"
            )

        monkeypatch.setattr(ops_mod, "spawn_rollout", _raise)
        with TestClient(app) as client:
            r = client.post("/api/cluster/rollout")
        assert r.status_code == 409
        assert "ava-test-rollout" in r.json()["detail"]
        assert published == []

    def test_returns_422_when_already_up_to_date(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        """behind==0 (NothingToUpdate) maps to 422, distinct from the 409 of an
        in-flight update, so a direct POST cannot silently roll an empty cluster."""
        from ops import ops_cluster as ops_mod

        set_machine_identity(role="gateway")

        def _raise(_origin: str, **_kw: object) -> dict[str, str]:
            raise cluster_mod.NothingToUpdate("cluster is already up to date — nothing to roll out")

        monkeypatch.setattr(ops_mod, "spawn_rollout", _raise)
        with TestClient(app) as client:
            r = client.post("/api/cluster/rollout")
        assert r.status_code == 422
        assert "up to date" in r.json()["detail"]

    def test_returns_503_when_the_session_backend_will_not_spawn(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        from ops import ops_cluster as ops_mod

        set_machine_identity(role="gateway")

        def _raise(_origin: str, **_kw: object) -> dict[str, str]:
            raise cluster_mod.OrchestrationSpawnFailed("session backend declined")

        monkeypatch.setattr(ops_mod, "spawn_rollout", _raise)
        with TestClient(app) as client:
            r = client.post("/api/cluster/rollout")
        assert r.status_code == 503
        assert "session" in r.json()["detail"]


class TestClusterRestartEndpoint:
    """`POST /api/cluster/restart` — gateway-only graceful no-pull bounce."""

    def test_returns_202_and_calls_restart_op_on_gateway(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        from gateway.routers import cluster as cluster_router
        from ops import ops_cluster as ops_mod

        origins: list[str] = []
        published: list[tuple[str, str]] = []
        set_machine_identity(role="gateway")

        def _capture_publish(kind: str, origin: str) -> None:
            published.append((kind, origin))

        monkeypatch.setattr(
            cluster_router,
            "_publish_cluster_update_started",
            _capture_publish,
        )
        monkeypatch.setattr(
            ops_mod,
            "spawn_restart",
            lambda origin, **_kw: (  # pyright: ignore[reportUnknownArgumentType]
                origins.append(origin)  # pyright: ignore[reportUnknownArgumentType]
                or {"session": "ava-test-cluster-restart", "log": "/var/log/restart.log"}
            ),
        )
        with TestClient(app) as client:
            r = client.post("/api/cluster/restart")
            assert r.status_code == 202
            assert origins == ["user"]
            assert r.json()["session"] == "ava-test-cluster-restart"
            r2 = client.post("/api/cluster/restart", json={"origin": "agent:7"})
            assert r2.status_code == 202
            assert origins == ["user", "agent:7"]
            assert published == [("restart", "user"), ("restart", "agent:7")]

    def test_returns_400_on_agent_runner(self, set_machine_identity) -> None:
        set_machine_identity(role="agent-runner")
        with TestClient(app) as client:
            r = client.post("/api/cluster/restart")
        assert r.status_code == 400
        assert "gateway" in r.json()["detail"]

    def test_returns_409_when_in_progress(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        from ops import ops_cluster as ops_mod

        set_machine_identity(role="gateway")

        def _raise(_origin: str, **_kw: object) -> dict[str, str]:
            raise cluster_mod.ClusterUpdateInProgress(
                "orchestration session 'ava-test-cluster-restart' exists"
            )

        monkeypatch.setattr(ops_mod, "spawn_restart", _raise)
        with TestClient(app) as client:
            r = client.post("/api/cluster/restart")
        assert r.status_code == 409


class TestClusterUpdateCheckEndpoint:
    """`GET /api/cluster/update-check` — read-only preflight, gateway only."""

    def test_returns_check_on_gateway(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        from ops import ops_cluster as ops_mod

        set_machine_identity(role="gateway")
        monkeypatch.setattr(
            ops_mod,
            "update_check",
            lambda: cluster_mod.UpdateCheck(
                behind=2, frontend_changed=False, backend_changed=True, needs_replay=False
            ),
        )
        with TestClient(app) as client:
            r = client.get("/api/cluster/update-check")
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "behind": 2,
            "frontend_changed": False,
            "backend_changed": True,
            "needs_replay": False,
        }

    def test_returns_400_on_agent_runner(self, set_machine_identity) -> None:
        set_machine_identity(role="agent-runner")
        with TestClient(app) as client:
            r = client.get("/api/cluster/update-check")
        assert r.status_code == 400


class TestStatusSnapshot:
    def test_snapshot_reflects_flag_and_role(self, fake_flag: Path, set_machine_identity) -> None:
        set_machine_identity(role="agent-runner", name="wsl")
        # unpaused
        snap = cluster_mod.status_snapshot()
        assert snap.machine_name == "wsl"
        assert snap.serve_gateway is False
        assert snap.serve_agent_runner is True
        assert snap.paused is False
        # paused
        fake_flag.write_text("")
        snap2 = cluster_mod.status_snapshot()
        assert snap2.paused is True

    def test_snapshot_includes_head_sha(
        self,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """status_snapshot threads the prod-source HEAD so the roster can compare
        it against the cluster pin."""
        set_machine_identity(role="agent-runner", name="wsl")
        monkeypatch.setattr("shared.cluster_drift.prod_source_head_sha", lambda: "abc1234")
        assert cluster_mod.status_snapshot().head_sha == "abc1234"

    def test_snapshot_includes_running_sha(
        self,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """status_snapshot threads the commit the answering process froze at its
        own boot — distinct from head_sha (the checkout the pin verdict compares)
        so the roster can expose a node running stale code even when its checkout
        reads on-pin."""
        set_machine_identity(role="agent-runner", name="wsl")
        monkeypatch.setattr("shared.process_sha.get", lambda: "def5678")
        assert cluster_mod.status_snapshot().running_sha == "def5678"

    def test_snapshot_ignores_the_start_bookmark(
        self,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A start that restarted nothing must not be able to make this node look
        current.

        `ava start` writes `$AVA_HOME/running_sha` from the fresh HEAD one line
        before a launcher that skips already-running sessions, so after a
        no-op start the bookmark and the checkout agree while the live processes
        still hold the old code. Sourcing the snapshot from the bookmark made
        that state indistinguishable from a real restart — every roster consumer
        saw running_sha == head_sha for a node on days-old code. The snapshot
        answers from the process, so the stale commit survives the bookmark's
        advance and the divergence every drift renderer keys on is there."""
        set_machine_identity(role="agent-runner", name="wsl")
        monkeypatch.setattr("shared.process_sha.get", lambda: "0ld0ld0aaaa")
        monkeypatch.setattr("shared.running_sha.get", lambda: "n3wn3w0bbbb")
        monkeypatch.setattr("shared.cluster_drift.prod_source_head_sha", lambda: "n3wn3w0bbbb")

        snap = cluster_mod.status_snapshot()

        assert snap.running_sha == "0ld0ld0aaaa"
        assert snap.head_sha == "n3wn3w0bbbb"
        assert snap.running_sha != snap.head_sha

    def test_watchdog_online_requires_only_this_capabilitys_watchdog(
        self,
        fake_flag: Path,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`watchdog_online` (single bool for the frontend dot) now means "every
        watchdog this host should run is alive". A split agent-runner has only the
        agent-runner watchdog — the gateway one is not its concern — so a dead
        gateway-watchdog pidfile must NOT drag the dot to offline."""
        from shared.config import settings

        del fake_flag  # unpaused
        set_machine_identity(role="agent-runner", name="wsl")

        def _fake_check(path: str) -> tuple[bool, int | None]:
            # The agent-runner watchdog is alive; the gateway one is "dead" (a
            # split agent-runner never runs it) and must be ignored.
            if path == str(settings.services.agent_runner_watchdog_pidfile):
                return True, 123
            return False, None

        monkeypatch.setattr(cluster_status, "_check_pidfile", _fake_check)
        assert cluster_mod.status_snapshot().watchdog_online is True

    def test_watchdog_online_single_box_requires_both(
        self,
        fake_flag: Path,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A single-box host runs BOTH watchdogs, so the dot is online only when
        both pidfiles are alive."""
        from shared.config import settings

        del fake_flag  # unpaused
        set_machine_identity(role="gateway,agent-runner", name="test-host")

        alive = {
            str(settings.services.gateway_watchdog_pidfile),
            str(settings.services.agent_runner_watchdog_pidfile),
        }

        def _fake_check(path: str) -> tuple[bool, int | None]:
            return (path in alive, 123 if path in alive else None)

        monkeypatch.setattr(cluster_status, "_check_pidfile", _fake_check)
        assert cluster_mod.status_snapshot().watchdog_online is True

        # gateway watchdog dies → the single-box dot goes offline.
        alive.discard(str(settings.services.gateway_watchdog_pidfile))
        assert cluster_mod.status_snapshot().watchdog_online is False


# ─── /api/cluster/stop + update endpoints via TestClient ─────────────────────


class TestClusterEndpoints:
    def test_post_stop_dispatches_cluster_stop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        """POST /api/cluster/stop forwards a cluster_stop op to this host's own
        ops server — the gateway router never touches the flag/session itself."""
        from gateway.routers import cluster as cluster_router

        set_machine_identity(role="gateway", name="test-host")
        dispatched: list[dict] = []

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            dispatched.append({"target_machine": target_machine, "kind": kind, "payload": payload})  # pyright: ignore[reportUnknownMemberType]
            return {}

        monkeypatch.setattr(cluster_router._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post(
                "/api/cluster/stop",
                json={"deploy_holder": "g:pid1", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
            )
        assert r.status_code == 200
        assert r.json() == {"paused": True}
        assert dispatched == [
            {
                "target_machine": "test-host",
                "kind": "cluster_stop",
                "payload": {
                    "deploy_holder": "g:pid1",
                    "deploy_acquired_at": "2026-08-25T00:00:00Z",
                },
            }
        ]

    def test_post_resume_dispatches_cluster_resume(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        """POST /api/cluster/resume forwards a cluster_resume op to this host's
        own ops server, symmetric with /stop."""
        from gateway.routers import cluster as cluster_router

        set_machine_identity(role="gateway", name="test-host")
        dispatched: list[dict] = []

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            dispatched.append({"target_machine": target_machine, "kind": kind, "payload": payload})  # pyright: ignore[reportUnknownMemberType]
            return {}

        monkeypatch.setattr(cluster_router._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post(
                "/api/cluster/resume",
                json={"deploy_holder": "g:pid1", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
            )
        assert r.status_code == 200
        assert r.json() == {"paused": False}
        assert dispatched == [
            {
                "target_machine": "test-host",
                "kind": "cluster_resume",
                "payload": {
                    "deploy_holder": "g:pid1",
                    "deploy_acquired_at": "2026-08-25T00:00:00Z",
                },
            }
        ]

    def test_post_resume_never_mints_a_capability_from_current_state(self) -> None:
        with TestClient(app) as client:
            r = client.post("/api/cluster/resume")
        assert r.status_code == 422

    def test_transition_capability_requires_an_rfc3339_offset(self) -> None:
        with TestClient(app) as client:
            r = client.post(
                "/api/cluster/resume",
                json={
                    "deploy_holder": "g:pid1",
                    "deploy_acquired_at": "2026-08-25T00:00:00",
                },
            )
        assert r.status_code == 422

    def test_post_stopping_marks_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """POST /api/cluster/stopping?machine=<name>&home=<home> retracts that unit."""
        marked: list[tuple[str, str]] = []
        from ops import ops_cluster as ops_mod

        monkeypatch.setattr(
            ops_mod,
            "mark_stopping",
            lambda name, home: marked.append((name, home)),  # pyright: ignore[reportUnknownArgumentType]
        )
        with TestClient(app) as client:
            r = client.post("/api/cluster/stopping", params={"machine": "wsl", "home": "~/.ava"})
        assert r.status_code == 200
        assert r.json() == {"machine": "wsl"}
        assert marked == [("wsl", "~/.ava")]

    def test_post_update_returns_202_with_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        from gateway.routers import cluster as cluster_router

        set_machine_identity(role="gateway", name="test-host")

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            return {"session": "ava-test-updater", "log": "/var/log/updater-123.log"}

        monkeypatch.setattr(cluster_router._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post("/api/cluster/update")
        assert r.status_code == 202
        assert r.json()["session"] == "ava-test-updater"
        assert "log" in r.json()


class TestClusterUpdateRouting:
    """`POST /api/cluster/update` dispatches a cluster_update op to the target's
    ops server — this host included (no in-process spawn shortcut)."""

    def _capture_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        from gateway.routers import cluster as cluster_router

        dispatched: list[dict] = []

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            dispatched.append({"target_machine": target_machine, "kind": kind, "payload": payload})  # pyright: ignore[reportUnknownMemberType]
            return {"session": "ava-test-updater", "log": "/var/log/ava-test.log"}

        monkeypatch.setattr(cluster_router._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        return dispatched

    def test_no_target_dispatches_to_self(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        """No target param → treated as self → op dispatched to this host's own
        ops server."""
        set_machine_identity(role="gateway", name="test-host")
        dispatched = self._capture_dispatch(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        with TestClient(app) as client:
            r = client.post("/api/cluster/update")
        assert r.status_code == 202
        assert dispatched[0]["target_machine"] == "test-host"
        assert dispatched[0]["kind"] == "cluster_update"
        assert r.json()["session"] == "ava-test-updater"

    def test_target_self_dispatches_to_self(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        """target=<own machine name> → same as no target → dispatched to this
        host's own ops server."""
        set_machine_identity(role="gateway", name="test-host")
        dispatched = self._capture_dispatch(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        with TestClient(app) as client:
            r = client.post("/api/cluster/update?target=test-host")
        assert r.status_code == 202
        assert dispatched[0]["target_machine"] == "test-host"

    def test_target_remote_dispatches(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        """target=<other machine> → POST cluster_update to its ops server, return its result."""
        from gateway.routers import cluster as cluster_router

        set_machine_identity(role="gateway", name="test-host")

        dispatched: list[dict] = []

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            dispatched.append({"target_machine": target_machine, "kind": kind, "payload": payload})  # pyright: ignore[reportUnknownMemberType]
            return {"session": "ava-main-updater", "log": "/x"}

        monkeypatch.setattr(cluster_router._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post("/api/cluster/update?target=cloud")
        assert r.status_code == 202
        assert r.json() == {"session": "ava-main-updater", "log": "/x"}
        assert dispatched[0]["target_machine"] == "cloud"
        assert dispatched[0]["kind"] == "cluster_update"


# ─── admin events query ──────────────────────────────────────────────────────


@pytest.fixture
def fake_admin_events(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Patch loki_events.query_events for the admin-events route; record the
    kwargs and return canned rows."""

    calls: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    def _query(**kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        calls.append(kwargs)
        return rows, False

    monkeypatch.setattr(loki_events, "query_events", _query)
    return {"calls": calls, "rows": rows}


def _row(
    *, msg: str = "hi", level: str = "info", event: str = "log", agent_id: int | None = None
) -> dict[str, Any]:
    return {
        "id": 1,
        "ts": datetime(2026, 8, 12, tzinfo=UTC),
        "agent_id": agent_id,
        "machine": "machine-1",
        "process": "gateway",
        "category": "telemetry",
        "event_name": event,
        "level": level,
        "source": "test",
        "target_agent_id": None,
        "attributes": {"msg": msg},
    }


class TestAdminEvents:
    """`GET /api/cluster/admin/events` slices the unified event stream from
    Loki for ops debugging without SSH (task #1197)."""

    def test_returns_newest_first(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        fake_admin_events["rows"].extend([_row(msg="oldest"), _row(msg="newest", agent_id=1)])
        with TestClient(app) as client:
            r = client.get("/api/cluster/admin/events?limit=10")
        assert r.status_code == 200
        items = r.json()["items"]
        assert [i["payload"]["msg"] for i in items] == ["oldest", "newest"]
        # wire shape: id / ts / agent_id / level / event / payload
        assert set(items[0]) == {"id", "ts", "agent_id", "level", "event", "payload"}

    def test_telemetry_and_log_categories_only(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        """Old PG contract: category IN (telemetry, log) — audit rows (spawn /
        send_message / ...) stay out of the ops log slice. The route passes the
        category set to Loki."""
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events")
        assert fake_admin_events["calls"][0]["categories"] == ["telemetry", "log"]

    def test_filter_agent_id(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?agent_id=3")
        kw = fake_admin_events["calls"][0]
        assert kw["agent_id"] == 3
        assert kw["service_only"] is False

    def test_filter_service_only(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?service_only=true")
        kw = fake_admin_events["calls"][0]
        assert kw["service_only"] is True
        assert kw["agent_id"] is None

    def test_rejects_agent_id_with_service_only(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            r = client.get("/api/cluster/admin/events?agent_id=1&service_only=true")
        assert r.status_code == 400
        assert fake_admin_events["calls"] == []

    def test_filter_level_threshold(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        # level is a MINIMUM threshold (warning -> warning|error|critical)
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?level=WARNING")
        kw = fake_admin_events["calls"][0]
        assert kw["level_min"] == "warning"
        assert "level" not in kw

    def test_rejects_invalid_level(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            r = client.get("/api/cluster/admin/events?level=NOPE")
        assert r.status_code == 400
        assert fake_admin_events["calls"] == []

    def test_filter_since_relative(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        before = datetime.now(UTC)
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?since=1m")
        after = datetime.now(UTC)
        from_ = fake_admin_events["calls"][0]["from_"]
        assert from_ is not None
        # cutoff = now - 60s, on the Python clock
        assert before - timedelta(seconds=70) <= from_ <= after - timedelta(seconds=50)

    def test_filter_since_absolute(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?since=2026-08-01T00:00:00Z")
        assert fake_admin_events["calls"][0]["from_"] == datetime(2026, 8, 1, tzinfo=UTC)

    def test_rejects_invalid_since(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            r = client.get("/api/cluster/admin/events?since=garbage")
        assert r.status_code == 400
        assert fake_admin_events["calls"] == []

    def test_filter_event_multi(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?event=spawn,terminate")
        assert fake_admin_events["calls"][0]["event_names"] == ["spawn", "terminate"]

    def test_filter_grep(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?grep=hello")
        assert fake_admin_events["calls"][0]["grep"] == "hello"

    def test_limit_defaults_to_200(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events")
        assert fake_admin_events["calls"][0]["limit"] == 200

    def test_limit_caps_at_1000(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            r = client.get("/api/cluster/admin/events?limit=1001")
        assert r.status_code == 400
        assert fake_admin_events["calls"] == []

    def test_no_since_uses_default_window(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        # no since -> from_ None -> query_events' 24h lower bound applies
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events")
        assert fake_admin_events["calls"][0]["from_"] is None


# ─── admin: DELETE /api/cluster/machines/{name} ──────────────────────────────


class TestAdminMachineDelete:
    def test_deletes_existing_row(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        set_machine_identity(role="gateway", name="cloud")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("DELETE FROM machines WHERE name = 'laminar-stale'")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('laminar-stale', ARRAY['gateway'], 'https://example.com')"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        with TestClient(app) as client:
            r = client.delete("/api/cluster/machines/laminar-stale")
        assert r.status_code == 200
        assert r.json() == {"deleted": True}
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("SELECT COUNT(*) FROM machines WHERE name = 'laminar-stale'")  # pyright: ignore[reportUnknownMemberType]
            (n,) = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
        assert n == 0

    def test_missing_row_returns_deleted_false(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        set_machine_identity(role="gateway", name="cloud")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("DELETE FROM machines WHERE name = 'never-existed'")  # pyright: ignore[reportUnknownMemberType]
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        with TestClient(app) as client:
            r = client.delete("/api/cluster/machines/never-existed")
        assert r.status_code == 200
        assert r.json() == {"deleted": False}

    def test_refuses_to_delete_self(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        set_machine_identity(role="gateway", name="cloud")
        with TestClient(app) as client:
            r = client.delete("/api/cluster/machines/cloud")
        assert r.status_code == 400
        assert "refusing" in r.json()["detail"]


# ─── agent roster: GET /api/cluster/machines ─────────────────────────────────


class TestAgentMachineList:
    def test_get_cluster_machines_returns_name_description_live(
        self,
        db_conn,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        # Seed one LOCAL agent-runner row. The local machine is probed through
        # its own ops server like any other (status_probe), so stub the op
        # dispatch; the row survives the agent-view filter (only agent-runner
        # machines run agents).
        from ops import cluster_rpc

        set_machine_identity(role="agent-runner", name="wsl-test")

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            ops_url=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            assert kind == "status_probe"
            assert ops_url == "http://wsl-test:18121"
            # The ops server echoes its own machine_name; the gateway verifies it
            # matches the probed row, so the stub must self-report the same name.
            return {
                "machine_name": "wsl-test",
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
            }

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url, description) "
                "VALUES ('wsl-test', ARRAY['agent-runner'], "
                "'http://wsl-test:18121', 'voice IO + browser')"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        with TestClient(app) as client:
            r = client.get("/api/cluster/machines")
        assert r.status_code == 200
        body = r.json()
        assert body == [
            {
                "name": "wsl-test",
                "description": "voice IO + browser",
                "live": True,
                "is_staging": False,
            }
        ]

    def test_get_cluster_machines_reachable_unknown_is_not_live(
        self,
        db_conn,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        """The agent/config projection must not target a runner whose ops
        server answered but could not provide a determinate status."""
        from datetime import UTC, datetime

        from gateway.routers import cluster as cluster_router
        from gateway.schemas import MachineStatus

        set_machine_identity(role="gateway", name="cloud-test")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('wsl-test', ARRAY['agent-runner'], 'http://wsl-test:18121')"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        now = datetime.now(UTC)

        async def _fake_gather(rows, local_name, **_kw):  # type: ignore[no-untyped-def]
            return [
                MachineStatus(
                    name="wsl-test",
                    serve_gateway=False,
                    serve_agent_runner=True,
                    gateway_url="http://wsl-test:18121",
                    up_since_at=now,
                    online=True,
                    paused=None,
                )
            ]

        monkeypatch.setattr(cluster_router, "gather_cluster_status", _fake_gather)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            response = client.get("/api/cluster/machines")

        assert response.status_code == 200
        assert response.json() == [
            {
                "name": "wsl-test",
                "description": None,
                "live": False,
                "is_staging": False,
            }
        ]

    def test_get_cluster_machines_excludes_gateway(
        self,
        db_conn,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        """The agent view (`/api/cluster/machines`) lists only machines that run
        agent processes — the gateway (which runs none) is filtered out.

        gather_cluster_status is stubbed so the filter is exercised in isolation,
        without a real status_probe round-trip (no live runner in tests). One DB
        row is seeded so the handler does not short-circuit on an empty roster
        before reaching the stub."""
        from datetime import UTC, datetime

        from gateway.routers import cluster as cluster_router
        from gateway.schemas import MachineStatus

        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('control-test', ARRAY['gateway'], 'https://example.com'), "
                "('wsl-test', ARRAY['agent-runner'], NULL)"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]

        now = datetime.now(UTC)

        async def _fake_gather(rows, local_name, *, cluster_target_sha=None):  # type: ignore[no-untyped-def]
            return [
                MachineStatus(
                    name="control-test",
                    serve_gateway=True,
                    serve_agent_runner=False,
                    gateway_url="https://example.com",
                    up_since_at=now,
                    online=True,
                    paused=False,
                    description="ops gateway",
                    stopped_at=None,
                ),
                MachineStatus(
                    name="wsl-test",
                    serve_gateway=False,
                    serve_agent_runner=True,
                    gateway_url="",
                    up_since_at=now,
                    online=True,
                    paused=False,
                    description="voice IO + browser",
                    stopped_at=None,
                ),
            ]

        monkeypatch.setattr(cluster_router, "gather_cluster_status", _fake_gather)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.get("/api/cluster/machines")
        assert r.status_code == 200
        body = r.json()
        assert body == [
            {
                "name": "wsl-test",
                "description": "voice IO + browser",
                "live": True,
                "is_staging": False,
            }
        ]
        assert all(m["name"] != "control-test" for m in body)

    def test_set_machine_staging_flips_flag_and_excludes_from_roster_targets(
        self, db_conn, set_machine_identity
    ) -> None:  # type: ignore[no-untyped-def]
        """POST /api/cluster/machines/{name}/staging flips the operator staging
        flag; a flagged row is still served on the roster (visible) but
        `list_agent_runners`-backed endpoints exclude it. Unknown name → 404."""
        set_machine_identity(role="gateway", name="test-host")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('stage', ARRAY['agent-runner'], NULL)"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]

        with TestClient(app) as client:
            r = client.post("/api/cluster/machines/stage/staging", json={"is_staging": True})
            assert r.status_code == 200
            assert r.json() == {"deleted": True}

            # unknown machine → 404
            r = client.post("/api/cluster/machines/ghost/staging", json={"is_staging": True})
            assert r.status_code == 404

            # roster still serves the row (staging is visible), with the flag set
            r = client.get("/api/cluster/roster")
            assert r.status_code == 200
            stage_row = next(m for m in r.json() if m["name"] == "stage")
            assert stage_row["is_staging"] is True

            # unmark restores the normal target posture
            r = client.post("/api/cluster/machines/stage/staging", json={"is_staging": False})
            assert r.status_code == 200
            r = client.get("/api/cluster/roster")
            stage_row = next(m for m in r.json() if m["name"] == "stage")
            assert stage_row["is_staging"] is False

    def test_get_cluster_roster_returns_full_status(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        """`/api/cluster/roster` returns the full MachineStatus rows (name/role/
        online/paused), backing the thin `ava cluster status`."""
        set_machine_identity(role="gateway", name="test-host")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('test-host', ARRAY['gateway'], 'https://example.com')"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        with TestClient(app) as client:
            r = client.get("/api/cluster/roster")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["name"] == "test-host"
        assert body[0]["serve_gateway"] is True
        assert body[0]["serve_agent_runner"] is False
        assert body[0]["online"] is True
        assert "stopped_at" in body[0]


# ─── machines pause / resume (Task #1283) ────────────────────────────────────


def _seed_away_machine(
    db_conn: psycopg.Connection, *, name: str = "away", local_row: bool = True
) -> None:
    """Machines rows for the pause tests, isolated from other tests' rows: the
    paused target (`name`, agent-runner) plus, when `local_row`, this gateway
    host's own row (gateway-only in the DB — the roster's local lightweight
    path, so no probe dial; the test identity itself carries agent-runner so
    spawn_agent works)."""
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
        if local_row:
            cur.execute(
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('test-host', ARRAY['gateway'], NULL)"
            )
        cur.execute(
            "INSERT INTO machines (name, role, gateway_url) "
            "VALUES (%s, ARRAY['agent-runner'], NULL)",
            (name,),
        )
    db_conn.commit()


def _seed_agent_on_machine(
    db_conn: psycopg.Connection, machine: str, *, status: str = "idling"
) -> int:
    """One live agent row homed on `machine` (test spawn helper + machine
    stamp; the row-creation path moved gateway-side, Task #1236 follow-up)."""
    from tests.conftest import spawn_agent

    aid = spawn_agent(spawner="user")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET machine = %s, status = %s WHERE id = %s",
            (machine, status, aid),
        )
    db_conn.commit()
    return aid


def _seed_drain_owner(db_conn: psycopg.Connection, agent_id: int = 405) -> None:
    """The drain-owner agent row the pause endpoint reassigns tasks to (the
    FK target `agent_tasks.owner -> agents(id)`)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (agent_id,),
        )
    db_conn.commit()


def _seed_in_progress_task(db_conn: psycopg.Connection, owner: int, title: str) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, owner, created_by) "
            "VALUES (%s, 'desc', 'in_progress', %s, 'user') RETURNING id",
            (title, owner),
        )
        row = cur.fetchone()
        assert row is not None
        (task_id,) = row
    db_conn.commit()
    return task_id


class TestMachinePauseResume:
    def test_pause_drains_terminates_and_hides_from_roster(
        self, db_conn, set_machine_identity
    ) -> None:  # type: ignore[no-untyped-def]
        """The full pause contract: tasks of the machine's live agents are
        drained to #405 with a note, every agent is terminated (graceful via
        the in-process lifecycle stub), and the machine vanishes from the
        roster + agent machine list. The row keeps its registration info."""
        # identity carries agent-runner so spawn_agent works; the DB row for
        # the local host stays gateway-only (roster's local lightweight path)
        set_machine_identity(role="agent-runner", name="test-host")
        _seed_away_machine(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _seed_drain_owner(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        aid = _seed_agent_on_machine(db_conn, "away")  # pyright: ignore[reportUnknownArgumentType]
        _seed_agent_on_machine(db_conn, "away")  # pyright: ignore[reportUnknownArgumentType]
        _seed_in_progress_task(db_conn, aid, "task-on-away")  # pyright: ignore[reportUnknownArgumentType]

        with TestClient(app) as client:
            r = client.post(
                "/api/cluster/machines/away/pause", json={"reason": "\u4f11\u5047\u4e00\u5468"}
            )
        assert r.status_code == 200
        body = r.json()
        assert body["paused"] is True
        assert body["terminated_agents"] == 2
        assert body["force_marked_agents"] == 0
        assert body["reassigned_tasks"] == 1
        assert body["pause_reason"] == "\u4f11\u5047\u4e00\u5468"
        assert body["paused_at"] is not None

        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "SELECT COUNT(*) FROM agents_meta WHERE machine = 'away' AND status != 'terminated'"
            )
            (n_live,) = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "SELECT owner, results FROM agent_tasks WHERE title = 'task-on-away'"
            )
            owner, results = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "SELECT gateway_url, role FROM machines WHERE name = 'away'"
            )
            gateway_url, role = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
        assert n_live == 0
        assert owner == 405
        assert "machine pause" in results or "paused" in results
        assert gateway_url is None and role == ["agent-runner"]  # registration kept

        # roster + agent machine list hide the paused machine — the cluster
        # shows only its active members (the gateway host itself)
        with TestClient(app) as client:
            roster = client.get("/api/cluster/roster").json()
            machines_list = client.get("/api/cluster/machines").json()
        assert [m["name"] for m in roster] == ["test-host"]
        assert machines_list == []

    def test_pause_already_paused_is_idempotent(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        """Re-pausing an already-paused machine is a safe no-op: nothing left
        to drain/terminate, the original latch values are returned."""
        set_machine_identity(role="gateway", name="test-host")
        _seed_away_machine(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            first = client.post("/api/cluster/machines/away/pause", json={"reason": "once"})
            second = client.post("/api/cluster/machines/away/pause", json={"reason": "twice"})
        assert first.status_code == 200 and second.status_code == 200
        assert first.json()["paused_at"] == second.json()["paused_at"]
        assert second.json()["pause_reason"] == "once"  # first reason preserved
        assert second.json()["terminated_agents"] == 0

    def test_pause_force_marks_when_ops_unreachable(
        self,
        db_conn: psycopg.Connection,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        """A machine whose ops server cannot take the graceful terminate (already
        unreachable) gets its agent rows force-marked terminated in the shared
        DB — pause must not leave agents 'running' on a machine that is leaving."""
        from gateway.routers import agents_forward as _fwd

        set_machine_identity(role="agent-runner", name="test-host")
        _seed_away_machine(db_conn)
        aid = _seed_agent_on_machine(db_conn, "away")
        from shared.db import insert_inbound_message

        old_chat_id = insert_inbound_message(db_conn, aid, "queued before pause", source="user")

        async def _unreachable(target: str, path: str, json_body: dict) -> dict:
            raise RuntimeError("ops server unreachable")

        monkeypatch.setattr(_fwd, "_enqueue_lifecycle", _unreachable)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post("/api/cluster/machines/away/pause", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["terminated_agents"] == 0
        assert body["force_marked_agents"] == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, termination_source, last_force_terminate_inbound_id "
                "FROM agents_meta WHERE machine = 'away'"
            )
            status_row = cur.fetchone()
            assert status_row is not None
            status, source, fence_id = status_row
            cur.execute(
                "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='terminate' "
                "ORDER BY id DESC LIMIT 1",
                (aid,),
            )
            terminate_row = cur.fetchone()
            assert terminate_row is not None
            terminate_id = terminate_row[0]
        assert status == "terminated"
        assert source == "user"
        assert old_chat_id < fence_id == terminate_id

        from psycopg_pool import ConnectionPool

        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending
        from shared.config import settings

        with ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2) as pool:
            assert select_terminated_owners_with_pending(cast(ConnectionPool, pool)) == []

    def test_pause_unknown_machine_404(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        set_machine_identity(role="gateway", name="test-host")
        with TestClient(app) as client:
            r = client.post("/api/cluster/machines/ghost/pause", json={})
        assert r.status_code == 404

    def test_pause_refuses_gateway_own_machine(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        """Pausing the gateway host itself is refused — the cluster needs its
        gateway member online to answer anything."""
        set_machine_identity(role="gateway", name="test-host")
        _seed_away_machine(db_conn, name="test-host", local_row=False)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post("/api/cluster/machines/test-host/pause", json={})
        assert r.status_code == 400
        assert "refusing" in r.json()["detail"]

    def test_resume_restores_roster_and_is_idempotent(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        """Resume clears the latch: the machine is served on the roster and the
        agent machine list again; resuming a not-paused machine is a no-op
        (resumed=False)."""
        set_machine_identity(role="gateway", name="test-host")
        _seed_away_machine(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            client.post("/api/cluster/machines/away/pause", json={})
            r = client.post("/api/cluster/machines/away/resume", json={})
            again = client.post("/api/cluster/machines/away/resume", json={})
        assert r.status_code == 200
        assert r.json() == {"name": "away", "resumed": True}
        assert again.json() == {"name": "away", "resumed": False}

        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "SELECT paused_at, pause_reason FROM machines WHERE name = 'away'"
            )
            paused_at, pause_reason = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
        assert paused_at is None and pause_reason is None

        with TestClient(app) as client:
            roster = client.get("/api/cluster/roster").json()
            machines_list = client.get("/api/cluster/machines").json()
        assert "away" in [m["name"] for m in roster]
        assert "away" in [m["name"] for m in machines_list]

    def test_resume_unknown_machine_404(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        set_machine_identity(role="gateway", name="test-host")
        with TestClient(app) as client:
            r = client.post("/api/cluster/machines/ghost/resume", json={})
        assert r.status_code == 404
