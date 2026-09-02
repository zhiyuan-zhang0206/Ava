"""Services config — ServiceSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, cast

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import NoDecode

from shared.config._base import EnvSettings, _unit_home


class ServiceSettings(EnvSettings):
    browser_enabled: bool = Field(
        default=True,
        alias="AVA_BROWSER_ENABLED",
        description="Run the shared headed Chrome on this agent-runner. Auto-skips when the host lacks display/Chrome.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    chrome_binary: str | None = Field(
        default=None,
        alias="AVA_CHROME_BINARY",
        description="Override path to the Chrome binary. None = resolve the platform default.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    browser_cdp_port: int = Field(
        default=9222,
        alias="AVA_BROWSER_CDP_PORT",
        description="Chrome remote-debugging (CDP) port the shared browser binds and chrome-devtools-mcp attaches to. Per-cluster so co-hosted clusters don't share one Chrome.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    permissions_helper_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "AVA_PERMISSIONS_HELPER_ENABLED", "AVA_NATIVE_HELPER_ENABLED"
        ),
        serialization_alias="AVA_PERMISSIONS_HELPER_ENABLED",
        description="Run the signed macOS permissions helper on this agent-runner. Auto-skips when the host isn't a capable macOS box.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    permissions_helper_port: int = Field(
        default=9223,
        validation_alias=AliasChoices("AVA_PERMISSIONS_HELPER_PORT", "AVA_NATIVE_HELPER_PORT"),
        serialization_alias="AVA_PERMISSIONS_HELPER_PORT",
        description="Port key for the macOS permissions helper's Unix socket. Per-cluster so co-hosted clusters get distinct sockets.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    project_root: Path | None = Field(
        default=None,
        alias="AVA_PROJECT_ROOT",
        description="Repo root directory — used as cwd when healthcheck restarts a service. None = derive from `__file__`.",
        json_schema_extra={
            "capability": "common",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    memory_root: Path = Field(
        default_factory=lambda: _unit_home() / "memory",
        alias="AVA_MEMORY_ROOT",
        description="Root directory of the ava.memory pool's markdown files; the memory indexer watches this path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    memory_indexer_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "memory_indexer.pid",
        alias="AVA_MEMORY_INDEXER_PIDFILE",
        description="Memory indexer daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    memory_indexer_health_url: str = Field(
        default="",
        alias="AVA_MEMORY_INDEXER_HEALTH_URL",
        description="Memory indexer healthcheck URL. Empty = derive via shared.daemon_health.health_port('memory_indexer').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    page_server_health_url: str = Field(
        default="",
        alias="AVA_PAGE_SERVER_HEALTH_URL",
        description="Page server supervisor healthcheck URL. Empty = derive via shared.daemon_health.health_port('page-server').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    agent_host_health_port: int | None = Field(
        default=None,
        alias="AVA_AGENT_HOST_HEALTH_PORT",
        description="Hosted agent-runner healthz port. None = derive from the unit's port block.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    page_server_health_port: int | None = Field(
        default=None,
        alias="AVA_PAGE_SERVER_HEALTH_PORT",
        description="Page server supervisor healthz port. None = derive from the unit's port block.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    milvus_uri: str = Field(
        default="http://127.0.0.1:19530",
        alias="AVA_MILVUS_URI",
        description="Milvus standalone server URI — memory_indexer / ava.memory.search both connect via this.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    memory_search_backend: str = Field(
        default="numpy",
        alias="AVA_MEMORY_SEARCH_BACKEND",
        description=(
            "Storage backend for the memory embedding index — 'milvus' | "
            "'numpy' (default; the local exact-search service) | 'pgvector' (the cluster "
            "Postgres as vector store — the vendored runtime injects the pinned "
            "pgvector files and `ava start` pre-creates the extension, so it works "
            "on the zero-manual-install path; a Postgres without the binaries "
            "still fails the startup preflight fast with the actionable fix). New "
            "backends land behind the same switch. The "
            "indexer daemon and the gateway search endpoint both read it — switching "
            "is one env var + a restart, the cold-start scan rebuilds the index."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    memory_search_max_concurrency: int = Field(
        default=20,
        alias="AVA_MEMORY_SEARCH_MAX_CONCURRENCY",
        description=(
            "Max concurrent /api/memory/search requests the gateway runs at once "
            "(the query-embed phase concurrency gate). The historical default 2 "
            "predated the async-embed fix (2026-08-03) and starved recall behind "
            "a queue during fleet-wake bursts; query embeds are single short "
            "Gemini calls on a paid Tier-2 key, so 20 is safe. Takes effect on "
            "gateway restart."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    memory_search_deadline_seconds: float = Field(
        default=15.0,
        alias="AVA_MEMORY_SEARCH_DEADLINE_SECONDS",
        description=(
            "Wall-clock budget for one POST /api/memory/search, covering the embed and "
            "the milvus round-trip. Must stay below the SDK's gateway HTTP timeout, or "
            "the caller reads out first and the server-side deadline never bites. Also "
            "the anchor of the SDK client's per-attempt timeout (deadline + 3s) and of "
            "memory_search_acquire_timeout_seconds, which must stay below this for the "
            "fast-fail to be the binding constraint on a congested gate."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    memory_search_acquire_timeout_seconds: float = Field(
        default=1.0,
        alias="AVA_MEMORY_SEARCH_ACQUIRE_TIMEOUT_SECONDS",
        description=(
            "How long one POST /api/memory/search may wait for a permit on the "
            "query-embed concurrency gate before failing fast (503 indexer_unavailable). "
            "A request parked in acquire behind a congested gate is exactly as stuck as "
            "one parked in the backend, but waiting there is not waiting for a backend: "
            "it only re-enters the same queue, so a fleet wake that saturates the gate "
            "should answer in ~1s and let passive recall's own deadline degrade quickly "
            "instead of queueing behind the whole gate (2026-08-29 storm). Must stay "
            "below memory_search_deadline_seconds, or the deadline is the binding "
            "constraint and this knob never bites."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    embedding_backend: str = Field(
        default="gemini",
        alias="AVA_EMBEDDING_BACKEND",
        description=(
            "Embedding provider for the memory index — 'gemini' (default: "
            "the Gemini Embedding 2 REST adapter). Unknown values fail fast "
            "at startup (never a silent fallback to the default). The vector "
            "space is a provider property: switching providers changes the "
            "semantic space even at the same dim, so the indexer detects the "
            "provider-fingerprint change and rebuilds the index from scratch."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    memory_embed_timeout_seconds: float = Field(
        default=60.0,
        alias="AVA_EMBED_TIMEOUT_SECONDS",
        description="Per-request timeout (seconds) for one Gemini batchEmbedContents call. A 32-file batch is one round-trip; the memory-indexer daemon's liveness timeout (180s) sits well above it, so a slow-but-legit batch does not trip the healthcheck (task #698 G8).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    milvus_port: int = Field(
        default=19530,
        alias="AVA_MILVUS_PORT",
        description="Milvus standalone server gRPC port.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    milvus_data_dir: Path = Field(
        default_factory=lambda: _unit_home() / "milvus-data",
        alias="AVA_MILVUS_DATA_DIR",
        description="Milvus on-disk data directory.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    memory_search_port: int = Field(
        default=19531,
        alias="AVA_MEMORY_SEARCH_PORT",
        description="Memory search service HTTP port (one past milvus's 19530).",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    memory_search_uri: str = Field(
        default="http://127.0.0.1:19531",
        alias="AVA_MEMORY_SEARCH_URI",
        description="Memory search service base URI — indexer daemon / gateway dial it when the numpy backend is selected.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    memory_search_data_dir: Path = Field(
        default_factory=lambda: _unit_home() / "memory-search",
        alias="AVA_MEMORY_SEARCH_DATA_DIR",
        description="Memory search on-disk data directory (vectors.npz).",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    memory_search_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "memory_search.pid",
        alias="AVA_MEMORY_SEARCH_PIDFILE",
        description="Memory search daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    restarter_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "restarter.pid",
        alias="AVA_RESTARTER_PIDFILE",
        description="Restarter daemon pidfile path.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    agent_host_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "agent-host.pid",
        alias="AVA_AGENT_HOST_PIDFILE",
        description="Hosted agent-runner daemon pidfile path.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    page_server_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "page-server.pid",
        alias="AVA_PAGE_SERVER_PIDFILE",
        description="Page server supervisor daemon pidfile path.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    labeler_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "labeler.pid",
        alias="AVA_LABELER_PIDFILE",
        description="Labeler daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    heartbeat_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "heartbeat.pid",
        alias="AVA_HEARTBEAT_PIDFILE",
        description="Heartbeat daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    delivery_watchdog_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "delivery_watchdog.pid",
        alias="AVA_DELIVERY_WATCHDOG_PIDFILE",
        description="Delivery watchdog daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    task_maintenance_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "task_maintenance.pid",
        alias="AVA_TASK_MAINTENANCE_PIDFILE",
        description="Task-maintenance daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    events_maintenance_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "events_maintenance.pid",
        alias="AVA_EVENTS_MAINTENANCE_PIDFILE",
        description="Events-maintenance daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    pg_backup_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "pg_backup.pid",
        alias="AVA_PG_BACKUP_PIDFILE",
        description="Postgres backup scheduler daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
    pitr_uploader_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "pitr_uploader.pid",
        alias="AVA_PITR_UPLOADER_PIDFILE",
        description="PITR uploader daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
    pitr_base_backup_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "pitr_base_backup.pid",
        alias="AVA_PITR_BASE_BACKUP_PIDFILE",
        description="PITR base candidate scheduler daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    gateway_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "gateway.pid",
        alias="AVA_GATEWAY_PIDFILE",
        description="Gateway uvicorn pidfile path. healthcheck goes via HTTP; this is auxiliary only.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    gateway_watchdog_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "gateway-watchdog.pid",
        alias="AVA_GATEWAY_WATCHDOG_PIDFILE",
        description="Gateway-capability watchdog daemon pidfile path (the monitor itself, no one monitors it).",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    agent_runner_watchdog_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "agent-runner-watchdog.pid",
        alias="AVA_AGENT_RUNNER_WATCHDOG_PIDFILE",
        description="Agent-runner-capability watchdog daemon pidfile path (the monitor itself, no one monitors it).",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    frontend_healthcheck_url: str = Field(
        default="http://localhost:3000",
        alias="AVA_FRONTEND_HEALTHCHECK_URL",
        description="The fleet UI entry the user reaches — the always-up gate's port (the Next.js app itself binds AVA_APP_PORT and is proxied).",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    app_port: int | None = Field(
        default=None,
        alias="AVA_APP_PORT",
        description="The Next.js app port the gate proxies to (the gate owns the entry port). Unset = entry port + 1; converge writes the explicit value from the cluster record.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    gateway_health_url: str = Field(
        default="http://localhost:8000/api/health",
        alias="AVA_GATEWAY_HEALTH_URL",
        description="Gateway healthcheck probe URL. Pure agent-runners derive it from AVA_GATEWAY_URL when no host override is set; gateway-capable units default to the local gateway.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    labeler_health_url: str = Field(
        default="",
        alias="AVA_LABELER_HEALTH_URL",
        description="Labeler healthcheck URL. Empty = derive via shared.daemon_health.health_port('labeler').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    im_bridge_enabled: bool = Field(
        default=True,
        alias="AVA_IM_BRIDGE_ENABLED",
        description="Whether the im_bridge service is part of this cluster's service roster. A cluster with no IM adapters configured (all Telegram/Weixin/Feishu credentials absent) can disable the service: its daemon exits immediately with zero adapters, and the watchdog's healthcheck then fails it every round (2026-08-10 preview noise).",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    im_bridge_health_url: str = Field(
        default="",
        alias="AVA_IM_BRIDGE_HEALTH_URL",
        description="IM Bridge healthcheck URL. Empty = derive via shared.daemon_health.health_port('im_bridge').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    im_disabled_adapters: Annotated[list[str], NoDecode] = Field(
        default=[],
        alias="AVA_IM_DISABLED_ADAPTERS",
        description="Comma-separated IM adapter names to skip at daemon load (code stays; e.g. 'weixin,feishu' — user ruling 2026-08-06 keeps only Telegram live).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    im_send_retry_delays: Annotated[list[float], NoDecode] = Field(
        default=[2.0, 4.0, 8.0, 16.0, 32.0],
        alias="AVA_IM_SEND_RETRY_DELAYS",
        description="Comma-separated backoff delays (seconds) between gateway enqueue retries. A gateway mid-rollout is down for roughly a minute; 2+4+8+16+32 covers it, then the message is dropped (task #698 G8).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    im_sse_read_timeout_seconds: float = Field(
        default=120.0,
        alias="AVA_IM_SSE_READ_TIMEOUT_SECONDS",
        description="Read timeout (seconds) for the IM Bridge SSE subscription stream. The stream is long-lived and mostly idle (the gateway sends a keep-alive comment about once a second), so this only trips on a genuinely dead connection (task #698 G8).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    heartbeat_health_url: str = Field(
        default="",
        alias="AVA_HEARTBEAT_HEALTH_URL",
        description="Heartbeat healthcheck URL. Empty = derive via shared.daemon_health.health_port('heartbeat').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    im_bridge_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "im_bridge.pid",
        alias="AVA_IM_BRIDGE_PIDFILE",
        description="IM Bridge daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    task_maintenance_health_url: str = Field(
        default="",
        alias="AVA_TASK_MAINTENANCE_HEALTH_URL",
        description="Task-maintenance healthcheck URL. Empty = derive via shared.daemon_health.health_port('task_maintenance').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    events_maintenance_health_url: str = Field(
        default="",
        alias="AVA_EVENTS_MAINTENANCE_HEALTH_URL",
        description="Events-maintenance healthcheck URL. Empty = derive via shared.daemon_health.health_port('events_maintenance').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    pg_backup_health_url: str = Field(
        default="",
        alias="AVA_PG_BACKUP_HEALTH_URL",
        description="Postgres backup scheduler healthcheck URL. Empty = derive via shared.daemon_health.health_port('pg_backup').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    restarter_health_url: str = Field(
        default="",
        alias="AVA_RESTARTER_HEALTH_URL",
        description="Restarter daemon healthcheck URL. Empty = derive via health_port('restarter').",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    watchdog_interval_seconds: float = Field(
        default=60.0,
        alias="AVA_WATCHDOG_INTERVAL_SECONDS",
        description="Watchdog daemon healthcheck round interval (seconds).",
        json_schema_extra={
            "capability": "common",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    watchdog_respawn_backoff_cap_seconds: float = Field(
        default=1800.0,
        alias="AVA_WATCHDOG_RESPAWN_BACKOFF_CAP_SECONDS",
        description=(
            "Watchdog respawn exponential-backoff ceiling (seconds). After a failed "
            "respawn, the next attempt is delayed by base * 2^n (base = watchdog round "
            "interval), capped at this value; a condition a respawn cannot cure stops "
            "being hammered once the delay exceeds it."
        ),
        json_schema_extra={
            "capability": "common",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    watchdog_respawn_breaker_rounds: int = Field(
        default=5,
        alias="AVA_WATCHDOG_RESPAWN_BREAKER_ROUNDS",
        description=(
            "Watchdog respawn circuit breaker: consecutive rounds without a probe-alive "
            "verdict that open the breaker — respawns stop and hold, with one "
            "respawn_breaker_open alert per episode, until a round probes alive."
        ),
        json_schema_extra={
            "capability": "common",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    # ── daemon /healthz ports — host scope, one unit at a time ───────────────
    #
    # `host`, not `cluster-pinned`, because a port block is a property of the
    # CLUSTER while the collision domain is one MACHINE's localhost namespace.
    # Those coincide until a machine carries two localhost namespaces (WSL2,
    # containers, netns) — on 2026-07-26 a WSL2 runner and a native Windows
    # runner of the same cluster held the same ports by construction and the
    # WSL2 relay republished the Linux daemons on the Windows loopback, so the
    # Windows watchdog probed its own port and was answered by the other unit
    # (issue #977). Nothing about a health port is cluster-constrained: the
    # runner computes its own ops URL from its own `health_port('ops')` and
    # registers it (`shared/machines.py`), and the gateway reads that URL back
    # off the machines row. So the gateway no longer serves these to runners
    # over /api/bootstrap — and a runner's .env never caches a gateway-served
    # value at all since the 2026-08-01 config refactor (every runner process
    # fetches at startup), so a per-unit port is durable by construction. A
    # co-located
    # second unit states its base once with `ava enroll --health-port-base`;
    # `ava start` refuses to launch onto a port another unit already answers on.
    # The sibling `*_health_url` / `*_pidfile` fields were already `host`.
    gateway_watchdog_health_port: int | None = Field(
        default=None,
        alias="AVA_GATEWAY_WATCHDOG_HEALTH_PORT",
        description="Gateway watchdog /healthz port override (per unit). Unset = shared default 8119.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    agent_runner_watchdog_health_port: int | None = Field(
        default=None,
        alias="AVA_AGENT_RUNNER_WATCHDOG_HEALTH_PORT",
        description="Agent-runner watchdog /healthz port override (per unit). Unset = shared default 8120.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    restarter_health_port: int | None = Field(
        default=None,
        alias="AVA_RESTARTER_HEALTH_PORT",
        description="Restarter daemon /healthz port override (per unit). Unset = shared default 8102.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    labeler_health_port: int | None = Field(
        default=None,
        alias="AVA_LABELER_HEALTH_PORT",
        description="Labeler daemon /healthz port override (per unit). Unset = shared default 8103.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    im_bridge_health_port: int | None = Field(
        default=None,
        alias="AVA_IM_BRIDGE_HEALTH_PORT",
        description="IM Bridge daemon /healthz port override (per unit). Unset = shared default 8111.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    heartbeat_health_port: int | None = Field(
        default=None,
        alias="AVA_HEARTBEAT_HEALTH_PORT",
        description="Heartbeat daemon /healthz port override (per unit). Unset = shared default 8107.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    delivery_watchdog_health_port: int | None = Field(
        default=None,
        alias="AVA_DELIVERY_WATCHDOG_HEALTH_PORT",
        description="Delivery watchdog /healthz port override (per unit). Unset = shared default 8110.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    delivery_watchdog_health_url: str = Field(
        default="",
        alias="AVA_DELIVERY_WATCHDOG_HEALTH_URL",
        description="Delivery watchdog healthcheck URL. Empty = derive via shared.daemon_health.health_port('delivery_watchdog').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    task_maintenance_health_port: int | None = Field(
        default=None,
        alias="AVA_TASK_MAINTENANCE_HEALTH_PORT",
        description="Task-maintenance daemon /healthz port override (per unit). Unset = shared default 8108.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    events_maintenance_health_port: int | None = Field(
        default=None,
        alias="AVA_EVENTS_MAINTENANCE_HEALTH_PORT",
        description="Events-maintenance daemon /healthz port override (per unit). Unset = shared default 8109.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    pg_backup_health_port: int | None = Field(
        default=None,
        alias="AVA_PG_BACKUP_HEALTH_PORT",
        description="Postgres backup scheduler /healthz port override (per unit). Unset = shared default 8116.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
    pitr_uploader_health_port: int | None = Field(
        default=None,
        alias="AVA_PITR_UPLOADER_HEALTH_PORT",
        description="PITR uploader /healthz port override (per unit). Unset = shared default 8117.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
    pitr_base_backup_health_port: int | None = Field(
        default=None,
        alias="AVA_PITR_BASE_BACKUP_HEALTH_PORT",
        description="PITR base candidate scheduler /healthz port override.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    memory_indexer_health_port: int | None = Field(
        default=None,
        alias="AVA_MEMORY_INDEXER_HEALTH_PORT",
        description="Memory indexer daemon /healthz port override (per unit). Unset = shared default 8105.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    ops_health_port: int | None = Field(
        default=None,
        alias="AVA_OPS_HEALTH_PORT",
        description="ava-ops daemon /healthz + /ops port override (per unit) — the agent-runner's inbound port the gateway dials to run cluster ops; the runner registers the resulting URL itself. Unset = shared default 8106.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    ops_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "ops.pid",
        alias="AVA_OPS_PIDFILE",
        description="ava-ops daemon pidfile path.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    ops_concurrency: int = Field(
        default=8,
        alias="AVA_OPS_CONCURRENCY",
        description="Max concurrent cluster ops the agent-runner executes; further inbound /ops requests queue. Prevents one fan-out from overwhelming the runner.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "ops",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    @field_validator("im_send_retry_delays", mode="before")
    @classmethod
    def _parse_delay_list(cls, v: object) -> object:
        """Env values for delay-list fields arrive as raw strings (NoDecode
        keeps pydantic-settings from JSON-decoding them). Accept both spellings:
        a JSON array ("[1.0, 5.0]") or a comma-separated list ("1.0,5.0")."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return [float(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("im_disabled_adapters", mode="before")
    @classmethod
    def _parse_str_list(cls, v: object) -> object:
        """AVA_IM_DISABLED_ADAPTERS arrives as a raw string (NoDecode).
        Accept a JSON array ("[\"weixin\", \"feishu\"]"), a comma-separated
        list ("weixin,feishu"), or an empty value -> [] (Task #855; P0 fix:
        without this validator any env value raised ValidationError and the
        settings load crashed every runner)."""
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            try:
                parsed = cast("list[Any]", json.loads(s))
                if isinstance(parsed, list):
                    return [x for x in parsed if isinstance(x, str)]
            except json.JSONDecodeError:
                pass  # fail-fast-ok: not JSON -> fall through to comma split
            return [x.strip() for x in s.split(",") if x.strip()]
        return v
