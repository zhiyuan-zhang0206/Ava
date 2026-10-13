"""`AgentSlices`: the per-agent configuration a turn reads, resolved once when the turn starts.

Many agents' turns share one host process, so an agent-scoped (`per_agent`) setting cannot live
in the process-global `settings`. The host resolves the agent's slices at the start of each turn
and hands them to the graph through `AvaContext.agent`; nodes and hooks read
`runtime.context.agent.<slice>.<field>` and pass the slice they need to the functions they call,
instead of reading a context-bound view.

Each slice is a frozen dataclass whose field names are the flat setting names, grouped by the
package that reads them. `AgentSlices.resolve(pins, default_reader=...)` fills every field with the agent's pin
(`config_overlay > birth_config`, merged by `resolve_agent_config_pins`) and falls through to the
live cluster default for an unpinned field: a configuration
change reaches the agent's next turn, and a turn sees one value of each field throughout.

List-valued settings are frozen into tuples.

This module sits beside the config index rather than in `base.agents.context` because importing it
pulls nothing heavy: the exec child and the SDK, which keep psycopg / redis out of their start,
build the slices too.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
    prompt_sequential_tool_calls_enabled: bool
    sdk_disable: tuple[str, ...]


@dataclass(frozen=True)
class MemoryRecall:
    """Passive memory recall, its injection threshold, memory write checks, and memory inheritance."""

    passive_memory_recall_enabled: bool
    memory_recall_inject_threshold: float
    memory_recall_inject_max: int
    memory_recall_deadline_seconds: float
    memory_write_check_deadline_seconds: float
    memory_write_dedup_threshold: float
    memory_inherit_depth: int


@dataclass(frozen=True)
class SkillRecall:
    """Skill recall: whether it runs, the decision model that scores the skill index, and how
    its scores become an offer."""

    skill_recall_enabled: bool
    skill_recall_model: str
    skill_recall_threshold: float
    skill_recall_max: int
    skill_recall_deadline_seconds: float


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


def _value(pins: Mapping[str, Any], name: str, default_reader: Callable[[str, str], Any]) -> Any:
    """The pin for `name` when the agent has one, else the live cluster default."""
    value = pins[name] if name in pins else default_reader(FIELD_DOMAINS[name], name)
    return tuple(cast("list[Any]", value)) if isinstance(value, list) else value


def agent_setting(
    name: str,
    pins: Mapping[str, Any] | None = None,
    *,
    default_reader: Callable[[str, str], Any],
) -> Any:
    """One per-agent setting: the pin when `pins` holds one, else the live default.

    For a process that needs a handful of fields and must not read the rest (the exec child boots
    on the lite config index, and reading a field outside it upgrades the whole config).
    """
    return _value(pins or {}, name, default_reader)


def _kwargs(
    slice_type: type, pins: Mapping[str, Any], default_reader: Callable[[str, str], Any]
) -> dict[str, Any]:
    return {f.name: _value(pins, f.name, default_reader) for f in fields(slice_type)}


class PluginConfigView:
    """One agent's plugin overrides over an explicitly supplied boot config image.

    The host receives the installer's resolved image and creates one view per
    agent turn, carrying it on ``AgentSlices``. An exec child applies its overlay
    at boot; an external attachment owns its agent's view. Overrides affect only
    that view. Frozen plugin instances are memoized per view, never globally.
    Plugin fields have no birth_config: an explicit overlay pins them; otherwise
    another process start reads the current authority image. See
    ``base/packages/plugins/docs/plugin-config.ava.okf.md`` for the shared lifecycle contract.

    The cache is per view (the host resolves one view per agent turn, on its `AgentSlices`).
    Concurrent first reads may build the same instance twice; both are equal frozen models and the last write wins,
    which is why this needs no lock.
    """

    __slots__ = ("_cache", "_configs", "_overrides")

    def __init__(
        self, configs: Mapping[str, BaseModel], overrides: Mapping[str, Mapping[str, Any]]
    ) -> None:
        self._configs = configs
        self._overrides = {p: dict(fields) for p, fields in overrides.items()}
        self._cache: dict[str, BaseModel] = {}

    def flat(self) -> dict[str, Any]:
        """The overrides as the flat execution-child overlay."""
        return {key: value for fields in self._overrides.values() for key, value in fields.items()}

    def config_for(self, plugin: str) -> BaseModel:
        """This agent's instance, or its supplied base when no override is set."""
        base = self._configs[plugin]
        updates = self._overrides.get(plugin)
        if not updates:
            return base
        cached = self._cache.get(plugin)
        if cached is not None and type(cached) is type(base):
            return cached
        built = type(base)(**{**base.model_dump(), **updates})
        self._cache[plugin] = built
        return built

    def configs(self) -> dict[str, BaseModel]:
        """The bound plugin configs with this agent's overrides applied."""
        return {plugin: self.config_for(plugin) for plugin in self._configs}


@dataclass(frozen=True)
class AgentSlices:
    """One agent's resolved configuration for one turn."""

    brain: AgentBrain
    prompt: Prompt
    memory: MemoryRecall
    skill_recall: SkillRecall
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
    _default_reader: Callable[[str, str], Any] = field(repr=False, compare=False)

    @classmethod
    def resolve(
        cls,
        pins: Mapping[str, Any] | None = None,
        plugin_pins: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        default_reader: Callable[[str, str], Any],
        plugin_configs: Mapping[str, BaseModel] | None = None,
        brain: AgentBrain | None = None,
    ) -> AgentSlices:
        """The slices of an agent holding `pins` (`resolve_agent_config_pins`) and `plugin_pins`
        (`resolve_agent_plugin_pins`); no pins reads the cluster defaults as they are now.
        A composition owner may supply a pre-resolved `brain`; other slices still use
        their existing live defaults and pins. The root supplies the live reader;
        resolving slices freezes their values, while read() remains live for unpinned fields."""
        pins = pins or {}
        plugin_pins = plugin_pins or {}
        return cls(
            brain=brain
            if brain is not None
            else AgentBrain(**_kwargs(AgentBrain, pins, default_reader)),
            prompt=Prompt(**_kwargs(Prompt, pins, default_reader)),
            memory=MemoryRecall(**_kwargs(MemoryRecall, pins, default_reader)),
            skill_recall=SkillRecall(**_kwargs(SkillRecall, pins, default_reader)),
            history_dump=HistoryDump(**_kwargs(HistoryDump, pins, default_reader)),
            sdk_reminders=SdkReminders(**_kwargs(SdkReminders, pins, default_reader)),
            llm_policy=LlmCallPolicy(**_kwargs(LlmCallPolicy, pins, default_reader)),
            overrides=ModelOverrides(**_kwargs(ModelOverrides, pins, default_reader)),
            sandbox=Sandbox(**_kwargs(Sandbox, pins, default_reader)),
            kernel=AgentKernel(**_kwargs(AgentKernel, pins, default_reader)),
            pins=MappingProxyType(dict(pins)),
            plugin_pins=MappingProxyType({p: dict(f) for p, f in plugin_pins.items()}),
            _plugin_view=PluginConfigView(plugin_configs or {}, plugin_pins),
            _default_reader=default_reader,
        )

    def read(self, domain: str, field: str) -> Any:
        """The raw value of any core setting for this agent: its pin, else the live default."""
        if field in self.pins:
            return self.pins[field]
        return self._default_reader(domain, field)

    def plugin_config(self, plugin: str) -> Any:
        """This agent's config: the supplied boot image with its own plugin pins."""
        return self._plugin_view.config_for(plugin)

    def plugin_configs(self) -> dict[str, Any]:
        """`plugin_config` of every registered plugin."""
        return self._plugin_view.configs()

    def overlay(self) -> dict[str, Any]:
        """The agent's pins as the flat overlay an exec child boots with (framework and plugin)."""
        return {**self.pins, **self._plugin_view.flat()}
