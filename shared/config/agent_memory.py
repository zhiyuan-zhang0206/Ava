"""Agent memory injection + recall — AgentMemorySettings.

Index-injection toggles (pool index / per-agent index) and the passive recall pipeline (retrieve -> filter -> inject, with model / retries / timeout). Split out of the former flat AgentSettings schema; each field keeps its exact env alias so the .env surface is unchanged."""

from __future__ import annotations

from pydantic import Field

from shared.config._base import EnvSettings


class AgentMemorySettings(EnvSettings):
    memory_index_inject_enabled: bool = Field(
        default=True,
        alias="AVA_MEMORY_INDEX_INJECT",
        description=(
            "Inject the standing memory index (the MEMORY.md pointer at the pool "
            "root) at cold start and after each compact, so durable facts stay in "
            "front of the agent. No-op when MEMORY.md is absent or empty."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    memory_per_agent_inject_enabled: bool = Field(
        default=True,
        alias="AVA_MEMORY_PER_AGENT_INJECT",
        description=(
            "Inject the agent's own memory index (workspace memory/MEMORY.md) at "
            "cold start and after compaction; entry files beside it are read on "
            "demand, not injected. A legacy single-file workspace MEMORY.md is "
            "migrated into the directory on first injection. When absent or empty, "
            "injects the framing with '(no content)'. Independent of "
            "memory_index_inject_enabled."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    memory_per_agent_index_max_lines: int = Field(
        default=200,
        alias="AVA_MEMORY_PER_AGENT_INDEX_MAX_LINES",
        description=(
            "Soft line cap on the agent's own memory index (workspace "
            "memory/MEMORY.md): over it, the injection carries a note nudging the "
            "agent to move detail into entry files and prune. Never truncates. "
            "0 disables the check. 200 matches the Claude Code auto-memory index "
            "scale (replaces the old 8000-char cap on the whole single-file memory; "
            "entry files are uncapped — they cost context only when read)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    passive_memory_recall_enabled: bool = Field(
        default=True,
        alias="AVA_PASSIVE_MEMORY_RECALL",
        description=(
            "Before each turn woken by new inbound, semantically search the memory "
            "pool with the recent conversation as the query and inject the top "
            "matches, so relevant notes surface without the agent asking. A note "
            "already injected this session is not re-injected. No-op when the memory "
            "index is unavailable."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    memory_recall_retrieve_k: int = Field(
        default=100,
        alias="AVA_MEMORY_RECALL_RETRIEVE_K",
        description=(
            "How many notes passive recall retrieves before filtering. Wide "
            "top-100 so the relaxed filter has candidates to judge; injection "
            "stays capped at memory_recall_inject_k."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    memory_recall_inject_k: int = Field(
        default=3,
        alias="AVA_MEMORY_RECALL_INJECT_K",
        description=(
            "The most notes passive recall injects in one turn. Kept small: notes "
            "crowd the context they are meant to inform."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    memory_recall_filter_enabled: bool = Field(
        default=True,
        alias="AVA_MEMORY_RECALL_FILTER",
        description=(
            "Judge each retrieved note against the conversation with a small model "
            "before injecting it, and inject nothing when none fit. Without it "
            "recall injects its top matches unconditionally, so a note that merely "
            "shares a word with the question arrives looking relevant. Off falls "
            "back to the unfiltered top matches."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    memory_recall_filter_model: str = Field(
        default="deepseek-v4-flash",
        alias="AVA_MEMORY_RECALL_FILTER_MODEL",
        description=(
            "Model that judges retrieved notes for passive recall. Runs once per "
            "inbound-woken turn on names and one-line descriptions only, with "
            "reasoning pinned off (~1.5s for a top-100 pass). deepseek-v4-flash "
            "per user ruling (task #595): relaxed prompt + top-100 retrieval "
            "make flash match pro on recall, so the cheap model stays."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    memory_recall_filter_max_retries: int = Field(
        default=3,
        alias="AVA_MEMORY_RECALL_FILTER_MAX_RETRIES",
        description="How many times the recall-filter judging call is retried before a flaky model reply (unparseable output / transient provider error) is reported as a warning and nothing is injected. LLM replies are statistically flaky, so one failure is routine; only exhaustion of every attempt is worth surfacing (user ruling 2026-08-05).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    memory_recall_filter_timeout_seconds: float = Field(
        default=20.0,
        alias="AVA_MEMORY_RECALL_FILTER_TIMEOUT_SECONDS",
        description="Hard bound (seconds) on one recall-filter judging call. It sits in front of the agent's turn, so a slow filter is a slow agent; the bound only fires on a genuinely wedged provider (task #698 G8).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )
