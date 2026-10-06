"""Agent process-runtime knobs — AgentRuntimeSettings.

DB wait/pool timeouts and the node-stall hang diagnostic: operational bounds of the agent process itself, independent of what the prompt contains or how memory/compaction behave. Split out of the former flat AgentSettings schema; each field keeps its exact env alias so the .env surface is unchanged."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from base.config.base import EnvSettings
from base.daemon.schedules.completion_notices import CompletionNoticePolicy


class AgentRuntimeSettings(EnvSettings):
    completion_notice_policy: Literal[CompletionNoticePolicy.ALL, CompletionNoticePolicy.HOURLY] = (
        Field(
            default=CompletionNoticePolicy.ALL,
            alias="AVA_COMPLETION_NOTICE_POLICY",
            description="Per-agent completion notification policy: all delivers one notice per completion, and hourly aggregates every completion in a durable hourly digest.",
            json_schema_extra={
                "restart_required": "agent",
                "writable": True,
                "sensitive": False,
                "scope": "cluster-default",
                "per_agent": True,
                "lifecycle": "live",
            },
        )
    )

    checkpoint_interval: int = Field(
        default=4,
        alias="AVA_CHECKPOINT_INTERVAL",
        description="Persist a LangGraph checkpoint every Nth super-step (4 by default; 1 restores every-step persistence). Crash recovery replays up to N-1 super-steps. Per-agent override, restart required.",
        gt=0,
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    db_notify_wait_timeout_seconds: float = Field(
        default=30.0,
        alias="AVA_DB_NOTIFY_WAIT_TIMEOUT_SECONDS",
        description="Agent-host inbound subscription read timeout and durable pending-work scan interval (seconds). A lost Redis wake is recovered by the next database scan.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    node_stall_dump_seconds: float = Field(
        default=0.0,
        alias="AVA_NODE_STALL_DUMP_SECONDS",
        description="If > 0, dump every thread's stack to stderr when a graph node stays in one node longer than this many seconds (one-shot per node). 0 disables. A hang diagnostic; off in prod, on in the e2e harness.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    db_pool_acquire_timeout_seconds: float = Field(
        default=30.0,
        alias="AVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS",
        description="Max seconds an agent waits to borrow a Postgres connection before raising. Keep it generous: a mid-turn raise exits the agent process, which is not auto-resurrected.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    db_pool_slow_acquire_warn_seconds: float = Field(
        default=10.0,
        alias="AVA_DB_POOL_SLOW_ACQUIRE_WARN_SECONDS",
        description="A successful connection borrow at least this slow is reported as a `db_pool_acquire_slow` event (WARNING). A healthy borrow takes milliseconds, so seconds mean the host is stalled or Postgres is reconnecting; 3s fired on every host-stall check spike (2026-10-03 triage #8: 39 of 41 lines had check_ms == elapsed, a normal reset check being ~30ms). Stays below db_pool_acquire_timeout_seconds so a slow-but-served borrow is visible before it becomes a hard timeout.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    impersonation_ack_window_seconds: int = Field(
        default=180,
        alias="AVA_IMPERSONATION_ACK_WINDOW_SECONDS",
        description="Seconds to ACK each impersonation delivery attempt. Snapshotted when a lease is requested; config edits apply to new leases, including after relay restart.",
        gt=0,
        le=2147483647,
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    impersonation_max_delivery_attempts: int = Field(
        default=2,
        alias="AVA_IMPERSONATION_MAX_DELIVERY_ATTEMPTS",
        description="Maximum total delivery attempts per impersonation message, including the initial submission. Exhaustion pauses automatic delivery of that message; the lease and late ACK remain valid. Snapshotted at request; config edits apply to new leases.",
        gt=0,
        le=2147483647,
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    heartbeat_pause_max_seconds: float = Field(
        default=86400.0,
        alias="AVA_HEARTBEAT_PAUSE_MAX_SECONDS",
        description="Maximum seconds one ava.self.pause_heartbeat(duration) call may pause the idle heartbeat. Cluster default; an agent can override it per-agent via its config overlay (e.g. ava.self.restart(config_overlay=...)).",
        gt=0,
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )
