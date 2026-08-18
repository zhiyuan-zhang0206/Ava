"""Agent compaction policy — AgentCompactionSettings.

Force-compact ceiling (fraction + absolute token cap), the soft wind-down reminder, and the reply-reminder cadence — the knobs that bound a growing context window. Split out of the former flat AgentSettings schema; each field keeps its exact env alias so the .env surface is unchanged."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from shared.config._base import EnvSettings


class AgentCompactionSettings(EnvSettings):
    auto_compact_fraction: float | None = Field(
        default=None,
        alias="AVA_AUTO_COMPACT_FRACTION",
        description=(
            "Force-compact ceiling as a fraction of the agent model's context "
            "window: when occupancy exceeds fraction * window, the history is "
            "replaced inline with a summary. Unset resolves the per-model default "
            "(shared/lm/registry.py; shared floor 0.4, which the whole roster runs); "
            "setting it pins one fraction for every model. Capped by "
            "auto_compact_ceiling_tokens. Range (0, 1]."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    auto_compact_ceiling_tokens: int | None = Field(
        default=None,
        alias="AVA_AUTO_COMPACT_CEILING_TOKENS",
        description=(
            "Absolute cap on the force-compact threshold, in tokens: the effective "
            "threshold is min(auto_compact_fraction * window, this). 0 disables the "
            "cap (pure fraction of window). Unset resolves the per-model default "
            "(shared/lm/registry.py; shared floor 0, and no registry entry opts in "
            "— the roster runs the pure fraction). Exists because "
            "advertised windows grew ~8x while measured effective context did not, "
            "so a fixed fraction means something different on a 200K model than on "
            "a 1M one; the reminder threshold is scaled down by the same factor so "
            "it keeps its proportional lead."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    compact_reminder_fraction: float | None = Field(
        default=None,
        alias="AVA_COMPACT_REMINDER_FRACTION",
        description=(
            "Soft wind-down reminder as a fraction of the context window: crossing "
            "it (while under auto_compact_fraction) fires a one-time note suggesting "
            "the agent self-compact before the forced ceiling. Unset resolves the "
            "per-model default (shared/lm/registry.py; shared floor 0.3). Keep it "
            "below auto_compact_fraction; set it >= to disable the reminder."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    agent_reply_reminder_cadence: Literal["once_per_compaction", "every_time"] = Field(
        default="once_per_compaction",
        alias="AVA_AGENT_REPLY_REMINDER_CADENCE",
        description=(
            "How often the agent-reply reminder fires — the note pointing at "
            "`ava.agents.send_message` when another agent's message arrives (a plain "
            "text reply is never delivered). 'once_per_compaction': at most once per "
            "context window (default). 'every_time': on every agent inbound."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    history_dump_enabled: bool = Field(
        default=False,
        alias="AVA_COMPACT_HISTORY_DUMP",
        description=(
            "Dump the full pre-compact conversation history (state.messages) to "
            "a JSONL file in the agent workspace (<workspace>/compact_dumps/"
            "<timestamp>.jsonl) whenever a compaction runs, and inject a system "
            "note in the fresh post-compact context pointing at the dump. Off by "
            "default: the dump is a forensics / trace-replay aid, not a retention "
            "mechanism (the summary remains the only memory that survives a "
            "compaction). Disk growth is bounded by history_dump_keep."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    history_dump_keep: int = Field(
        default=5,
        alias="AVA_COMPACT_HISTORY_DUMP_KEEP",
        description=(
            "How many pre-compact history dumps to keep per agent: after writing "
            "a new dump, older files in <workspace>/compact_dumps/ beyond this "
            "count are deleted. Each dump is a full conversation snapshot, so "
            "this bounds disk usage. Values below 1 are clamped to 1 (the new "
            "dump is always kept)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )
