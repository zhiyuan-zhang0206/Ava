"""Web UI / HTTP API (pure JSON API, does not serve HTML).

`/docs` / `/redoc` / `/openapi.json` are disabled (docs_url=None etc. below,
to avoid leaking the route schema) — this module is the contract source of
truth; codegen auto-generates frontend / SDK types from the code, and any drift
is caught by the codegen-fresh hook. The docstring does **not** re-list
endpoints (rot defense).

The non-JSON response surface is deliberately small and enumerated:
- `GET /api/okf/graph` (`gateway/routers/okf_graph.py`) — the OKF
  knowledge-graph D3 visualization as a self-contained HTML page (dev-tool
  template + build mechanics, not a frontend component).
- `GET /api/agents/{id}/uploads/...` (`gateway/routers/uploads.py`) —
  `FileResponse` file downloads.
- `/pages/{agent_id}-{name}/...` (`gateway/routers/pages.py`) — a
  streaming reverse proxy to an agent's own page server (arbitrary content).
- `/grafana/*` (`gateway/routers/grafana.py`) — streaming reverse proxy to a
  co-located Grafana instance (HTML dashboard, default off → 404).
All four sit behind the normal session-cookie / bearer-secret middleware.

Design-wise, the gateway (spawn / send_message) is centralized under
`/api/agents/*` — SDK (`ava.agents.*`) / frontend / bootstrap script
all share the same endpoint set. Auto-resurrect is handled internally
by `deliver_chat_inbound`. See
decisions/2026-05-09-stateless-gateway.md.

Architectural rule: this module does **not** build the turn-loop prompts
(spawn/draft endpoints like schedules/guide/packages DO inline a fixed
system prompt for the agent they spawn — that is the deliberate exception),
does not run the LLM, does not construct the LangGraph; it only imports
pure-function helpers from `agent.messages` (legacy cancel marker detection,
etc.).
UI and kernel are coupled through Postgres (pending / agents tables) +
Redis (`ava:events` channel); they do not share Python process state or
semantic payload.

Concurrency:
- DB uses one `shared.db.pool()` per process; each request borrows a connection
- Publish callsites reuse one process-wide `aredis.Redis` via
  `shared.redis_client.get_async_redis()`; SSE / pubsub subscribers still
  open their own connection per request (subscriber lifecycle ≠ publisher).

Frontend: Next.js app under `ui/web/`, served on :3000; the browser calls
this service directly at `<hostname>:8000` (no rewrites proxy — see
ui/web/next.config.ts).

Endpoint implementations live under `gateway/routers/<domain>.py` and are
mounted at the bottom of this file. Lifespan and middleware registration remain
in this module; exception-to-envelope adapters live in `gateway/error_handlers.py`.

Start: `.venv/bin/python scripts/start_gateway.py` (or `python -m gateway`)
-> uvicorn :8000 on all interfaces, both IPv4 and IPv6 (reachable on the
cluster's private network)
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import shared.db
from gateway import _agent_max_id as _agent_max_id_module
from gateway import _auth401_log as _auth401_log_module
from gateway import (
    _idempotency,
    _latency,
    _pause_policy,
    _runtime_metrics,
    alert_reconciliation,
    loki_events,
    loki_query_budget,
    prom_metrics,
    ttl_reaper,
)
from gateway import mcp_endpoint as _mcp_endpoint
from gateway._auth401_log import _log_auth401_rejection
from gateway._cors import cors_allowed_origins
from gateway._server import main as _run_gateway
from gateway.error_envelope import error_response, request_trace_middleware
from gateway.error_handlers import (
    _ava_agent_error_handler,
    _http_exception_handler,
    _loki_query_budget_error_handler,
    _observability_read_unavailable_handler,
    _prom_query_budget_error_handler,
    _request_validation_error_handler,
    _unhandled_exception_handler,
)
from gateway.error_handlers import (
    _cors_headers as _cors_headers,
)
from gateway.routers import (
    _machine_pause as machine_pause_router,
)
from gateway.routers import (
    agent_events as agent_events_router,
)
from gateway.routers import (
    agent_inspect as agent_inspect_router,
)
from gateway.routers import (
    agents as agents_router,
)
from gateway.routers import (
    agents_lifecycle as agents_lifecycle_router,
)
from gateway.routers import (
    agents_state as agents_state_router,
)
from gateway.routers import (
    alerts as alerts_router,
)
from gateway.routers import (
    auth as auth_router,
)
from gateway.routers import (
    bootstrap as bootstrap_router,
)
from gateway.routers import (
    cluster as cluster_router,
)
from gateway.routers import (
    commands as commands_router,
)
from gateway.routers import (
    computer_traces as computer_traces_router,
)
from gateway.routers import (
    config as config_router,
)
from gateway.routers import (
    default_model as default_model_router,
)
from gateway.routers import (
    event_resolutions as event_resolutions_router,
)
from gateway.routers import (
    events as events_router,
)
from gateway.routers import (
    fleet_graph as fleet_graph_router,
)
from gateway.routers import (
    frontend_telemetry as frontend_telemetry_router,
)
from gateway.routers import (
    grafana as grafana_router,
)
from gateway.routers import (
    guide as guide_router,
)
from gateway.routers import (
    inventory as inventory_router,
)
from gateway.routers import (
    mcp_clients as mcp_clients_router,
)
from gateway.routers import (
    memory as memory_router,
)
from gateway.routers import (
    metrics as metrics_router,
)
from gateway.routers import (
    notices as notices_router,
)
from gateway.routers import (
    okf_graph as okf_graph_router,
)
from gateway.routers import (
    ops_monitor as ops_monitor_router,
)
from gateway.routers import (
    packages as packages_router,
)
from gateway.routers import (
    pages as pages_router,
)
from gateway.routers import (
    plugin_ui as plugin_ui_router,
)
from gateway.routers import (
    presets as presets_router,
)
from gateway.routers import (
    run_timeline as run_timeline_router,
)
from gateway.routers import (
    schedules as schedules_router,
)
from gateway.routers import (
    settings as settings_router,
)
from gateway.routers import (
    shell as shell_router,
)
from gateway.routers import (
    skills as skills_router,
)
from gateway.routers import (
    status as status_router,
)
from gateway.routers import (
    system as system_router,
)
from gateway.routers import (
    tasks as tasks_router,
)
from gateway.routers import (
    timeline as timeline_router,
)
from gateway.routers import (
    ui_contributions as ui_contributions_router,
)
from gateway.routers import (
    uploads as uploads_router,
)
from gateway.routers import (
    work_failed as work_failed_router,
)
from gateway.schedule_manager import ScheduleManager
from gateway.session_store import session_is_valid, touch_session
from shared.agents import AvaAgentError
from shared.cluster_auth import (
    cookie_name,
    verify_bearer,
)
from shared.config import settings, warn_deprecated_env_aliases
from shared.context import AvaContext
from shared.lm._plugin_providers import ensure_provider_plugins_loaded
from shared.machine import machine_name
from shared.os_cron import register_os_cron

_log = logging.getLogger(__name__)


def _start_periodic_flushers(app: FastAPI) -> None:
    """Start the gateway's periodic telemetry flushers as app.state tasks.

    Three loops, one lifecycle group: gateway latency aggregates (Task
    #1091), the auth-401 aggregate counter (Task #1712) and the agent-registry
    max-id gauge (Task #2010). Each loop emits ONE bounded event per 60s and
    never raises out of its loop; the lifespan teardown cancels all three.
    """
    app.state.latency_flusher = asyncio.create_task(_latency.latency_flusher())
    app.state.auth401_flusher = asyncio.create_task(_auth401_log_module.auth401_flusher())
    app.state.agent_max_id_flusher = asyncio.create_task(
        _agent_max_id_module.max_agent_id_flusher(app.state.db_pool)
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """App-level resources: data-plane and control-plane DB pools.

    Redis is not shared at app level — SSE endpoints need independent pubsub
    connections (one per client); attaching pubsub to a shared client causes
    interference.

    Web does not run the LLM, does not build the graph, does not hold the
    checkpointer — all operations that need those (framework compact /
    agent compact) go through writes to the inbound_messages table and are
    handed off to the kernel.

    The agent host handles native lifecycle work. Auto label generation
    runs in the separate services/labeler daemon.
    """
    ensure_provider_plugins_loaded()

    # AvaContext bundle — gateway is a non-graph entry point, so handles
    # (llm / ops_pool / inbound_listener) stay None; string-level
    # config (db_url / events_channel / ...) populates from settings defaults. Handlers that
    # want a uniform view of "where this process should talk to infra" read
    # app.state.ctx; raw db_pool / get_async_redis() keep working for
    # call sites that aren't migrated yet.
    app.state.ctx = AvaContext()
    # Runtime consumer -> `shared.db.pool()` dials the pooled URL (PgBouncer when
    # enabled, else direct) and decides the connection kwargs in one place:
    # prepare_threshold=None keeps every borrowed connection transaction-pooling-safe,
    # and PG_KEEPALIVE_KWARGS bounds a borrow on a half-dead socket. The second
    # matters here because this pool outlives everything — it is opened once per
    # gateway process and serves every request, so a connection idle across a host
    # sleep or a network change comes back on a dead TCP flow and, unbounded, parks
    # the request handler on the OS TCP-retransmit timeout.
    app.state.db_pool = shared.db.pool(max_size=8)
    # The control plane must never queue behind the saturated data-plane pool.
    # Audit P0-2 follows the 2026-08-23 watchdog misjudgment chain: health and
    # recovery reads need their own short, small reservation.
    app.state.control_db_pool = shared.db.pool(min_size=1, max_size=2, timeout=2.0)

    # Shared upstream client for the Grafana reverse proxy — one connection
    # pool across proxied requests instead of an AsyncClient per request.
    # Cheap when the proxy is disabled: no connection exists until the first
    # proxied request.
    app.state.grafana_client = grafana_router.build_proxy_client()
    app.state.alert_reconciler = alert_reconciliation.start_grafana_alert_reconciler(
        app.state.db_pool,
        app.state.grafana_client,
        alerts_router.publish_alert_rows,
    )

    # TTL reaper — enforce serve() page and persistent-shell deadlines (user
    # ruling 2026-08-25). Owns no request path; a pass that fails logs and
    # retries on the next interval.
    app.state.ttl_reaper = ttl_reaper.start_ttl_reaper(app.state.db_pool)

    # Register the OS-level health-probe cron (launchd plist on macOS, crontab
    # on Linux). This is the primary registration path — every gateway start
    # refreshes the plist, so an `ava cluster update` that changes the probe command
    # (e.g. adds --auto-rollback) takes effect on the next gateway restart
    # without relying on the converge phase. Idempotent.
    try:
        await asyncio.to_thread(register_os_cron)
    except Exception:
        _log.warning("OS health-probe cron registration failed", exc_info=True)

    # Cluster-internal schedule manager — one per cluster, owned by the gateway.
    # Supervises one session per enabled `schedules` row (the successor to
    # the retired cron scheduler).
    app.state.schedule_manager = ScheduleManager(app.state.db_pool)
    await app.state.schedule_manager.start()

    # Built-in schedules (schedules/manifest.json) — provisioned on boot
    # so a fresh install comes up with its product schedules (self-evolution,
    # memory) enabled and its cluster-operator schedules (trace-ship-tempo)
    # present but disabled, per the pre-open-source policy ruling (2026-08-11).
    # Idempotent create-if-missing: existing rows are never touched, so an
    # operator's edits survive every boot and a deliberately deleted built-in
    # comes back with its manifest default. Best-effort — a missing or corrupt
    # manifest must not take the gateway down; the reconcile loop launches any
    # newly created enabled schedule within a poll tick.
    try:
        from shared.builtin_schedules import provision_builtin_schedules

        def _provision() -> list[str]:
            # Connection acquisition included: `pool.connection()` blocks and
            # must not run on the event loop.
            with app.state.db_pool.connection() as conn:
                return provision_builtin_schedules(conn)

        created = await asyncio.to_thread(_provision)
        if created:
            _log.info("provisioned built-in schedules: %s", ", ".join(created))
    except Exception:
        _log.warning("built-in schedule provisioning failed", exc_info=True)

    warn_deprecated_env_aliases()

    # Config migrations (the retired override layers -> .env) run in the converge
    # phase before the gateway process starts, so by the time this Settings is
    # built the .env is already complete; nothing to do at lifespan startup.

    # Periodic telemetry emitters (latency / auth-401 / agent max-id / runtime): each
    # drains its accumulator or DB sample once per 60s and emits ONE bounded
    # event; the lifespan owns and stops every task or scheduled callback.
    _start_periodic_flushers(app)
    app.state.runtime_metrics = _runtime_metrics.start_runtime_monitor()

    # /mcp endpoint (design task #1212 step 1): flag-gated, built fresh per
    # lifespan — StreamableHTTPSessionManager.run() can only be entered once
    # per instance, and the tools close over this pool. Off (the default):
    # /mcp answers 404 through the mcp_gateway wrapper and nothing changes.
    mcp_manager = None
    if settings.gateway.mcp_endpoint_enabled:
        mcp_manager = _mcp_endpoint.build_manager(app.state.db_pool)
        app.state.mcp_manager = mcp_manager

    try:
        if mcp_manager is not None:
            async with mcp_manager.run():
                yield
        else:
            yield
    finally:
        app.state.mcp_manager = None
        app.state.runtime_metrics.stop()
        await ttl_reaper.stop_ttl_reaper(app.state.ttl_reaper)
        await alert_reconciliation.stop_grafana_alert_reconciler(app.state.alert_reconciler)
        await app.state.grafana_client.aclose()
        for flusher in (
            app.state.latency_flusher,
            app.state.auth401_flusher,
            app.state.agent_max_id_flusher,
        ):
            flusher.cancel()
            with suppress(asyncio.CancelledError):
                await flusher
        await app.state.schedule_manager.stop()
        app.state.db_pool.close()
        app.state.control_db_pool.close()


app = FastAPI(
    title="Ava",
    lifespan=lifespan,
    # FastAPI's auto-generated metadata leaks the full route schema; disable it.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Pause exemptions are a route-declared attribute: the middleware consumes
# only the tested decision function `gateway._pause_policy.should_bypass_pause`,
# which reads the CONTROL_PLANE doorplates from `shared/contracts.py`. The
# exempt surface (control plane + agent self-reports) is enumerable and
# audited by tests/gateway/test_route_contracts.py — a new exemption is a
# deliberate declaration, not an incident patch.


_PAUSE_READ_TTL_S = 1.0
"""How long a pause-posture read is cached. A 1s-stale judgment is fine for
the 503 gate (the pause fan-out itself is a multi-second rollout step, and
R1's lease semantics tolerate sub-second staleness); the cache is what keeps
the middleware off the DB for the steady state — one pool borrow + SELECT per
second per gateway process instead of one per request."""

_pause_cache: list[tuple[float, bool] | None] = [None]
"""``(expires_at_monotonic, paused)`` — the last posture read and when it
expires. A one-element list so the async reader can update it without a
`global` statement (ruff PLW0603); the middleware is the only writer."""

_pause_inflight: list[asyncio.Future[bool] | None] = [None]
"""One shared expired-cache posture read; followers await this Future."""


async def _cluster_is_paused(request: Request) -> bool:
    """Whether this host's posture is `paused`, read off the event loop.

    The posture row lives in the central DB and is read by the gateway's 503
    middleware on every request (audit P1-1: the old path opened a fresh
    non-pooled connection and ran a synchronous SELECT directly on the event
    loop — a slow DB froze the whole gateway exactly when pause matters
    most). This version borrows the reserved control-plane pool, runs the
    read in the threadpool, caches it for `_PAUSE_READ_TTL_S`, and shares one
    in-flight read when the cache expires.

    A read failure reads as NOT paused — the same conservative direction the
    old flag-file stat had (an unreadable flag was an absent flag). Offline
    maintenance projection comes from the cluster orchestrator's durable Gate
    marker, not this host posture read.
    """
    now = time.monotonic()
    cached = _pause_cache[0]
    if cached is not None and now < cached[0]:
        return cached[1]
    inflight = _pause_inflight[0]
    if inflight is not None:
        return await asyncio.shield(inflight)

    def _read_posture() -> bool:
        try:
            with request.app.state.control_db_pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT posture FROM host_deploy_state WHERE machine = %s",
                    (machine_name(),),
                )
                row = cur.fetchone()
            return row is not None and row[0] == "paused"
        except Exception:
            _log.warning(
                "[cluster] pause posture read failed; reading as not paused",
                exc_info=True,
            )
            return False

    async def _read_and_cache() -> bool:
        paused = await asyncio.to_thread(_read_posture)
        _pause_cache[0] = (now + _PAUSE_READ_TTL_S, paused)
        return paused

    # A canceled request stops awaiting this one worker read but does not
    # cancel it for concurrent middleware followers.
    task = asyncio.create_task(_read_and_cache())
    _pause_inflight[0] = task

    def _clear_inflight(done: asyncio.Future[bool]) -> None:
        if _pause_inflight[0] is done:
            _pause_inflight[0] = None

    task.add_done_callback(_clear_inflight)
    return await asyncio.shield(task)


# AtLeastOnceWithKey dedup (doorplate ①): generic keyed routes store/replay a
# response here. Transactional keyed routes (chat message delivery) bypass this
# layer and own the key in the same commit as their durable business row.
# Registered BEFORE the pause middleware below: Starlette's middleware stack
# runs in REVERSE registration order, so with this order a paused cluster
# answers 503 before dedup engages (audit P2-1 — the previous order let
# pause-window requests claim a placeholder and then 503, an INSERT+DELETE
# per request that also bricked the key if the process died inside the
# window).
app.middleware("http")(_idempotency.idempotency_middleware)


@app.middleware("http")
async def _cluster_pause_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """While this host's posture is `paused`, short-circuit SDK / UI /
    data-plane requests to 503 so the caller sees "cluster updating, retry
    shortly" and the request does not punch through to business logic that
    might step on a migrating schema.

    Exempt: every route whose doorplate declares CONTROL_PLANE (the
    /api/cluster/* control plane and the Grafana alerting webhook).
    Everything else 503.
    """
    if await _cluster_is_paused(request) and not _pause_policy.should_bypass_pause(
        request.method, request.url.path
    ):
        return error_response(
            request,
            code="cluster_updating",
            status=503,
            detail="cluster updating, retry shortly",
            retryable=True,
            headers={"Retry-After": "30"},
        )
    return await call_next(request)


# `/api/health`, `/api/auth/login`, and `/api/auth/check` must remain
# reachable without auth. Health is probed by each host on the private
# network; login is how the browser obtains a session cookie.
# Every other API route requires either a valid session cookie or a
# Bearer token carrying the cluster secret — unless the cluster has no secret
# at all (no-auth posture) or the middleware is disabled for e2e.
_AUTH_BYPASS_PATHS: frozenset[str] = frozenset(
    {
        "/api/health",
        "/api/auth/login",
        "/api/auth/check",
        "/api/auth/logout",
        # /mcp authenticates revocable, scoped clients in its ASGI wrapper.
        # Starlette redirects the mount root to /mcp/, so both spellings must
        # bypass cluster auth. Cluster credentials are not MCP identities.
        "/mcp",
        "/mcp/",
    }
)
_AUTH_BYPASS_METHOD_PATHS: frozenset[tuple[str, str]] = frozenset(
    {
        # Ingest webhooks — authenticated by
        # its own token (X-Alerts-Token / X-Ops-Alerts-Token / cluster-secret
        # Bearer / loopback trust) inside the router, not by the
        # session/bearer middleware. Alert reads still require cluster auth.
        ("POST", "/api/alerts"),
        ("POST", "/api/work-failed"),
    }
)
_STATE_CHANGING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_SESSION_TOUCH_INTERVAL_S = 60.0
_SESSION_TOUCH_MAX_ENTRIES = 1024
_session_last_touch: dict[str, float] = {}


def _prune_session_last_touch(now: float) -> None:
    """Drop expired bookkeeping and cap the map by oldest touch time."""
    if len(_session_last_touch) <= _SESSION_TOUCH_MAX_ENTRIES:
        return
    stale_before = now - settings.gateway.session_ttl_seconds
    for session_id, touched_at in tuple(_session_last_touch.items()):
        if touched_at < stale_before:
            _session_last_touch.pop(session_id, None)
    overflow = len(_session_last_touch) - _SESSION_TOUCH_MAX_ENTRIES
    if overflow > 0:
        oldest = sorted(_session_last_touch, key=_session_last_touch.__getitem__)[:overflow]
        for session_id in oldest:
            _session_last_touch.pop(session_id, None)


@app.middleware("http")
async def _cluster_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Require a valid session cookie OR Bearer token on every API route.

    Two auth methods, checked in order:
    1. Session cookie (``ava_session``) — for browser users who logged in.
    2. ``Authorization: Bearer <secret>`` — for SDK / agent / script callers.

    Two states serve the API unauthenticated, both first-class:
    - ``cluster_secret`` empty — a no-secret cluster is fully unauthenticated by
      design (single-box posture; the gateway then binds loopback only, see
      ``main()``).
    - ``auth_middleware_enabled=false`` — the e2e/test knob: bypass the
      middleware while keeping the cluster secret for internal
      service-to-service auth (ops / agent-host).
    """
    from gateway.request_principal import AuthPrincipal

    # This is set only by credential verification, never by caller/source JSON.
    request.state.auth_principal = None
    request.state.source_verified_by = None
    secret = settings.data_plane.cluster_secret
    if not settings.gateway.auth_middleware_enabled or not secret:
        return await call_next(request)
    # CORS preflight (OPTIONS) carries no credentials by spec, so it can never
    # satisfy the cookie/Bearer check below. CORSMiddleware is now OUTERMOST and
    # answers preflights before this middleware runs, so this branch is a
    # defensive fallback for a future CORS reconfiguration — keep it, because a
    # preflight that 401s here would surface every cross-origin POST/PATCH/PUT
    # (resurrect / terminate / restart / send-message) as "Failed to fetch".
    if request.method == "OPTIONS":
        return await call_next(request)
    if (
        request.url.path in _AUTH_BYPASS_PATHS
        or (
            request.method,
            request.url.path,
        )
        in _AUTH_BYPASS_METHOD_PATHS
    ):
        return await call_next(request)

    # 1. Check session cookie
    cookie_token = request.cookies.get(cookie_name())
    if cookie_token and await asyncio.to_thread(
        session_is_valid,
        request.app.state.db_pool,
        cookie_token,
    ):
        request.state.auth_principal = AuthPrincipal("cluster", "administrator")
        request.state.source_verified_by = "user_session"
        origin = request.headers.get("Origin")
        # Origin is checked only after valid cookie auth; without it, the request
        # reaches 401 unless another explicit credential authenticates it.
        # Bearer callers carry no ambient credential; login is exempt by design,
        # with SameSite=Lax and the rate limiter bounding its CSRF exposure.
        # Exact mutation origins also close the same-site-subdomain vector.
        if (
            request.method in _STATE_CHANGING_METHODS
            and origin is not None
            and origin not in cors_allowed_origins()
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "origin not allowed"},
                headers={"Vary": "Origin"},
            )
        now = time.monotonic()
        last_touch = _session_last_touch.get(cookie_token)
        touch_due = last_touch is None or now - last_touch >= _SESSION_TOUCH_INTERVAL_S
        if touch_due:
            _session_last_touch[cookie_token] = now
        _prune_session_last_touch(now)
        if touch_due:
            await asyncio.to_thread(
                touch_session,
                request.app.state.db_pool,
                cookie_token,
            )
        return await call_next(request)

    # 2. Check Bearer token
    authorization = request.headers.get("Authorization")
    if verify_bearer(authorization, secret):
        request.state.auth_principal = AuthPrincipal("cluster", "administrator")
        request.state.source_verified_by = "cluster_bearer"
        return await call_next(request)

    _log_auth401_rejection(request)
    return error_response(
        request,
        code="authentication_required",
        status=401,
        detail="authentication required",
        retryable=False,
    )


# Per-endpoint latency metering (Task #1091). Registered near-last so it is
# near-OUTERMOST — Starlette builds the stack in reverse registration order —
# and the measurement covers the full pipeline: pause gate, idempotency
# dedup, and auth. `await call_next` returns at response headers, so SSE /
# long-poll connections count time-to-first-byte, not lifetime.
app.middleware("http")(_latency.latency_middleware)


# CORSMiddleware is registered AFTER latency — the OUTERMOST middleware — so
# allowlisted responses carry Access-Control-* headers even when inner
# middleware short-circuits with 401 or 503. Unhandled route exceptions need
# the Exception handler below because ServerErrorMiddleware sits outside user
# middleware (#187). Browser origins are exact matches: an explicit setting is
# authoritative; otherwise the allowlist derives the local frontend origins and
# the gateway URL's own origin (its own scheme, host, and port). A CORS
# preflight (OPTIONS) is answered here before auth or pause ever see it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Registered last so every response path, including auth and pause short-circuits,
# receives a fallback trace id before it reaches an error response builder.
app.middleware("http")(request_trace_middleware)


app.add_exception_handler(AvaAgentError, _ava_agent_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(
    loki_query_budget.LokiQueryBudgetError,
    _loki_query_budget_error_handler,  # type: ignore[arg-type]
)
app.add_exception_handler(
    loki_events.ObservabilityReadUnavailable,
    _observability_read_unavailable_handler,  # type: ignore[arg-type]
)
app.add_exception_handler(
    prom_metrics.PromQueryBudgetError,
    _prom_query_budget_error_handler,  # type: ignore[arg-type]
)
app.add_exception_handler(RequestValidationError, _request_validation_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(Exception, _unhandled_exception_handler)


# `_publish_inbound_arrived` was inlined into the spawn/lifecycle handlers;
# the same implementation now lives in gateway/ops_*.py as the public
# helper `publish_inbound_arrived`, reused by both FastAPI handlers and the
# ava-ops in-process dispatch.


# --- Router registration (endpoints live in gateway/routers/<domain>.py) ---
app.include_router(auth_router.router)
app.include_router(bootstrap_router.router)
app.include_router(agents_router.router)
app.include_router(agents_lifecycle_router.router)
app.include_router(agents_state_router.router)
app.include_router(agent_events_router.router)
app.include_router(computer_traces_router.router)
app.include_router(agent_inspect_router.router)
app.include_router(shell_router.router)
app.include_router(timeline_router.router)
app.include_router(system_router.router)
app.include_router(pages_router.router)
app.include_router(notices_router.router)
app.include_router(cluster_router.router)
app.include_router(machine_pause_router.router)
app.include_router(commands_router.router)
app.include_router(config_router.router)
app.include_router(default_model_router.router)
app.include_router(settings_router.router)
app.include_router(schedules_router.router)
app.include_router(presets_router.router)
app.include_router(guide_router.router)
app.include_router(inventory_router.router)
app.include_router(skills_router.router)
app.include_router(packages_router.router)
app.include_router(metrics_router.router)
app.include_router(events_router.router)
app.include_router(run_timeline_router.router)
app.include_router(event_resolutions_router.router)
app.include_router(ops_monitor_router.router)
app.include_router(alerts_router.router)
app.include_router(status_router.router)
app.include_router(memory_router.router)
app.include_router(mcp_clients_router.router)
app.include_router(fleet_graph_router.router)
app.include_router(frontend_telemetry_router.router)
app.include_router(grafana_router.router)
app.include_router(okf_graph_router.router)
app.include_router(tasks_router.router)
app.include_router(plugin_ui_router.router)
app.include_router(ui_contributions_router.router)
app.include_router(uploads_router.router)
app.include_router(work_failed_router.router)

# /mcp — MCP control plane (design task #1212 step 1). Mounted always; the
# wrapper answers 404 while settings.gateway.mcp_endpoint_enabled is off, so
# the route surface is stable and the flag is a pure on/off switch. Auth is
# the mounted wrapper requires its own revocable client token, including on
# no-secret clusters; cluster cookies and Bearer secrets are not MCP identities.
app.mount("/mcp", _mcp_endpoint.mcp_gateway(app))


def main() -> None:
    """Run the gateway process through the stable `gateway.app` entry point."""
    _run_gateway()
