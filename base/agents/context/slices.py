"""`AgentSlices`: the per-agent configuration a turn reads, resolved once when the turn starts.

Many agents' turns share one host process, so an agent-scoped (`per_agent`) setting cannot live
in the process-global `settings`. The host resolves the agent's slices at the start of each turn
and hands them to the graph through `AvaContext.agent`; nodes and hooks read
`runtime.context.agent.<slice>.<field>` and pass the slice they need to the functions they call,
instead of reading a context-bound view (`base.config.turn_view`).

Each slice is a frozen dataclass whose field names are the flat setting names, grouped by the
package that reads them. `AgentSlices.resolve(pins)` fills every field with the agent's pin
(`config_overlay > birth_config`, merged by `resolve_agent_config_pins`) and falls through to the
live cluster default for an unpinned field, exactly as `turn_settings` does: a configuration
change reaches the agent's next turn, and a turn sees one value of each field throughout.

List-valued settings are frozen into tuples.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, cast

from base.host.env.config_lite_table import FIELD_DOMAINS


@dataclass(frozen=True)
class AgentBrain:
    """The model the agent runs."""

    llm_model: str


@dataclass(frozen=True)
class Prompt:
    """What shapes the system prompt and the opening notes."""

    system_prompt_extra: tuple[str, ...]
    skills_to_inject_into_system_prompt: tuple[str, ...]
    skills_to_expand_at_start: tuple[str, ...]
    agent_communication_style: str | None
    prompt_codeact_enabled: bool
    sdk_disable: tuple[str, ...]


@dataclass(frozen=True)
class MemoryRecall:
    """Passive memory recall, its filter pass, and memory inheritance."""

    passive_memory_recall_enabled: bool
    memory_recall_retrieve_k: int
    memory_recall_inject_k: int
    memory_recall_deadline_seconds: float
    memory_recall_filter_enabled: bool
    memory_recall_filter_model: str
    memory_recall_filter_max_retries: int
    memory_recall_filter_timeout_seconds: float
    memory_inherit_depth: int


@dataclass(frozen=True)
class HistoryDump:
    """The per-turn history dump into the agent's workspace."""

    history_dump_enabled: bool
    history_dump_keep: int


@dataclass(frozen=True)
class SdkReminders:
    """When the SDK reminders and hints fire."""

    sdk_code_reminder_cadence: str
    sdk_nameerror_hint_enabled: bool
    agent_reply_reminder_cadence: str


@dataclass(frozen=True)
class LlmCallPolicy:
    """How a model call's failures and provider caches are treated."""

    llm_fatal_provider_error_types: str
    gemini_explicit_cache_enabled: bool
    gemini_cache_timeout_seconds: float


@dataclass(frozen=True)
class Sandbox:
    """Isolation of the agent's code execution."""

    eval_isolation: bool
    eval_network_allowlist: tuple[str, ...]
    syntax_fix_ruff_format: bool


@dataclass(frozen=True)
class AgentKernel:
    """The turn loop's own pacing."""

    checkpoint_interval: int
    heartbeat_pause_max_seconds: float


def _value(pins: Mapping[str, Any], name: str) -> Any:
    """The pin for `name` when the agent has one, else the live cluster default."""
    if name in pins:
        value = pins[name]
    else:
        from base.config import settings

        value = getattr(getattr(settings, FIELD_DOMAINS[name]), name)
    return tuple(cast("list[Any]", value)) if isinstance(value, list) else value


def _kwargs(slice_type: type, pins: Mapping[str, Any]) -> dict[str, Any]:
    return {f.name: _value(pins, f.name) for f in fields(slice_type)}


@dataclass(frozen=True)
class AgentSlices:
    """One agent's resolved configuration for one turn."""

    brain: AgentBrain
    prompt: Prompt
    memory: MemoryRecall
    history_dump: HistoryDump
    sdk_reminders: SdkReminders
    llm_policy: LlmCallPolicy
    sandbox: Sandbox
    kernel: AgentKernel

    @classmethod
    def resolve(cls, pins: Mapping[str, Any] | None = None) -> AgentSlices:
        """The slices of an agent holding `pins` (`resolve_agent_config_pins`); no pins reads the
        cluster defaults as they are now."""
        pins = pins or {}
        return cls(
            brain=AgentBrain(**_kwargs(AgentBrain, pins)),
            prompt=Prompt(**_kwargs(Prompt, pins)),
            memory=MemoryRecall(**_kwargs(MemoryRecall, pins)),
            history_dump=HistoryDump(**_kwargs(HistoryDump, pins)),
            sdk_reminders=SdkReminders(**_kwargs(SdkReminders, pins)),
            llm_policy=LlmCallPolicy(**_kwargs(LlmCallPolicy, pins)),
            sandbox=Sandbox(**_kwargs(Sandbox, pins)),
            kernel=AgentKernel(**_kwargs(AgentKernel, pins)),
        )
