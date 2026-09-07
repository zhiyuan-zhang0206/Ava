"""tests/e2e/ pytest fixtures — cross-process happy path orchestration.

Fixture dependency:
- e2e_env (function): depends on spawned_agent + playwright_page + frontend_proc
- spawned_agent (function): depends on gateway_proc + truncated_db
- gateway_proc (function): depends on scenario_env (env must be set first)
- package-scoped: _e2e_process_env / e2e_db / playwright_runtime / playwright_browser —
  package, not session, because `_e2e_process_env` mutates process-global env and its
  restore must fire when pytest leaves this directory rather than at end-of-session,
  and playwright_runtime's main-thread asyncio loop must close the same way (see their
  docstrings)
- session-scoped: frontend_proc

env injection chain:
    pytest sets os.environ → gateway subprocess inherits → gateway internally spawns
    agent subprocess inherits → agent build_chat_model reads AVA_LLM_OVERRIDE.
    gateway must be function-scoped——session-scoped start once then env is fixed,
    subsequent setenv cannot get into agent subprocess.

Concurrency isolation: DB/Redis reuses the throwaway Postgres + Redis started per worker in the top-level conftest
(`_provisioned_db` / `_provisioned_redis`, URLs already in settings + env). This conftest
adds another layer of per-session suffix `<PID>_<TS>` to isolate remaining process-level resources:
- Redis channel `ava:events:e2e:<suffix>`
- AVA_HOME `tmp/ava_e2e_home_<suffix>` (transcripts/plugins)
- gateway/frontend ports (kernel-assigned, see `_ports.py`)
- agents_id_seq PID offset (session names don't collide across workers)
ensuring multiple local e2e sessions (dev agents + CI runner) truly run concurrently without interference.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import psutil
import psycopg
import pytest
from _pytest.reports import TestReport
from langgraph.checkpoint.postgres import PostgresSaver
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from psycopg import sql

from shared.agents import AgentStatus
from shared.config import settings
from tests.e2e._env import E2EEnv
from tests.e2e._ports import FRONTEND_PORT, FRONTEND_URL, GATEWAY_PORT, GATEWAY_SOCKET, GATEWAY_URL
from tests.e2e._proc import (
    listener_evidence,
    managed_proc,
    proc_log_tail,
    require_native_listener,
    sweep_stale_e2e_processes,
    wait_for_port,
)

# ---- module-level overrides (run after top-level conftest) ---------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_previous_gateway: subprocess.Popen[str] | None = None


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip the whole e2e package up front when its prerequisites are missing.

    The skip is a collection-time MARKER, not a `pytest.skip()` thrown from a
    fixture: a fixture-stage skip still instantiates the session/package
    fixtures the test depends on (playwright_runtime opens a main-thread
    asyncio loop that then lives until the package teardown), while a marker
    skips the item before any fixture runs. This keeps a missing-node_modules
    local run from polluting later async tests (see playwright_runtime).
    """
    # Reap residue of dead e2e runs BEFORE anything in this session starts. The
    # sweep must also fire when prerequisites are missing (collection-time
    # skip): a crashed run's stack is exactly what most needs reaping, and a
    # node_modules-less session may still be the one that would otherwise never
    # get a chance to clean it.
    with contextlib.suppress(Exception):  # a cleanup failure must not abort collection
        sweep_stale_e2e_processes()
    if shutil.which("npm") and (_REPO_ROOT / "ui" / "web" / "node_modules" / "next").exists():
        return
    for item in items:
        if item.nodeid.startswith("tests/e2e/"):
            item.add_marker(
                pytest.mark.skip(reason="e2e prerequisites missing (npm or ui/web/node_modules)")
            )


# Suffix: PID + microsecond timestamp. e2e reuses the root conftest's session
# native Postgres + Redis (provisioned by its autouse fixtures); this suffix
# only scopes the per-session AVA_HOME and the Redis channel names so
# concurrent e2e workers don't collide.
_E2E_SUFFIX = f"{os.getpid()}_{int(time.time() * 1_000_000)}"

_AVA_HOME = _REPO_ROOT / "tmp" / f"ava_e2e_home_{_E2E_SUFFIX}"


# ---- session fixtures ---------------------------------------------------


_GC_THRESHOLD_SECONDS = 3600


def _gc_orphan_e2e() -> None:
    """GC orphan tmp AVA_HOME + frontend build dirs older than 1h, left by a
    crashed session. The e2e Postgres is the root conftest's session cluster
    (auto-removed), so there are no orphan databases to drop."""
    threshold_us = int((time.time() - _GC_THRESHOLD_SECONDS) * 1_000_000)
    # tmp dir GC
    tmp_root = _REPO_ROOT / "tmp"
    if tmp_root.exists():
        for d in tmp_root.glob("ava_e2e_home_*"):
            try:
                ts_us = int(d.name.rsplit("_", 1)[1])
            except (IndexError, ValueError):
                continue
            if ts_us > threshold_us:
                continue
            with contextlib.suppress(OSError):
                shutil.rmtree(d)
    # frontend build dirs (in ui/web/.builds/, not in tmp/ because Turbopack
    # symlink constraints—see frontend_proc fixture docstring)
    builds_root = _REPO_ROOT / "ui" / "web" / ".builds"
    if builds_root.exists():
        for d in builds_root.glob("build-*"):
            try:
                ts_us = int(d.name.rsplit("_", 1)[1])
            except (IndexError, ValueError):
                continue
            if ts_us > threshold_us:
                continue
            with contextlib.suppress(OSError):
                shutil.rmtree(d)


def _apply_e2e_seq_offset() -> None:
    """Offset agents_id_seq by PID so concurrent e2e workers spawn non-colliding
    session names (`ava-agent-<id>` shares a global namespace). PID <
    65536, ×1000 leaves ample room. Applied once per e2e package — there is exactly
    one, so once per run: `truncated_db` truncates without RESTART IDENTITY, so the
    sequence is never reset and this base persists (ids stay monotonic +
    per-worker-disjoint for the whole run)."""
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("ALTER SEQUENCE agents_id_seq RESTART {}").format(
                sql.Literal(os.getpid() * 1000)
            )
        )


@pytest.fixture(scope="package", autouse=True)
def _e2e_process_env(  # noqa: PLR0915 -- one cohesive env-layering + restore sequence; each step is one statement
    _provisioned_db: str, _provisioned_redis: str
) -> Iterator[None]:
    """Layer e2e-specific process config on the root conftest's session Postgres +
    Redis (provisioned by its autouse fixtures). The DB/Redis
    URLs are already in settings + os.environ (AVA_DB_URL / AVA_REDIS_URL), so the
    gateway / agent subprocesses inherit them. This adds the e2e Redis channel
    names, AVA_HOME, machine files, and the agents_id_seq PID offset.

    **`scope="package"` is what makes the restore below honest, and it only works
    because `tests/e2e/__init__.py` exists.** This fixture reassigns seven
    process-global env vars, `AVA_HOME` and `AVA_GATEWAY_URL` among them, and puts them
    back in its `finally`. Under `scope="session"` that `finally` fired at the end of
    the *pytest session* — not when pytest left `tests/e2e/` — so every test collected
    after this package in the same process ran with the e2e values still installed.
    `tests/test_home_isolation.py` always sorts after `tests/e2e/`, and it is the guard
    that exists to notice exactly this class of leak; it failed on two of its four
    assertions in every serial run, on `main`, for as long as the keyword said
    `session`.

    **The `__init__.py` is not decoration.** `_pytest.fixtures.get_scope_package`
    walks the node's parents for a `Package` whose nodeid matches this fixture's and
    **returns `node.session` when it finds none** — silently, no warning. pytest builds
    a `Package` node only for a directory containing `__init__.py`; without one the
    directory is a plain `Dir` and `scope="package"` is a synonym for
    `scope="session"`. So the keyword alone is a no-op here: both changes landed
    together, and `tests/test_home_isolation.py` asserts both.

    Why it was invisible: CI's backend job runs `--ignore=tests/e2e`, so the two files
    never share a process there at all, and under `-n auto` they land in different
    workers regardless. The leak could only ever be seen locally, where a reader had
    every reason to write the red off as someone else's flake.

    Anything that depends on this fixture must be package-scoped or narrower —
    `e2e_db` is (a session-scoped consumer raises `ScopeMismatch`, which is how pytest
    tells you about the rest of the chain).
    """
    _gc_orphan_e2e()
    # Belt over the collection-time reaping above: a session can reach package
    # setup with residue that appeared since (e.g. a peer's run died mid-way).
    # A reaper failure must not abort the session (same rule as collection) —
    # and at teardown a failure must not skip the env restore below.
    with contextlib.suppress(Exception):
        sweep_stale_e2e_processes()
    _apply_e2e_seq_offset()
    # Every key assigned below, no exceptions — `tests/test_home_isolation.py`'s
    # `test_the_e2e_fixture_restores_every_env_key_it_assigns` derives the assigned set
    # from this body by AST and fails if the tuple falls behind it, so a key added to
    # the setup without a line here cannot merge.
    _env_keys = (
        "AVA_AUTH_MIDDLEWARE_ENABLED",
        "AVA_CONFIG_FETCH",
        "AVA_EVENTS_CHANNEL",
        "AVA_HOME",
        "AVA_GATEWAY_CORS_ALLOWED_ORIGINS",
        "AVA_GATEWAY_URL",
        "AVA_GATEWAY_HEALTH_URL",
        "AVA_MACHINE_SERVE_GATEWAY",
        "AVA_MACHINE_SERVE_AGENT_RUNNER",
        "AVA_MACHINE_NAME",
        "AVA_NODE_STALL_DUMP_SECONDS",
        "AVA_RUNNER_DB_PASSWORD",
    )
    prev_events = settings.data_plane.events_channel
    prev_env = {k: os.environ.get(k) for k in _env_keys}

    settings.data_plane.events_channel = f"ava:events:e2e:{_E2E_SUFFIX}"
    os.environ["AVA_EVENTS_CHANNEL"] = settings.data_plane.events_channel
    # Diagnostic: the e2e model is instant (AVA_LLM_OVERRIDE), so any node that
    # sits longer than this is a real stall, not inference. Spawned agents
    # inherit the var and dump every thread's stack to stderr (captured as the
    # agent-*.log artifact), naming the blocked frame.
    os.environ["AVA_NODE_STALL_DUMP_SECONDS"] = "15"
    # No AVA_LAUNCH_CONFIRM_TIMEOUT_SECONDS override (and so no `_env_keys` entry
    # for it): this fixture used to widen the 10s prod default to 25s because a CI
    # runner's parallel load ran the agent's boot — self-fetching config from the
    # gateway plus its import chain — past it. The default is now 45s with a
    # live-child extension on top, so pinning a number here could only NARROW the
    # window this suite runs with.
    _AVA_HOME.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["AVA_HOME"] = str(_AVA_HOME)
    # multi-machine setup: spawn_agent / claim_agent_row / post_agents read
    # $AVA_HOME/machine_name + the serve-capability files; write them or resolution
    # raises.
    (_AVA_HOME / "machine_name").write_text(f"e2e-{_E2E_SUFFIX}")
    (_AVA_HOME / "machine_serve_agent_runner").write_text("true")
    (_AVA_HOME / "machine_serve_gateway").write_text("true")
    # Set the identity env explicitly: the host operator's real ~/.ava/.env may
    # have exported AVA_MACHINE_NAME / AVA_MACHINE_SERVE_* (and `_resolve_*` reads
    # env before the file), which would leak the host's identity (e.g. name=test-host)
    # into the gateway/ops subprocesses — so the gateway would dispatch a spawn to
    # the host's name instead of the e2e machine registered below.
    os.environ["AVA_MACHINE_NAME"] = f"e2e-{_E2E_SUFFIX}"
    os.environ["AVA_MACHINE_SERVE_AGENT_RUNNER"] = "true"
    os.environ["AVA_MACHINE_SERVE_GATEWAY"] = "true"
    # This home owns both the gateway config and its local runner, like the
    # production single-box shape. Calling it a pure runner while handing ops
    # an owner URL would bypass the authenticated runner projection contract.
    # Keep the maintenance fetch opt-out for the fixture's subprocess setup.
    os.environ["AVA_CONFIG_FETCH"] = "skip"
    # Agent subprocesses reach the e2e gateway via gateway_api_base().
    os.environ["AVA_GATEWAY_URL"] = GATEWAY_URL
    # The restarter daemon (services/restarter/daemon.py) gates respawn on
    # `_gateway_healthy()` which probes settings.services.gateway_health_url. Default is
    # http://localhost:8000/api/health — but the e2e gateway runs on a dynamic
    # port. Point it at the e2e gateway so the health check can pass.
    os.environ["AVA_GATEWAY_HEALTH_URL"] = f"{GATEWAY_URL}/api/health"
    # e2e runs the PROD DEFAULT deployment shape: a single box. It is the one
    # place a real gateway spawns real agent subprocesses, so it exercises the
    # shape prod actually runs — issue #993 (every agent dead on a single box:
    # spawn env pinned AVA_CONFIG_SOURCE=gateway while /api/bootstrap 404s).
    # A real single box keeps the cluster config in $AVA_HOME/.env — that file
    # is what a spawned agent (AVA_CONFIG_SOURCE=local, cluster keys env -u'd
    # off its launch command) re-reads at import. Mirror that: persist the
    # session's cluster-scoped values (the native DB/Redis URLs, the e2e
    # event channels) into the e2e home's .env.
    # Daemon health ports are dynamically allocated per e2e run (ops / restarter /
    # labeler / etc. fixtures each bind to a free kernel-assigned port). Writing
    # them into $AVA_HOME/.env would let _enforce_cluster_env_authority() (called
    # by load_ava_env at daemon startup) clobber the dynamic ports with the .env
    # values — causing "address already in use" on the production defaults.
    # Derived from the same map `health_port()` consults, not hand-listed: the
    # hand-listed version had already drifted (events_maintenance missing), and a
    # newly registered daemon would silently re-open this hole.
    from shared.daemon_health import _HEALTH_PORT_OVERRIDES
    from shared.env_registry import cluster_scope_aliases

    _health_port_keys: frozenset[str] = frozenset(
        f"AVA_{attr.upper()}" for attr in _HEALTH_PORT_OVERRIDES.values()
    )
    # Task #1236: the e2e home mirrors a BORN cluster — the gateway's .env must
    # carry the runner credential and the least-privilege ava_runner role must
    # exist in the e2e Postgres, or the spawned agents' bootstrap fetches
    # (?role=runner — the runner projection) get a 400 and every agent dies at
    # boot. The throwaway pg's db/role identifier is `ava_citest` (the URL
    # username is the throwaway admin, not the identity — read the db name).
    import secrets
    from urllib.parse import urlsplit

    from shared.cluster import ensure_runner_role

    runner_pw = secrets.token_urlsafe(32)
    _e2e_db_url = settings.data_plane.db_url
    ensure_runner_role(
        urlsplit(_e2e_db_url).path.strip("/"),
        base_admin_url=_e2e_db_url.rsplit("/", 1)[0] + "/postgres",
        runner_password=runner_pw,
    )
    os.environ["AVA_RUNNER_DB_PASSWORD"] = runner_pw
    # Cluster-scoped CORS origins must be in the unit .env before gateway boot.
    os.environ["AVA_GATEWAY_CORS_ALLOWED_ORIGINS"] = (
        f"http://localhost:{FRONTEND_PORT},http://127.0.0.1:{FRONTEND_PORT}"
    )
    # auth_middleware_enabled is cluster-pinned: the env-authority pass drops
    # an inherited value the unit .env does not declare (default True = 401 on
    # every unauthenticated e2e call). Declare it so e2e runs auth-off.
    os.environ["AVA_AUTH_MIDDLEWARE_ENABLED"] = "false"

    env_lines = [
        f"{key}={os.environ[key]}"
        for key in sorted(cluster_scope_aliases())
        if key in os.environ and key not in _health_port_keys
    ]
    # The runner credential is NOT a cluster-scope alias (it is a gateway-.env
    # secret that only travels inside the projected URL) — write it explicitly
    # so the gateway's bootstrap projection can read it from the file.
    env_lines.append(f"AVA_RUNNER_DB_PASSWORD={runner_pw}")
    (_AVA_HOME / ".env").write_text("\n".join(env_lines) + "\n")
    try:
        yield
    finally:
        # Reap whatever OUR session left behind (an agent that ignored its
        # terminate, a gateway child that escaped `managed_proc`). Runs at
        # package teardown, when the session fixtures (frontend, browser) are
        # still holding their own processes — those are also e2e-owned and
        # get reaped here too, which their own teardowns simply find already
        # dead instead of killing again. A reaper failure must never skip the
        # env restore (the leak this file's scope contract exists to prevent).
        with contextlib.suppress(Exception):
            sweep_stale_e2e_processes(include_own=True)
        settings.data_plane.events_channel = prev_events
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # Preserve agent logs into _LOG_DIR (tmp/e2e-logs, the dir CI uploads on
        # failure) before tearing down AVA_HOME. The artifact upload runs after
        # pytest exits, by which point this rmtree would otherwise have deleted
        # $AVA_HOME/logs/agent-*.log — so an e2e failure left no agent-side trace.
        agent_logs = _AVA_HOME / "logs"
        if agent_logs.is_dir():
            for log in agent_logs.glob("agent-*.log"):
                with contextlib.suppress(OSError):
                    shutil.copy2(log, _LOG_DIR / log.name)
        with contextlib.suppress(OSError):
            shutil.rmtree(_AVA_HOME)


@pytest.fixture(scope="package")
def e2e_db(_e2e_process_env: None) -> Iterator[None]:
    """The e2e database is the root conftest's session Postgres (root conftest +
    _e2e_process_env). Kept as a thin fixture because the e2e fixtures
    (truncated_db) depend on it to order after process-env setup.

    Package-scoped because `_e2e_process_env` is: a broader scope may not depend on a
    narrower one, so leaving this at `session` raises `ScopeMismatch` at setup. The DB
    itself is unaffected — it is the *root* conftest's session Postgres, provisioned
    once per session by `_provisioned_db`, and this fixture only orders consumers
    after the env layering."""
    yield


_LOG_DIR = _REPO_ROOT / "tmp" / "e2e-logs"


@pytest.fixture(scope="session")
def frontend_proc() -> Iterator[None]:
    """next build + next start :<dynamic_port>——run in prod mode (dev mode 1.5GB
    memory too heavy, prod start ~200MB, e2e total memory peak 2.2GB → 0.9GB).

    Multi-worker (pytest-xdist) isolation: each session runs build in
    `ui/web/.builds/build-<suffix>/`, source (src/public/config total <1MB) copied in,
    `node_modules` (624MB) symlinked to `../../node_modules` (=main ui/web/node_modules) not duplicating disk.

    Why build_dir placed at `ui/web/.builds/`:
    - Turbopack strictly checks node_modules symlink cannot point outside "filesystem root",
      symlink in parallel tmp/ dir fails; `ui/web/.builds/build-X/node_modules
      → ../../node_modules` is monorepo-style hoisted lookup, Turbopack accepts
    - `.builds/` starts with dot, Next dev watcher default ignore won't trigger reload
    - `.gitignore` already added ui/web/.builds/

    Each session is a cold build (`.next/` not reused across sessions): `NEXT_PUBLIC_API_BASE`
    build-time inlined into client bundle (api.ts:46), reusing cached chunk across sessions
    would leave the previous session's dynamic GATEWAY_URL residual. Project small cold build 5-6s,
    safer than 0.7s incremental build.
    """
    frontend_src = _REPO_ROOT / "ui" / "web"
    if not shutil.which("npm"):
        pytest.skip("npm not found — e2e tests require Node.js")
    if not (frontend_src / "node_modules" / "next").exists():
        pytest.skip("ui/web/node_modules not installed — run `npm install` in ui/web/ first")
    build_dir = frontend_src / ".builds" / f"build-{_E2E_SUFFIX}"
    env = {**os.environ, "NEXT_PUBLIC_API_BASE": GATEWAY_URL}

    if build_dir.exists():
        shutil.rmtree(build_dir)
    shutil.copytree(
        frontend_src,
        build_dir,
        ignore=shutil.ignore_patterns(
            "node_modules", ".next", ".builds", "coverage", "*.log", "tsconfig.tsbuildinfo"
        ),
    )
    # symlink goes relative path `../../node_modules` (= ui/web/node_modules)
    (build_dir / "node_modules").symlink_to(Path("../../node_modules"))

    # _LOG_DIR is created by the package-scoped _e2e_process_env, which runs
    # AFTER session-scoped frontend_proc — a fresh checkout (CI runner cache
    # evicted, new worktree) has no tmp/e2e-logs and this open() would fail.
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    build_log = _LOG_DIR / f"frontend-build-{_E2E_SUFFIX}.log"
    with build_log.open("w") as f:
        result = subprocess.run(
            ["npm", "run", "build"],
            cwd=str(build_dir),
            env=env,
            stdout=f,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"npm run build failed (exit {result.returncode}); see {build_log}")

    try:
        with managed_proc(
            ["npm", "run", "start", "--", "-p", str(FRONTEND_PORT)],
            cwd=str(build_dir),
            env=env,
            label="frontend",
            log_path=str(_LOG_DIR / f"frontend-{_E2E_SUFFIX}.log"),
        ):
            wait_for_port("127.0.0.1", FRONTEND_PORT, timeout=30.0, label="frontend")
            yield
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(build_dir)


@pytest.fixture(scope="package")
def playwright_runtime() -> Iterator[Playwright]:
    """Single `sync_playwright()` context for this e2e package.

    Playwright's sync API rejects nested entries; tests that want a second
    engine (e.g. webkit alongside the default chromium) must reuse this
    runtime rather than open their own.

    `scope="package"` (not session): the sync API runs an asyncio event loop
    on the main thread for as long as the context is open, and a session-scoped
    context would keep that loop alive until the whole pytest session ends —
    breaking every later async test (`Runner.run() cannot be called from a
    running event loop`, observed pairing tests/e2e with tests/gateway). The
    package scope fires teardown when pytest leaves `tests/e2e/` (the same
    pattern `_e2e_process_env` uses for its env restore).
    """
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="package")
def playwright_browser(playwright_runtime: Playwright) -> Iterator[Browser]:
    headed = os.environ.get("HEADED") == "1"
    browser = playwright_runtime.chromium.launch(headless=not headed)
    try:
        yield browser
    finally:
        browser.close()


# ---- function fixtures --------------------------------------------------


@pytest.fixture
def scenario_env(request: pytest.FixtureRequest) -> Iterator[None]:
    """Read test's @pytest.mark.scenario('module:factory') to set AVA_LLM_OVERRIDE.

    Use bare os.environ not monkeypatch——the latter function teardown automatically unsets,
    which would clear it while gateway_proc still needs env. This fixture's own finalizer clears.
    """
    # pytest's FixtureRequest.node resolves to Unknown in its stub —
    # the marker read is the whole point of this fixture.
    marker = request.node.get_closest_marker("scenario")  # pyright: ignore[reportUnknownMemberType]
    if marker is None:
        raise RuntimeError(
            f"e2e test {request.node.name} needs @pytest.mark.scenario('module:factory')"  # pyright: ignore[reportUnknownMemberType]
        )
    scenario = marker.args[0]  # pyright: ignore[reportUnknownMemberType]
    prev = os.environ.get("AVA_LLM_OVERRIDE")
    os.environ["AVA_LLM_OVERRIDE"] = scenario
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("AVA_LLM_OVERRIDE", None)
        else:
            os.environ["AVA_LLM_OVERRIDE"] = prev


@pytest.fixture
def gateway_proc(scenario_env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """gateway uvicorn (kernel-assigned port, see `_ports.py`)——inherits current os.environ (including AVA_LLM_OVERRIDE).

    function-scoped rather than session: each test restarts gateway to pick up new scenario env.
    Startup ~2-3s acceptable.
    """
    from shared import start_serving

    global _previous_gateway  # noqa: PLW0603 — retain the exact prior fixture child, not just its PID.
    if _previous_gateway is not None and _previous_gateway.poll() is None:
        raise RuntimeError("previous exact gateway fixture has not exited")

    # The pytest process imported Settings before this package redirected
    # AVA_HOME. Keep its marker writes in the exact home inherited by the
    # gateway, restarter, and agent-host subprocesses.
    monkeypatch.setattr(settings.general, "ava_home", _AVA_HOME)
    generation = start_serving.begin_start()
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "gateway.app:app",
        "--host",
        "127.0.0.1",
        "--fd",
        str(GATEWAY_SOCKET.fileno()),
    ]
    log_path = _LOG_DIR / f"gateway-{_E2E_SUFFIX}.log"
    env = os.environ.copy()
    # Disable the auth middleware in e2e — the auth layer is tested by its
    # dedicated suite; e2e tests exercise business logic without auth overhead.
    env["AVA_AUTH_MIDDLEWARE_ENABLED"] = "false"
    evidence_path = log_path.with_suffix(".listeners.json")
    evidence_path.write_text(json.dumps(listener_evidence(GATEWAY_PORT, "reserved-before-start")))
    with managed_proc(
        cmd,
        env=env,
        label="gateway",
        log_path=str(log_path),
        pass_fds=(GATEWAY_SOCKET.fileno(),),
    ) as gateway:
        _previous_gateway = gateway
        gateway_birth = psutil.Process(gateway.pid).create_time()
        # The retained listener alone is not readiness: require normal HTTP lifespan.
        # /api/agents returns 200 = app is up (lifespan has started db_pool/redis pool)
        last_status: int | None = None
        last_err: Exception | None = None
        for _ in range(60):
            try:
                resp = httpx.get(f"{GATEWAY_URL}/api/agents", timeout=1.0)
                last_status = resp.status_code
                if resp.status_code == 200:
                    break
            except httpx.HTTPError as e:
                last_err = e
            time.sleep(0.5)
        else:
            evidence_path.write_text(
                json.dumps(listener_evidence(GATEWAY_PORT, "http-readiness-failed"))
            )
            raise RuntimeError(
                f"gateway /api/agents not ready within 30s: "
                f"last_status={last_status} last_err={last_err!r}; see {log_path}"
            )
        try:
            require_native_listener(gateway.pid, gateway_birth, GATEWAY_PORT)
            evidence_path.write_text(json.dumps(listener_evidence(GATEWAY_PORT, "http-ready")))
            yield generation
        finally:
            start_serving.clear_serving()


@pytest.fixture
def truncated_db(e2e_db: None) -> Iterator[None]:
    """Before each test, clear all business tables in e2e DB (FK order)."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        # TRUNCATE without RESTART IDENTITY: ids grow monotonically from the
        # session-level `_apply_e2e_seq_offset` PID base and are never reused
        # across tests (the same no-reuse contract as the non-e2e suite — see
        # decisions/2026-06-30-monotonic-test-ids.md). Leaving the sequence untouched also
        # preserves the per-worker PID offset without re-applying it, so
        # cross-worker session names stay disjoint.
        cur.execute(
            # `events` was dropped with the task #1281/#1823 cleanup.
            "TRUNCATE inbound_messages, agents_meta, agents, "
            "checkpoint_blobs, checkpoint_writes, checkpoints, checkpoint_migrations "
            "CASCADE"
        )
        conn.commit()
    # checkpoint_migrations truncated, saver runs setup again (idempotent)
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.setup()
    yield


@pytest.fixture
def agent_host_proc(gateway_proc: str) -> Iterator[None]:
    """Run the local agent host with this test's model and machine identity."""
    cmd = [sys.executable, "-m", "services.agent_host.daemon"]
    env = os.environ.copy()
    # Prod-shaped profile construction: the healthcheck launches the daemon with
    # the `agent` profile (it runs the agent kernel in-process). Marker-less
    # full construction here masked the consumption-matrix gap that crashed a
    # `runner`-profile launch at soak startup (2026-08-30).
    env["AVA_PROCESS_PROFILE"] = "agent"
    # pidfile placed in e2e tmp dir, avoids conflict with dev daemon / cross-test residue
    env["AVA_AGENT_HOST_PIDFILE"] = str(_AVA_HOME / "agent_host.pid")
    # Kernel-assigned per worker, avoiding a shared health port across tests.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        agent_host_health_port = s.getsockname()[1]
    env["AVA_AGENT_HOST_HEALTH_PORT"] = str(agent_host_health_port)
    log_path = _LOG_DIR / f"agent-host-{_E2E_SUFFIX}.log"
    with managed_proc(cmd, env=env, label="agent-host", log_path=str(log_path)) as proc:
        try:
            wait_for_port("127.0.0.1", agent_host_health_port, timeout=30.0, label="agent-host")
        except RuntimeError as e:
            code = proc.poll()
            state = f"exited with code {code}" if code is not None else "still running"
            raise RuntimeError(
                f"{e}; agent-host daemon {state}; log tail:\n{proc_log_tail(str(log_path))}"
            ) from e
        from shared import start_serving

        if not start_serving.mark_serving(gateway_proc):
            raise RuntimeError("e2e agent-host lost the gateway start generation")
        yield


@pytest.fixture
def ops_proc(gateway_proc: str) -> Iterator[None]:
    """services/agent_ops/daemon.py — the agent-runner ops server.

    Spawn is HTTP-uniform: `POST /api/agents` creates the row on the gateway and
    forwards the launch to this host's ops server over localhost (no in-process
    shortcut), which dispatches launch_agent_op. So the e2e single box must run
    the ops daemon (like a real `ava start`) AND have its machine registered
    with the ops URL, or every spawn 502s. Function-scoped
    + inherits os.environ so the spawned agent subprocess sees the per-test
    scenario env (AVA_LLM_OVERRIDE), same reason gateway_proc is function-scoped.
    """
    env = os.environ.copy()
    env["AVA_PROCESS_PROFILE"] = "runner"
    env["AVA_OPS_PIDFILE"] = str(_AVA_HOME / "ops.pid")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        ops_port = s.getsockname()[1]
    env["AVA_OPS_HEALTH_PORT"] = str(ops_port)
    # Register this single box as a runner advertising its ops URL — what the
    # gateway dials. UPSERT (function-scoped; the port is re-allocated per test).
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, role, gateway_url) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET role = EXCLUDED.role, "
            "gateway_url = EXCLUDED.gateway_url, up_since_at = NOW()",
            (f"e2e-{_E2E_SUFFIX}", ["agent-runner"], f"http://127.0.0.1:{ops_port}"),
        )
        conn.commit()
    log_path = _LOG_DIR / f"ops-{_E2E_SUFFIX}.log"
    with managed_proc(
        [sys.executable, "-m", "services.agent_ops.daemon"],
        env=env,
        label="ops",
        log_path=str(log_path),
    ):
        wait_for_port("127.0.0.1", ops_port, timeout=30.0, label="ops")
        yield


@pytest.fixture
def spawned_agent(ops_proc: None, agent_host_proc: None, truncated_db: None) -> Iterator[int]:
    """Allocate an idle agent via the gateway; the host runs it when work arrives."""
    resp = httpx.post(f"{GATEWAY_URL}/api/agents", json={"spawner": "user"}, timeout=30.0)
    resp.raise_for_status()
    agent_id = int(resp.json()["id"])

    try:
        # Allocation creates idle intent; an idle agent owns no process or task.
        deadline = time.monotonic() + 90.0
        last_status: str | None = None
        last_pid: int | None = None
        while time.monotonic() < deadline:
            with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
                cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (agent_id,))
                row = cur.fetchone()
            if row is not None:
                last_status, last_pid = row
                if last_status == AgentStatus.IDLING.value:
                    break
            time.sleep(0.3)
        else:
            raise RuntimeError(
                f"agent {agent_id} did not reach idling within 90s: "
                f"last_status={last_status!r} last_pid={last_pid!r}; "
                f"see $AVA_HOME/logs/agent-{agent_id}.log "
                f"(CI: the e2e-logs artifact)"
            )
        yield agent_id
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{GATEWAY_URL}/api/agents/{agent_id}/terminate", timeout=5.0)
            # Wait for the agent to finish exiting before this test's
            # function-scoped gateway is torn down. The agent finalizes its
            # own status via POST /api/agents/{id}/exited, which needs a live
            # gateway — matching prod, where the gateway long outlives any
            # single agent's exit. Without the wait, gateway_proc teardown
            # kills the gateway mid-exit; the agent's /exited call then fails +
            # network-retries, the process lingers holding DB connections, and
            # that races the next test's spawn / the session-end
            # pg_terminate_backend reap (manifests as a spawn 500).
            terminate_deadline = time.monotonic() + 10.0
            while time.monotonic() < terminate_deadline:
                tr = httpx.get(f"{GATEWAY_URL}/api/agents/{agent_id}", timeout=2.0)
                if (
                    tr.status_code == 200
                    and tr.json().get("status") == AgentStatus.TERMINATED.value
                ):
                    break
                time.sleep(0.2)


@pytest.fixture
def playwright_context(playwright_browser: Browser) -> Iterator[BrowserContext]:
    # No session cookie is injected: the frontend page (localhost) and the
    # gateway (127.0.0.1) are different hosts, so a SameSite=Lax cookie is
    # dropped on the cross-site /api/auth/check request. Instead the gateway
    # reports authenticated when auth is disabled, so AuthGuard renders
    # the app directly.
    ctx = playwright_browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def playwright_page(playwright_context: BrowserContext) -> Iterator[Page]:
    page = playwright_context.new_page()
    try:
        yield page
    finally:
        page.close()


@pytest.fixture
def e2e_env(
    frontend_proc: None,
    spawned_agent: int,
    playwright_page: Page,
) -> E2EEnv:
    return E2EEnv(
        gateway_url=GATEWAY_URL,
        frontend_url=FRONTEND_URL,
        agent_url=f"{FRONTEND_URL}?agent_id={spawned_agent}",
        page=playwright_page,
        agent_id=spawned_agent,
    )


# ---- markers ------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "scenario(module_factory): specify fake LLM scenario module:factory string",
    )


# socket / subprocess references to avoid ruff treating as unused imports erroneously removed (only type hints usage)
_ = socket
_ = subprocess


# ── issue #213: dead-server evidence on failure ────────────────────────────


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[object]
) -> Generator[None, object, None]:
    """When a test fails, append evidence for any e2e server that exited while
    its fixture was still active — exit code + the server's own log tail — so a
    "server that should have been up wasn't" flake (issue #213) carries its
    cause in the report instead of only in an uploaded artifact."""
    outcome = yield
    report = cast(TestReport, cast(Any, outcome).get_result())
    if not report.failed:
        return
    from tests.e2e._proc import dead_server_evidence

    evidence = dead_server_evidence()
    if not evidence:
        return
    rendered = str(report.longrepr) + "\n\n" + evidence
    report.longrepr = rendered
