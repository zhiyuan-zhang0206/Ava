import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from functools import partial
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from base import paths, telemetry
from base.api_contracts.agent_contract import MeivaAgentError
from base.cluster.machine.boot import machine_from_boot
from base.cluster.rate_limit import LoginRateLimitConfig, LoginRateLimiter
from base.config import ConfigBoot, Settings
from base.config.service_read import ConfigAuthority
from base.events.live.bus import EventBus
from base.host.db_authority.api import AcceptanceCache
from base.host.net.auth import cookie_name
from base.host.system.cron import register_os_cron
from base.host.system.hostport import Host
from base.packages.plugin_providers import build_model_catalog
from gateway.agents import launch_retry as agents_launch_retry_router
from gateway.agents import lifecycle as agents_lifecycle_router
from gateway.agents import notices as notices_router
from gateway.agents import router as agents_router
from gateway.agents import state as agents_state_router
from gateway.agents.history import conversation as conversation_router
from gateway.agents.history import timeline as timeline_router
from gateway.agents.history import understanding as agents_understanding_router
from gateway.agents.history.configuration import timeline_policy, understanding_policy
from gateway.agents.notice_operations import router as guarded_notices_router
from gateway.agents.reply_suggestion import router as reply_suggestion_router
from gateway.agents.reply_suggestion.configuration import reply_suggestion_service
from gateway.agents.task_assignment import router as task_assignments_router
from gateway.agents.to_user import document as to_user_router
from gateway.alerts import router as alerts_router
from gateway.cluster import alert_classes as alert_classes_router
from gateway.cluster import bootstrap as bootstrap_router
from gateway.cluster import machine_pause as machine_pause_router
from gateway.cluster import ops_monitor as ops_monitor_router
from gateway.cluster import router as cluster_router
from gateway.cluster import status as status_router
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from gateway.cluster.roster_probe import IdentityMismatchLog
from gateway.cluster.server import main as _run_gateway
from gateway.cluster.status import StatusCache
from gateway.events import agent_events as agent_events_router
from gateway.events import computer_traces as computer_traces_router
from gateway.events import metrics as metrics_router
from gateway.events import resolutions as event_resolutions_router
from gateway.events import router as events_router
from gateway.events import system as system_router
from gateway.extensions import inventory as inventory_router
from gateway.extensions import packages as packages_router
from gateway.extensions import plugin_ui as plugin_ui_router
from gateway.extensions import skills as skills_router
from gateway.extensions import ui_contributions as ui_contributions_router
from gateway.http.auth import rejection_log
from gateway.http.auth import router as auth_router
from gateway.http.auth.request_principal import SessionKeys
from gateway.http.auth.session_store import SessionStore, SessionTouchThrottle, touch_session
from gateway.http.config import GatewayConfig, gateway_config_from_boot
from gateway.http.middleware import latency, pause_policy, runtime_metrics
from gateway.http.middleware.error_envelope import error_response, request_trace_middleware
from gateway.http.middleware.error_handlers import (
    http_exception_handler,
    meiva_agent_error_handler,
    model_catalog_unavailable_handler,
    request_validation_error_handler,
    unhandled_exception_handler,
)
from gateway.http.model_catalog import (
    ModelCatalogUnavailableError,
    load_model_catalog,
    require_catalog,
)
from gateway.inspect import okf_graph as okf_graph_router
from gateway.inspect import router as inspect_router
from gateway.lgtm.telemetry_staleness import TelemetryStaleness
from gateway.mcp_server import endpoint as mcp_server_endpoint
from gateway.mcp_server import router as mcp_server_router
from gateway.routers import (
    commands as commands_router,
)
from gateway.routers import fleet_graph as fleet_graph_router
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
    insights as insights_router,
)
from gateway.routers import (
    memory as memory_router,
)
from gateway.routers import (
    pages as pages_router,
)
from gateway.routers import (
    settings as settings_router,
)
from gateway.routers import (
    shell as shell_router,
)
from gateway.routers import (
    tasks as tasks_router,
)
from gateway.routers.configuration import default_model as default_model_router
from gateway.routers.configuration import runtime as config_router
from gateway.routers.upload import router as uploads_router
from gateway.schedules import router as schedules_router
from gateway.upload_delivery import router as upload_delivery_router
from gateway.upload_delivery.worker import UploadRecovery

_log = logging.getLogger(__name__)


def _closer(resource: Any, method: str) -> Callable[[], Any]:
    def close() -> Any:
        return getattr(resource, method)()

    return close


@asynccontextmanager
async def _startup_releases() -> AsyncGenerator[AsyncExitStack]:

    releases = AsyncExitStack()
    try:
        yield releases
    except BaseException as primary:
        try:
            await releases.aclose()
        except BaseException as cleanup:
            primary.add_note(f"gateway start-up cleanup also failed: {cleanup!r}")
        raise
    await releases.aclose()


def _build_request_resources(app: FastAPI, config: ConfigBoot, releases: AsyncExitStack) -> None:

    machine_token_acceptance: AcceptanceCache = {}
    app.state.machine_token_acceptance = machine_token_acceptance
    app.state.telemetry_staleness = TelemetryStaleness()
    app.state.status_cache = StatusCache()
    app.state.identity_mismatch_log = IdentityMismatchLog()
    inspect_queries: Any = inspect_router.build_query_cache()
    app.state.inspect_query_cache = inspect_queries
    releases.push_async_callback(_closer(inspect_queries, "aclose"))
    app.state.timeline_policy = timeline_policy(config)
    app.state.understanding_policy = understanding_policy(config, app.state.config_authority)
    app.state.reply_suggestions = reply_suggestion_service(
        config,
        db=app.state.db,
        catalog_reader=partial(require_catalog, app.state.catalog),
        rendering=app.state.timeline_policy.rendering,
    )
    app.state.memory_search_gate = memory_router.build_search_gate(
        config.view.services.memory_search_max_concurrency
    )

    def memory_api_key() -> str | None:
        secret = config.view.lm.gemini_api_key
        return None if secret is None else secret.get_secret_value()

    app.state.memory_search_inputs = memory_router.MemorySearchInputs(
        embedding_backend=lambda: config.view.services.embedding_backend,
        backend=lambda: config.view.services.memory_search_backend,
        uri=lambda: config.view.services.memory_search_uri,
        embedding_timeout=lambda: config.view.services.memory_embed_timeout_seconds,
        api_key=memory_api_key,
        acquire_timeout=lambda: config.view.services.memory_search_acquire_timeout_seconds,
        deadline=lambda: config.view.services.memory_search_deadline_seconds,
        rerank_model=lambda: config.view.services.memory_rerank_model,
        rerank_candidates=lambda: config.view.services.memory_rerank_candidates,
        dedup_candidates=lambda: config.view.services.memory_dedup_candidates,
    )
    app.state.memory_graph_cache = memory_router.MemoryGraphCache()
    app.state.auth401_log = rejection_log.AuthRejectionLog()
    app.state.login_limiter = LoginRateLimiter(
        max_failures_reader=lambda: config.view.gateway.login_max_failures,
        lockout_seconds_reader=lambda: config.view.gateway.login_lockout_seconds,
        config=LoginRateLimitConfig(),
    )

    insights_client: Any = insights_router.build_client()
    app.state.insights_client = insights_client
    releases.push_async_callback(_closer(insights_client, "aclose"))


@asynccontextmanager
async def lifespan(
    app: FastAPI,
    *,
    process: GatewayProcess,
    process_ownership: Literal["borrowed", "owned"],
) -> AsyncGenerator[None]:

    if process_ownership not in {"borrowed", "owned"}:
        raise ValueError("gateway process ownership must be borrowed or owned")
    app.state.gateway_process = process
    app.state.machine = process.machine
    app.state.host = process.host
    with process.lifetime() if process_ownership == "owned" else nullcontext(process):
        config = process.config
        app.state.process_image = process.image
        telemetry.init_telemetry(
            process="gateway",
            pipeline=process.event_pipeline(),
            machine_reader=process.machine.name,
        )
        async with _startup_releases() as releases:
            app.state.catalog = load_model_catalog(build_model_catalog)
            app.state.config_authority = ConfigAuthority(
                runtime=config.view,
                all_domains=config.view if config.view.profile is None else Settings(profile=None),
                env_path=paths.meiva_home() / ".env",
            )

            app.state.started_at = time.time()
            app.state.db = process.database()
            app.state.bus = EventBus.from_settings()
            db_pool: Any = app.state.db.pool(max_size=8)
            app.state.db_pool = db_pool
            releases.callback(_closer(db_pool, "close"))
            app.state.sessions = SessionStore(db_pool)
            app.state.session_keys = SessionKeys()
            app.state.session_touch = SessionTouchThrottle()
            app.state.page_host_cache = pages_router.PageHostCache()
            app.state.telemetry_rate_limiter = frontend_telemetry_router.SessionRateLimiter()

            control_db_pool: Any = app.state.db.pool(min_size=1, max_size=2, timeout=2.0)
            app.state.control_db_pool = control_db_pool
            releases.callback(_closer(control_db_pool, "close"))

            _build_request_resources(app, config, releases)

            grafana_client: Any = grafana_router.build_proxy_client()
            app.state.grafana_client = grafana_client
            releases.push_async_callback(_closer(grafana_client, "aclose"))

            try:
                await asyncio.to_thread(
                    register_os_cron,
                    enabled_reader=lambda: config.view.general.os_jobs_enabled,
                    host=process.host,
                )
            except Exception:
                _log.warning("OS health-probe cron registration failed", exc_info=True)

            monitor: Any = runtime_metrics.start_runtime_monitor()
            app.state.runtime_metrics = monitor
            releases.callback(_closer(monitor, "stop"))

            upload_recovery = UploadRecovery(
                app.state.db_pool, app.state.db, app.state.bus, machine=process.machine
            )
            app.state.upload_recovery = upload_recovery

            async with _background_lifetime(app, upload_recovery):
                yield


@asynccontextmanager
async def _background_lifetime(
    app: FastAPI, upload_recovery: UploadRecovery
) -> AsyncGenerator[None]:

    mcp_manager = None
    config: GatewayConfig = app.state.gateway_config
    if config.mcp_endpoint_enabled:
        mcp_manager = mcp_server_endpoint.build_manager(
            app.state.db_pool,
            app.state.db,
            app.state.bus,
            catalog_reader=lambda: app.state.catalog,
            authority=app.state.config_authority,
            image=app.state.process_image,
            local_machine=app.state.machine,
        )
        app.state.mcp_manager = mcp_manager

    try:
        async with asyncio.TaskGroup() as background:
            latency_flusher = background.create_task(latency.latency_flusher())
            auth401_flusher = background.create_task(
                rejection_log.auth401_flusher(app.state.auth401_log)
            )
            upload_recovery.start(background)
            try:
                if mcp_manager is not None:
                    async with mcp_manager.run():
                        yield
                else:
                    yield
            finally:
                latency_flusher.cancel()
                auth401_flusher.cancel()
                await upload_recovery.close()
    finally:
        app.state.mcp_manager = None


async def cluster_is_paused(_request: Request) -> bool:

    from base.deploy.maintenance.admission import business_paused

    return await asyncio.to_thread(business_paused)


async def cluster_pause_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:

    if not pause_policy.should_bypass_pause(
        request.method, request.url.path
    ) and await cluster_is_paused(request):
        return error_response(
            request,
            code="cluster_updating",
            status=503,
            detail="cluster updating, retry shortly",
            retryable=True,
            headers={"Retry-After": "30"},
        )
    return await call_next(request)


_AUTH_BYPASS_PATHS: frozenset[str] = frozenset(
    {
        "/api/health",
        "/api/auth/login",
        "/api/auth/login-ticket/redeem",
        "/api/auth/check",
        "/api/auth/logout",
        "/mcp",
        "/mcp/",
    }
)
_AUTH_BYPASS_METHOD_PATHS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/alerts"),
    }
)
_STATE_CHANGING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def _cookie_session(request: Request, secret: str) -> tuple[str, str] | None:

    from gateway.http.auth.request_principal import current_session_fact

    cookie_token = request.cookies.get(cookie_name())
    if not cookie_token:
        return None
    fact = await asyncio.to_thread(
        current_session_fact,
        request.app.state.sessions,
        request.app.state.session_keys,
        cookie_token,
        secret,
        cache=request.app.state.machine_token_acceptance,
    )
    return None if fact is None else (cookie_token, fact)


async def cluster_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:

    from gateway.http.auth.request_principal import AuthPrincipal, cluster_credential

    request.state.auth_principal = None
    request.state.source_verified_by = None
    config: GatewayConfig = request.app.state.gateway_config
    secret = config.cluster_secret

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

    session = await _cookie_session(request, secret)
    if session is not None:
        cookie_token, session_fact = session
        request.state.auth_principal = AuthPrincipal("cluster", "administrator")
        request.state.source_verified_by = session_fact
        origin = request.headers.get("Origin")

        if (
            request.method in _STATE_CHANGING_METHODS
            and origin is not None
            and origin not in config.allowed_origins
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "origin not allowed"},
                headers={"Vary": "Origin"},
            )
        now = time.monotonic()
        touch: SessionTouchThrottle = request.app.state.session_touch
        if touch.due(cookie_token, now, stale_after_s=config.session_ttl_seconds):
            await asyncio.to_thread(
                touch_session,
                request.app.state.db_pool,
                cookie_token,
            )
        return await call_next(request)

    verified_by = cluster_credential(
        request.headers.get("Authorization"),
        secret,
        cache=request.app.state.machine_token_acceptance,
    )
    if verified_by is not None:
        request.state.auth_principal = AuthPrincipal("cluster", "administrator")
        request.state.source_verified_by = verified_by
        return await call_next(request)

    request.app.state.auth401_log.log(request)
    return error_response(
        request,
        code="authentication_required",
        status=401,
        detail="authentication required",
        retryable=False,
    )


# meiva:ref gateway/docs/error-responses-and-middleware-order.meiva.okf.md#error-response-cors
def create_app(
    config: GatewayConfig,
    *,
    process: GatewayProcess,
    process_ownership: Literal["borrowed", "owned"],
) -> FastAPI:

    if process_ownership not in {"borrowed", "owned"}:
        raise ValueError("gateway process ownership must be borrowed or owned")
    app = FastAPI(
        title="Meiva",
        lifespan=partial(lifespan, process=process, process_ownership=process_ownership),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.gateway_config = config
    app.state.gateway_process = process
    machine_token_acceptance: AcceptanceCache = {}
    app.state.machine_token_acceptance = machine_token_acceptance
    app.middleware("http")(cluster_pause_middleware)
    app.middleware("http")(cluster_auth_middleware)

    app.middleware("http")(latency.latency_middleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.middleware("http")(request_trace_middleware)

    app.add_exception_handler(MeivaAgentError, meiva_agent_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.add_exception_handler(ModelCatalogUnavailableError, model_catalog_unavailable_handler)

    for router in (
        auth_router.router,
        bootstrap_router.router,
        agents_router.router,
        to_user_router.router,
        agents_launch_retry_router.router,
        agents_lifecycle_router.router,
        agents_understanding_router.router,
        agents_state_router.router,
        agent_events_router.router,
        computer_traces_router.router,
        inspect_router.router,
        shell_router.router,
        timeline_router.router,
        reply_suggestion_router.router,
        system_router.router,
        pages_router.router,
        notices_router.router,
        guarded_notices_router.router,
        cluster_router.router,
        machine_pause_router.router,
        commands_router.router,
        config_router.router,
        conversation_router.router,
        default_model_router.router,
        settings_router.router,
        schedules_router.router,
        guide_router.router,
        inventory_router.router,
        skills_router.router,
        packages_router.router,
        metrics_router.router,
        events_router.router,
        insights_router.router,
        event_resolutions_router.router,
        ops_monitor_router.router,
        alerts_router.router,
        status_router.router,
        alert_classes_router.router,
        memory_router.router,
        mcp_server_router.router,
        fleet_graph_router.router,
        frontend_telemetry_router.router,
        grafana_router.router,
        okf_graph_router.router,
        tasks_router.router,
        task_assignments_router.router,
        plugin_ui_router.router,
        ui_contributions_router.router,
        uploads_router.router,
        upload_delivery_router.router,
    ):
        app.include_router(router)

    app.mount("/mcp", mcp_server_endpoint.mcp_gateway(app))
    return app


def create_app_from_environment() -> FastAPI:

    config = ConfigBoot()
    config.boot()
    config.ensure_eager()
    process = GatewayProcess(
        config=config,
        image=LOADED_IMAGE,
        machine=machine_from_boot(config),
        host=Host.system(),
    )
    return create_app(
        gateway_config_from_boot(process.config), process=process, process_ownership="owned"
    )


def openapi_schema() -> dict[str, Any]:

    config = ConfigBoot()
    process = GatewayProcess(
        config=config, image=LOADED_IMAGE, machine=machine_from_boot(config), host=Host.system()
    )
    schema_config = GatewayConfig(
        cluster_secret="",
        session_ttl_seconds=3600,
        mcp_endpoint_enabled=False,
        allowed_origins=(),
        browser_origin="",
        session_cookie_secure=False,
        memory_root=Path("/nonexistent/memory"),
    )
    with process.lifetime():
        app = create_app(schema_config, process=process, process_ownership="borrowed")
        return app.openapi()


def _borrowed_app(process: GatewayProcess) -> FastAPI:
    return create_app(
        gateway_config_from_boot(process.config), process=process, process_ownership="borrowed"
    )


def main() -> None:

    _run_gateway(build_app=_borrowed_app)
