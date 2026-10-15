# ava:ref base/host/env/docs/agent-slices.ava.okf.md#turn-snapshot
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
    llm_model: str


@dataclass(frozen=True)
class Prompt:
    system_prompt_extra: tuple[str, ...]
    skills_to_inject_into_system_prompt: tuple[str, ...]
    skills_to_expand_at_start: tuple[str, ...]
    agent_communication_style: str | None
    prompt_codeact_enabled: bool
    prompt_sequential_tool_calls_enabled: bool
    sdk_disable: tuple[str, ...]


@dataclass(frozen=True)
class MemoryRecall:
    passive_memory_recall_enabled: bool
    memory_recall_inject_threshold: float
    memory_recall_inject_max: int
    memory_recall_deadline_seconds: float
    memory_write_check_deadline_seconds: float
    memory_write_dedup_threshold: float
    memory_inherit_depth: int


@dataclass(frozen=True)
class SkillRecall:
    skill_recall_enabled: bool
    skill_recall_model: str
    skill_recall_threshold: float
    skill_recall_max: int
    skill_recall_deadline_seconds: float


@dataclass(frozen=True)
class HistoryDump:
    history_dump_enabled: bool
    history_dump_keep: int


@dataclass(frozen=True)
class SdkReminders:
    sdk_code_reminder_cadence: Cadence
    sdk_nameerror_hint_enabled: bool
    agent_reply_reminder_cadence: Cadence


@dataclass(frozen=True)
class LlmCallPolicy:
    llm_fatal_provider_error_types: str


@dataclass(frozen=True)
class Sandbox:
    eval_isolation: bool
    eval_network_allowlist: tuple[str, ...]
    syntax_fix_ruff_format: bool


@dataclass(frozen=True)
class ModelOverrides:
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

        pins = pins or {}
        return cls(**{f.name: pins.get(f.name) for f in fields(cls)})


@dataclass(frozen=True)
class AgentKernel:
    checkpoint_interval: int
    heartbeat_pause_max_seconds: float


def _value(pins: Mapping[str, Any], name: str, default_reader: Callable[[str, str], Any]) -> Any:

    value = pins[name] if name in pins else default_reader(FIELD_DOMAINS[name], name)
    return tuple(cast("list[Any]", value)) if isinstance(value, list) else value


def agent_setting(
    name: str,
    pins: Mapping[str, Any] | None = None,
    *,
    default_reader: Callable[[str, str], Any],
) -> Any:

    return _value(pins or {}, name, default_reader)


def _kwargs(
    slice_type: type, pins: Mapping[str, Any], default_reader: Callable[[str, str], Any]
) -> dict[str, Any]:
    return {f.name: _value(pins, f.name, default_reader) for f in fields(slice_type)}


class PluginConfigView:
    __slots__ = ("_cache", "_configs", "_overrides")

    def __init__(
        self, configs: Mapping[str, BaseModel], overrides: Mapping[str, Mapping[str, Any]]
    ) -> None:
        self._configs = configs
        self._overrides = {p: dict(fields) for p, fields in overrides.items()}
        self._cache: dict[str, BaseModel] = {}

    def flat(self) -> dict[str, Any]:

        return {key: value for fields in self._overrides.values() for key, value in fields.items()}

    def config_for(self, plugin: str) -> BaseModel:

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

        return {plugin: self.config_for(plugin) for plugin in self._configs}


@dataclass(frozen=True)
class AgentSlices:
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

        if field in self.pins:
            return self.pins[field]
        return self._default_reader(domain, field)

    def plugin_config(self, plugin: str) -> Any:

        return self._plugin_view.config_for(plugin)

    def plugin_configs(self) -> dict[str, Any]:

        return self._plugin_view.configs()

    def overlay(self) -> dict[str, Any]:

        return {**self.pins, **self._plugin_view.flat()}
