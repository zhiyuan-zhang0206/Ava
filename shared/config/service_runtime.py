"""Shared browser, helper, and memory-service runtime settings."""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field

from shared.config._base import EnvSettings, _unit_home

__all__ = [
    "_ServiceRuntimeSettings",
]


class _ServiceRuntimeSettings(EnvSettings):
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

    browser_reach_probe_interval_s: int = Field(
        default=300,
        alias="AVA_BROWSER_REACH_PROBE_INTERVAL_S",
        description="Throttle for the browser-reach canary (seconds): the canary opens and closes one background about:blank target in the shared headed browser, so probing every watchdog round would churn the user's tab strip. 300s keeps that negligible while bounding a sticky hang to threshold*interval (~15 min). 0 disables the throttle.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    browser_reach_timeout_s: float = Field(
        default=6.0,
        alias="AVA_BROWSER_REACH_TIMEOUT_S",
        description="Wall-clock budget for one browser-reach canary (seconds): bounds CDP target setup plus the in-page fetch. The 2026-09-18 hang left fetches pending for 9s+, so a smaller budget separates a quick network reject from a hang without stalling the watchdog round; the host-path control read uses the same budget.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    browser_reach_failure_threshold: int = Field(
        default=3,
        alias="AVA_BROWSER_REACH_FAILURE_THRESHOLD",
        description="Consecutive failing browser-reach probes before the single ERROR (single-round jitter suppression); the episode then stays quiet until a healthy probe re-arms it. 3 probes at the 300s default interval bound detection at ~15 min.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
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

    # Gray rollout: installing the helper remains independent from entrusting
    # every long-lived macOS process spawn to it. Operators enable this only
    # after the helper nursery protocol is deployed and healthy on the host.
    permissions_helper_spawn: bool = Field(
        default=False,
        alias="AVA_PERMISSIONS_HELPER_SPAWN",
        description="Spawn macOS service, agent, and PTY-host processes directly through the permissions helper so they inherit its stable TCC identity.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    # Gray rollout: the root supervisor replaces the per-service session
    # launch at `ava start` / `ava stop` once a host is switched. It is a
    # host-scope commitment (identity, logging and recovery all move with it),
    # so the default stays off until a host is deliberately switched — the
    # session path is unchanged otherwise.
    root_driver_enabled: bool = Field(
        default=False,
        alias="AVA_ROOT_DRIVER_ENABLED",
        description=(
            "Bring this host's service tree up/down through the ava-root supervisor "
            "(manifests -> units) instead of launching service sessions directly. "
            "Default off: the session path stays until a host is switched."
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

    permissions_helper_keychain: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AVA_PERMISSIONS_HELPER_KEYCHAIN"),
        serialization_alias="AVA_PERMISSIONS_HELPER_KEYCHAIN",
        description="Optional keychain path override for the permissions helper's signing identity. None = the user's login keychain; CI sets this to an isolated keychain it owns.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
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

    memory_indexer_reconcile_retry_backoff_seconds: float = Field(
        default=60.0,
        gt=0,
        alias="AVA_MEMORY_INDEXER_RECONCILE_RETRY_BACKOFF_SECONDS",
        description=(
            "Base delay before the indexer daemon retries an incomplete reconcile "
            "pass. The first incomplete signal uses this base; consecutive "
            "signals (incomplete passes or drain failures that arm an idle "
            "schedule) double the delay, capped by "
            "memory_indexer_reconcile_retry_backoff_cap_seconds. The 60s base "
            "covers a per-minute embedding quota window; consecutive failures "
            "back off to the cap. Takes effect on indexer restart."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    memory_indexer_reconcile_retry_backoff_cap_seconds: float = Field(
        default=900.0,
        gt=0,
        alias="AVA_MEMORY_INDEXER_RECONCILE_RETRY_BACKOFF_CAP_SECONDS",
        description=(
            "Ceiling of the reconcile retry backoff. 15min bounds the steady-state "
            "cost of a long-exhausted quota (e.g. a daily limit) at one probe round "
            "per 15 minutes, with the next attempt starting within 15 minutes "
            "of the quota resetting — the daemon never needs a restart to "
            "close the gap. Takes effect on indexer restart."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
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
            # The one official repair surface for a hosted-runner port that
            # collides on a mirrored localhost namespace: `.env` hand-edits were
            # the only fix during the 2026-09-02 win/wsl 8114 incident (the
            # field was reserved read-only before the service existed). Host
            # scope stays host-writable; remote_writable=False keeps a remote
            # `--machine` set out, like every other host-scope key.
            "writable": True,
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

    memory_search_max_batch_rows: int = Field(
        default=65536,
        gt=0,
        alias="AVA_MEMORY_SEARCH_MAX_BATCH_ROWS",
        description=(
            "Row-count cap on one memory-search batch request body (/upsert_batch, "
            "/delete_stale_batch), enforced by the wire models. Loopback-only is the "
            "trust model, not a security boundary, so one request's parse and "
            "allocation stay bounded; 65536 is far above the indexer's normal batch "
            "while keeping the worst case bounded. Resolved at import in the "
            "memory-search app — takes effect on its restart."
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
        description=(
            "HTTP operation timeout and enforced total attempt deadline (seconds) for Gemini "
            "batchEmbedContents. Sync streaming checks the deadline between chunks, allowing "
            "one extra read window. The memory-indexer liveness ceiling is derived as "
            "max(180s, provider batch retry budget + 30s)."
        ),
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
