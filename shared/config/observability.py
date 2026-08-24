"""Observability config — ObservabilitySettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

from pydantic import Field

from shared.config._base import EnvSettings


class ObservabilitySettings(EnvSettings):
    trace_tags: str = Field(
        default="",
        alias="AVA_TRACE_TAGS",
        description=(
            "General trace-tag passthrough (CSV): a caller (bench / eval) sets tags "
            "in env and they attach to the root trace span. Agent code doesn't see "
            "tag semantics. Empty = no-op."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    trace_enabled: bool = Field(
        default=True,
        alias="AVA_TRACE_ENABLED",
        description="Export OTel spans to the local OTel Collector sidecar (which fans out to Tempo and writes the JSONL mirror under $AVA_HOME/traces/ via its file exporter). Disable to skip span export entirely — with the sidecar architecture recording IS the export, so the mirror stops too (one kill switch for the whole OTLP surface).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    trace_retention_days: int = Field(
        default=3,
        alias="AVA_TRACE_RETENTION_DAYS",
        description="Delete local trace-mirror files older than this many days (pruned on each agent start; the collector file exporter also rotates by max_days from the same setting, baked into its config at converge). A window not replayed in time is gone.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    trace_strip_content: bool = Field(
        default=True,
        alias="AVA_TRACE_STRIP_CONTENT",
        description="Strip LLM content attributes (gen_ai.task.input/output, traceloop.entity.input/output, messages, system_instructions, ...) from recorded spans. Metadata-only spans: names, langgraph paths, checkpoint refs, agent_id, durations, status, trace_id. Content stays out of the mirror; it is fetched on demand from checkpoints by trace id.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    trace_max_dir_mb: int = Field(
        default=2048,
        alias="AVA_TRACE_MAX_DIR_MB",
        description="Hard cap on the local trace-mirror directory size (MB). When exceeded on an agent start, oldest mirror files are deleted until the directory fits. Bounds disk even when retention_days is wide or the collector rotation is misconfigured.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    trace_disk_watermark: float = Field(
        default=0.9,
        alias="AVA_TRACE_DISK_WATERMARK",
        description="Data-disk usage fraction (0..1) at which trace recording auto-degrades: the agent skips enabling span recording (and logs a warning event) so the mirror can never fill the disk. 1.0 or above disables the guard.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    telemetry_otlp_enabled: bool = Field(
        default=True,
        alias="AVA_TELEMETRY_OTLP_ENABLED",
        description=(
            "Dual-write the unified event stream to the OTLP/HTTP backend "
            "(OTel+Tempo+Loki+Prometheus stack, 2026-08-11 decision): events -> "
            "OTLP logs (Loki), telemetry numeric payloads -> OTLP metrics "
            "(Prometheus). On (default) enables that dual-write. Off leaves the "
            "JSONL mirror only: OTLP export stops entirely, so Loki and Prometheus "
            "stop receiving data and their read surfaces (fleet-graph token totals/"
            "scores, stats-dashboard cost, inspect event history, Grafana) freeze "
            "at the last data. The mirror prevents data loss, but visibility stops. "
            "`ava trace ship` refuses while off (one kill switch for the whole OTLP "
            "surface). There is no Postgres fallback: the PG events copy was retired "
            "with the LGTM cutover (task #1197) and is a read-only archive. Applies "
            "on the next process start."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    telemetry_otlp_endpoint: str = Field(
        default="http://127.0.0.1:4318",
        alias="AVA_TELEMETRY_OTLP_ENDPOINT",
        description=(
            "Base OTLP/HTTP endpoint of the LOCAL OTel Collector sidecar "
            "(standard OTLP port 4318, one sidecar per machine — task #1266). "
            "The live exporter appends /v1/logs + /v1/metrics and the trace "
            "exporter appends /v1/traces. Always points at the local sidecar, "
            "on every machine — agents never dial a backend directly; the "
            "gateway sidecar fans out to loopback backends while a pure runner "
            "sidecar relays to the gateway's authenticated OTLP receiver. "
            "`ava trace ship` bypasses the local sidecar to avoid re-mirroring."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    telemetry_tempo_endpoint: str = Field(
        default="http://127.0.0.1:14318",
        alias="AVA_TELEMETRY_TEMPO_ENDPOINT",
        description=(
            "Tempo OTLP/HTTP base URL. The single-box default is the host "
            "loopback. A cluster's Tempo may be remote through a host-scope "
            "AVA_TELEMETRY_TEMPO_ENDPOINT override, as configured on the "
            "production cluster. A pure runner ignores this URL and relays "
            "through the gateway."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    telemetry_tempo_query_url: str = Field(
        default="http://127.0.0.1:3200",
        alias="AVA_TELEMETRY_TEMPO_QUERY_URL",
        description=(
            "Tempo query/metrics base URL for the Grafana Tempo datasource and "
            "Prometheus scrape of the trace backend. The OTLP intake endpoint is "
            "AVA_TELEMETRY_TEMPO_ENDPOINT on a different port."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    telemetry_loki_url: str = Field(
        default="http://127.0.0.1:3100",
        alias="AVA_TELEMETRY_LOKI_URL",
        description="Loki base URL (single-binary HTTP port) for the LGTM event-history read path — the replacement for PG `events` reads (task #1197).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    telemetry_prometheus_url: str = Field(
        default="http://127.0.0.1:9090",
        alias="AVA_TELEMETRY_PROMETHEUS_URL",
        description="Prometheus base URL (HTTP API port) for the LGTM telemetry-aggregate read path — the replacement for PG `events` llm_usage reads (task #1197).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    otel_collector_metrics_port: int = Field(
        default=8888,
        alias="AVA_OTELCOL_METRICS_PORT",
        description=(
            "TCP port where the local OTel Collector sidecar exposes its own /metrics, "
            "baked into service.telemetry.metrics.address at converge. Two Ava units "
            "on one machine must use different ports or the second collector cannot "
            "start (2026-08-24 WSL prod/preview collision)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
