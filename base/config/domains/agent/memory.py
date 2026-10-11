"""Agent memory injection + recall — AgentMemorySettings.

Index-injection toggles (pool index / per-agent index) and the passive recall pipeline (search with gateway-side rerank -> inject notes at or above a score threshold, capped per turn). Split out of the former flat AgentSettings schema; each field keeps its exact env alias so the .env surface is unchanged."""

from __future__ import annotations

from pydantic import Field

from base.config.base import EnvSettings


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
            "demand, not injected. When absent or empty, "
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

    memory_inherit_depth: int = Field(
        default=1,
        ge=0,
        le=10,
        alias="AVA_MEMORY_INHERIT_DEPTH",
        description=(
            "How many ancestors up the birth chain an agent inherits from, in "
            "hops (1 = the direct birth parent), nearest first. 0 disables "
            "inheritance. An ancestor declares shareable content by wrapping it "
            "in `<!-- ava:inheritable -->` … `<!-- /ava:inheritable -->` inside "
            "a personal memory entry; the child reads those blocks when its "
            "context window is established. Cap 10: chains are short, so a "
            "higher value only invites an accidental whole-family pull."
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

    memory_inherit_max_block_chars: int = Field(
        default=4000,
        ge=0,
        alias="AVA_MEMORY_INHERIT_MAX_BLOCK_CHARS",
        description=(
            "Size guardrail for inherited-memory blocks: one declared block "
            "injects at most this many characters; over it the block is "
            "truncated with a visible `[truncated …]` marker plus a warning, so "
            "an oversized declaration cannot balloon every descendant's "
            "context. 0 disables the per-block cap."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    memory_inherit_max_total_chars: int = Field(
        default=16000,
        ge=0,
        alias="AVA_MEMORY_INHERIT_MAX_TOTAL_CHARS",
        description=(
            "Size guardrail for the whole inherited-memory note: once the "
            "accumulated block text reaches this budget, the crossing block is "
            "truncated to the remainder and further blocks are omitted — both "
            "marked visibly in the note and logged as warnings. 0 disables the "
            "total cap."
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

    memory_recall_inject_threshold: float = Field(
        # Calibrated on the 2026-10-11 benchmark: jev-1.13.0 with the rerank question and state
        # templates of services/derived/memory_indexer/rerank.py. Scores are only comparable
        # under that exact model and wording; changing either means recalibrating this value.
        default=0.80,
        ge=0,
        le=1,
        alias="AVA_MEMORY_RECALL_INJECT_THRESHOLD",
        description=(
            "Minimum relevance score (0..1, the decision model's probability that a "
            "note's best chunk is relevant to the conversation) for passive recall to "
            "inject a note. A turn where no note reaches it injects nothing. The default was "
            "calibrated for the default memory_rerank_model and the ranking prompts; "
            "recalibrate it when either changes."
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

    memory_recall_inject_max: int = Field(
        default=3,
        ge=0,
        alias="AVA_MEMORY_RECALL_INJECT_MAX",
        description=(
            "The most notes passive recall injects in one turn, taken best score first "
            "among the notes that reach memory_recall_inject_threshold. Notes crowd the "
            "context they are meant to inform, so the cap holds even when more qualify."
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

    memory_recall_deadline_seconds: float = Field(
        default=5.0,
        alias="AVA_MEMORY_RECALL_DEADLINE_SECONDS",
        description=(
            "Wall-clock budget (seconds) for one passive-recall pass — the search "
            "request, which includes the gateway's rerank of the retrieved chunks. "
            "Recall runs before the LLM on every inbound turn, so a congested gateway "
            "or a slow ranking model must not hold the turn hostage: when the deadline "
            "expires the turn proceeds with no recall this turn and the next turn "
            "retries. A fleet wake that queues searches behind the gateway's search "
            "endpoint exceeds it and degrades instead of stalling the first LLM call."
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

    memory_write_check_deadline_seconds: float = Field(
        default=5.0,
        alias="AVA_MEMORY_WRITE_CHECK_DEADLINE_SECONDS",
        description=(
            "Wall-clock budget (seconds) for checking the memory notes one execute_code "
            "call wrote: the rule check of every written note plus the duplicate search for "
            "the shared ones. The check runs after the call and only ever adds a reminder, "
            "so when the budget expires it is abandoned with a log line and the agent "
            "hears nothing about that call's notes."
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

    memory_write_dedup_threshold: float = Field(
        # Not calibrated: 0.8 is borrowed from memory_recall_inject_threshold. Every duplicate
        # probability is logged by the check, so the threshold can be calibrated against them.
        default=0.80,
        ge=0,
        le=1,
        alias="AVA_MEMORY_WRITE_DEDUP_THRESHOLD",
        description=(
            "Minimum probability (0..1, the decision model's answer to \"does an existing "
            'shared note record essentially the same knowledge as this new one") for the '
            "write check to point the agent at that note as a likely duplicate. Not "
            "calibrated by a benchmark: the value is a starting point, and every probability "
            "is logged so it can be calibrated later."
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
