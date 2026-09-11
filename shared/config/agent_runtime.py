"""Agent process-runtime knobs — AgentRuntimeSettings.

DB wait/pool timeouts and the node-stall hang diagnostic: operational bounds of the agent process itself, independent of what the prompt contains or how memory/compaction behave. Split out of the former flat AgentSettings schema; each field keeps its exact env alias so the .env surface is unchanged."""

from __future__ import annotations

import json

from pydantic import Field, field_validator

from shared.config._base import EnvSettings


class AgentRuntimeSettings(EnvSettings):
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

    checkpoint_max_blob_bytes: int = Field(
        default=16 * 1024 * 1024,
        alias="AVA_CHECKPOINT_MAX_BLOB_BYTES",
        description=(
            "Refuse to write any single checkpoint blob whose serialized size "
            "exceeds this many bytes. Multi-megabyte inline content (e.g. images "
            "in the messages channel) is rewritten in full on every checkpoint, "
            "and a cross-network write of such a blob stalls at the database "
            "statement timeout; the guard fails that write fast and loudly with "
            "a clear error instead. Nothing is trimmed or dropped — the write "
            "does not happen. Raise the limit only as a stopgap."
        ),
        gt=0,
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    checkpoint_max_blob_bytes_overrides: dict[str, int] = Field(
        default_factory=dict,
        alias="AVA_CHECKPOINT_MAX_BLOB_BYTES_OVERRIDES",
        description=(
            "Per-agent override of checkpoint_max_blob_bytes, as a JSON object "
            'mapping agent id to bytes (e.g. {"6093": 33554432}). A thread '
            "without an entry uses the base limit. Exists so an agent whose "
            "stored history already carries an oversized blob can keep writing "
            "on its current host while the storage fix lands, instead of being "
            "locked out by the guard."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    @field_validator("checkpoint_max_blob_bytes_overrides", mode="before")
    @classmethod
    def _parse_blob_limit_overrides(cls, value: object) -> object:
        """Accept the JSON-object environment value (e.g. '{"6093": 33554432}')."""
        if isinstance(value, str):
            return json.loads(value)
        return value

    @field_validator("checkpoint_max_blob_bytes_overrides")
    @classmethod
    def _validate_blob_limit_overrides(cls, value: dict[str, int]) -> dict[str, int]:
        """Reject malformed override entries at config build, not at first write."""
        for agent_id, limit in value.items():
            if not agent_id.isdigit():
                raise ValueError(f"override key must be a numeric agent id, got {agent_id!r}")
            if limit <= 0:
                raise ValueError(f"override for agent {agent_id} must be positive, got {limit}")
        return value

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
