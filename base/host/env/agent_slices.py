"""`AgentSlices`: the per-agent configuration a turn reads, resolved once when the turn starts.

Many agents' turns share one host process, so an agent-scoped (`per_agent`) setting cannot live
in the process-global `settings`. The host resolves the agent's slices at the start of each turn
and hands them to the graph through `AvaContext.agent`; nodes and hooks read
`runtime.context.agent.<slice>.<field>` and pass the slice they need to the functions they call,
instead of reading a context-bound view.

Each slice is a frozen dataclass whose field names are the flat setting names, grouped by the
package that reads them. `AgentSlices.resolve(pins)` fills every field with the agent's pin
(`config_overlay > birth_config`, merged by `resolve_agent_config_pins`) and falls through to the
live cluster default for an unpinned field: a configuration
change reaches the agent's next turn, and a turn sees one value of each field throughout.

List-valued settings are frozen into tuples.

This module sits beside the config index rather than in `base.agents.context` because importing it
pulls nothing heavy: the exec child and the SDK, which keep psycopg / redis out of their start,
build the slices too.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, Literal, cast

from pydantic import BaseModel

from base.host.env.config_lite_table import FIELD_DOMAINS

Cadence = Literal["once_per_compaction", "every_time"]


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

    sdk_code_reminder_cadence: Cadence
    sdk_nameerror_hint_enabled: bool
    agent_reply_reminder_cadence: Cadence


@dataclass(frozen=True)
class LlmCallPolicy:
    """How a model call's provider failures are treated."""

    llm_fatal_provider_error_types: str


@dataclass(frozen=True)
class Sandbox:
    """Isolation of the agent's code execution."""

    eval_isolation: bool
    eval_network_allowlist: tuple[str, ...]
    syntax_fix_ruff_format: bool


@dataclass(frozen=True)
class ModelOverrides:
    """The agent's explicit values for the per-model-defaultable settings (`None` = not set).

    A set value wins over the model's registry default (`base.lm.registry.resolve_setting`), so
    these are the inputs of that layering rather than final values: stream timeouts, reasoning
    effort, thinking budget and the compaction thresholds.
    """

    llm_stream_ttft_timeout_seconds: float | None
    llm_stream_total_timeout_seconds: float | None
    llm_stream_inter_chunk_timeout_seconds: float | None
    reasoning_effort: str | None
    claude_thinking_budget_tokens: int | None
    auto_compact_fraction: float | None
    auto_compact_ceiling_tokens: int | None
    compact_reminder_fraction: float | None

    @classmethod
    def from_pins(cls, pins: Mapping[str, Any] | None) -> ModelOverrides:
        """Only what `pins` holds, with no read of the live cluster default: for a process that
        displays an agent's values and does not hold the agent domain of the config (the gateway)."""
        pins = pins or {}
        return cls(**{f.name: pins.get(f.name) for f in fields(cls)})


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


def agent_setting(name: str, pins: Mapping[str, Any] | None = None) -> Any:
    """One per-agent setting: the pin when `pins` holds one, else the live default.

    For a process that needs a handful of fields and must not read the rest (the exec child boots
    on the lite config index, and reading a field outside it upgrades the whole config).
    """
    return _value(pins or {}, name)


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
    overrides: ModelOverrides
    sandbox: Sandbox
    kernel: AgentKernel
    # The agent's framework pins, for the settings no slice names (`read`), and its plugin pins
    # (`plugin -> {field: value}`), for `plugin_config`.
    pins: Mapping[str, Any]
    plugin_pins: Mapping[str, Mapping[str, Any]]
    _plugin_view: Any = field(repr=False, compare=False)

    @classmethod
    def resolve(
        cls,
        pins: Mapping[str, Any] | None = None,
        plugin_pins: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        plugin_configs: Mapping[str, BaseModel] | None = None,
    ) -> AgentSlices:
        """The slices of an agent holding `pins` (`resolve_agent_config_pins`) and `plugin_pins`
        (`resolve_agent_plugin_pins`); no pins reads the cluster defaults as they are now."""
        from base.packages.plugins.config_view import PluginConfigView

        pins = pins or {}
        plugin_pins = plugin_pins or {}
        return cls(
            brain=AgentBrain(**_kwargs(AgentBrain, pins)),
            prompt=Prompt(**_kwargs(Prompt, pins)),
            memory=MemoryRecall(**_kwargs(MemoryRecall, pins)),
            history_dump=HistoryDump(**_kwargs(HistoryDump, pins)),
            sdk_reminders=SdkReminders(**_kwargs(SdkReminders, pins)),
            llm_policy=LlmCallPolicy(**_kwargs(LlmCallPolicy, pins)),
            overrides=ModelOverrides(**_kwargs(ModelOverrides, pins)),
            sandbox=Sandbox(**_kwargs(Sandbox, pins)),
            kernel=AgentKernel(**_kwargs(AgentKernel, pins)),
            pins=MappingProxyType(dict(pins)),
            plugin_pins=MappingProxyType({p: dict(f) for p, f in plugin_pins.items()}),
            _plugin_view=PluginConfigView(plugin_configs or {}, plugin_pins),
        )

    def read(self, domain: str, field: str) -> Any:
        """The raw value of any core setting for this agent: its pin, else the live default."""
        if field in self.pins:
            return self.pins[field]
        from base.config import settings

        return getattr(getattr(settings, domain), field)

    def plugin_config(self, plugin: str) -> Any:
        """This agent's config: the supplied boot image with its own plugin pins."""
        return self._plugin_view.config_for(plugin)

    def plugin_configs(self) -> dict[str, Any]:
        """`plugin_config` of every registered plugin."""
        return self._plugin_view.configs()

    def overlay(self) -> dict[str, Any]:
        """The agent's pins as the flat overlay an exec child boots with (framework and plugin)."""
        return {**self.pins, **self._plugin_view.flat()}
