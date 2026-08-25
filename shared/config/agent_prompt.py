"""Agent system-prompt composition — AgentPromptSettings.

What goes INTO the system prompt: SDK/skills injection lists, the workspace
section, the /<name> command module, the communication style, and every
prompt-section toggle (SDK overview, knowledge cutoff, guided-workflow
sections, ...). Unset toggles resolve the per-model default via
shared/lm/registry.py. Split out of the former flat AgentSettings schema;
each field keeps its exact env alias so the .env surface is unchanged."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import NoDecode

from shared.config._base import EnvSettings

# Boolean spellings pydantic accepted for the retired AVA_SYSTEM_PROMPT_PROGRESS
# toggle, kept so an existing `.env` keeps meaning what it meant. See
# AgentPromptSettings._legacy_progress_bool_as_style. "off" is deliberately absent from
# the false set: it is now the enum's own 'off' member (omit the section
# entirely) rather than a boolean spelling, and that meaning is stronger than
# the 'silent' this alias used to produce for it.
_LEGACY_PROGRESS_TRUE = frozenset({"true", "1", "yes", "on", "t", "y"})
_LEGACY_PROGRESS_FALSE = frozenset({"false", "0", "no", "f", "n"})


class AgentPromptSettings(EnvSettings):
    sdk_disable: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="AVA_SDK_DISABLE",
        description=(
            "Comma-separated agent-facing SDK paths to remove before the agent "
            "imports `ava`. Each entry is a top-level module (`monitor`) or a dotted "
            "module.attr (`self.terminate`); the name then raises on import/access. "
            "Used to scope the SDK to a context, e.g. a benchmark runner that owns "
            "the agent lifecycle."
        ),
        json_schema_extra={
            "per_agent": True,
            "lifecycle": "frozen",
            "restart_required": "agent",
            "writable": False,
            "sensitive": False,
            "scope": "agent",
        },
    )

    sdk_expand_in_system_prompt: Annotated[list[str], NoDecode] = Field(
        # `*` expands every top-level public namespace, discovered from the live
        # `help(ava)` surface and sorted — so a newly added namespace is covered
        # without editing this default. A plugin namespace (e.g. ava_code's cwd)
        # still promotes its own paths via `ava.register_sdk_expand`, which
        # render ahead of this list (issue #1011).
        default_factory=lambda: ["*"],
        alias="AVA_SDK_EXPAND",
        description=(
            "Comma-separated SDK paths whose full contract (signatures + "
            "docstrings) is expanded into the system prompt after the SDK overview, "
            "so the agent calls them without a drill-down turn. Each entry is a "
            "dotted path under `ava`; `*` expands every top-level public namespace "
            "(combine with an explicit nested path, `*,shell.sessions`, to also "
            "expand a sub-namespace). `*` skips the capability surfaces `skills` "
            "and `mcps` — those are indexed once by the `# Capabilities` section, "
            "and expanding them here would render a second full index; name one "
            "explicitly (`*,skills`) to override. Names in AVA_SDK_DISABLE are "
            "excluded; unresolved entries are skipped with a warning. Empty "
            "expands nothing."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    skills_to_inject_into_system_prompt: Annotated[list[str], NoDecode] = Field(
        # `*` = the whole loaded catalog. An index line costs one line per skill,
        # and an agent cannot decide not to rebuild a capability it was never
        # told it has — so the default is completeness, and a shorter list is a
        # deliberate per-agent NARROWING, not the baseline.
        default_factory=lambda: ["*"],
        alias="AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT",
        description=(
            "Comma-separated skill names whose name + one-line description are "
            "injected into the system prompt as an always-on index; the agent loads "
            "the full body on demand. Each entry resolves by `.`-identifier "
            "(`ava-code.pr`) then bare frontmatter name, dash and underscore "
            "spellings alike; `*` (the default) injects every loaded skill. "
            "Unresolved names are skipped. Set an explicit list per agent to "
            "NARROW the index below the full catalog. Empty injects no "
            "index."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            # cluster-default + per_agent: a spawner narrows one worker's index
            # below the catalog (shared/plugin_config_registry.py reads the gate).
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "frozen",
        },
    )

    skills_to_expand_at_start: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="AVA_SKILLS_TO_EXPAND_AT_START",
        description=(
            "Comma-separated skill names whose FULL body is preloaded as a system "
            "note at session start and after each compact, so the agent has read "
            "them before its first turn. Stronger than "
            "skills_to_inject_into_system_prompt (name + one-line only) — use it for "
            "short disciplinary skills that must be active from spawn, and leave "
            "large reference skills in the index. `*` preloads every skill (a lot of "
            "tokens). Unresolved names are skipped. Default empty; per-agent "
            "overridable."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            # cluster-default + per_agent: mirrors skills_to_inject_into_system_prompt
            # so a spawner can arm one worker with a preloaded discipline skill.
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "frozen",
        },
    )

    system_prompt_extra: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="AVA_SYSTEM_PROMPT_EXTRA",
        description=(
            "Comma-separated extra system-prompt section keys to enable; each maps "
            "to a plugin-registered section that is off by default (e.g. "
            "`ava_code_workflow`). Empty (default) enables none."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "frozen",
        },
    )

    workspace_in_system_prompt: bool = Field(
        default=True,
        alias="AVA_WORKSPACE_IN_SYSTEM_PROMPT",
        description=(
            "Inject the `# Workspace` section pointing the agent at its per-agent "
            "workspace folder. Gates the prompt section only — the folder is still "
            "created and relative paths still resolve against it. Set False e.g. on "
            "a benchmark runner where the workspace pointer is noise."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    commands_enabled: bool = Field(
        default=True,
        alias="AVA_COMMANDS_ENABLED",
        description=(
            "Enable the `/<name>` command module: discovery + expansion of a "
            "leading `/<name>` chat inbound into the command's prompt. When False "
            "the module is inert and `/<name>` passes through as literal text."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_sdk_overview_enabled: bool = Field(
        default=True,
        alias="AVA_SYSTEM_PROMPT_SDK_OVERVIEW",
        validation_alias=AliasChoices("AVA_SYSTEM_PROMPT_SDK_OVERVIEW", "AVA_PROMPT_SDK_OVERVIEW"),
        description=(
            "Include the SDK overview (# ava + namespace index) in the system "
            "prompt. When False, only the bare identity paragraph is shown."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_knowledge_cutoff_enabled: bool = Field(
        default=True,
        alias="AVA_PROMPT_KNOWLEDGE_CUTOFF",
        description=(
            "Append a 'Knowledge cutoff: YYYY-MM' line to the system prompt. "
            "When False, the line is omitted — useful for benchmark runs where "
            "temporal metadata is noise."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_prefer_sdk_enabled: bool | None = Field(
        default=None,
        alias="AVA_SYSTEM_PROMPT_PREFER_SDK",
        validation_alias=AliasChoices("AVA_SYSTEM_PROMPT_PREFER_SDK", "AVA_PROMPT_PREFER_SDK"),
        description=(
            "Inject a one-line 'Prefer your SDK' section: use an `ava.*` tool over "
            "a plain-Python or raw-shell equivalent when one exists. Unset resolves "
            "the per-model default (shared floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_keep_it_simple_enabled: bool | None = Field(
        default=None,
        alias="AVA_SYSTEM_PROMPT_KEEP_IT_SIMPLE",
        validation_alias=AliasChoices(
            "AVA_SYSTEM_PROMPT_KEEP_IT_SIMPLE", "AVA_PROMPT_KEEP_IT_SIMPLE"
        ),
        description=(
            "Inject a 'Keep It Simple' section — prefer the mechanically correct, "
            "conceptually simple solution over clever shortcuts; relentless: favor "
            "the principle even when tedious. Unset resolves the per-model default "
            "(shared floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    agent_communication_style: Literal["oriented", "silent", "concise", "off"] | None = Field(
        default=None,
        alias="AVA_AGENT_COMMUNICATION_STYLE",
        validation_alias=AliasChoices(
            "AVA_AGENT_COMMUNICATION_STYLE",
            "AVA_SYSTEM_PROMPT_PROGRESS",
            "AVA_PROMPT_PROGRESS",
        ),
        description=(
            "How much the agent narrates while it works. 'oriented': brief "
            "interleaved updates. 'concise': speak only at real milestones. 'silent': "
            "one report at the end. 'off': the section is omitted entirely. Unset "
            "resolves the per-model default (shared/lm/registry.py; shared floor "
            "'off'). The retired AVA_SYSTEM_PROMPT_PROGRESS boolean still maps "
            "in (false -> 'silent', true -> 'oriented'); this var wins."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "frozen",
        },
    )

    prompt_memory_behavior_enabled: bool | None = Field(
        default=None,
        alias="AVA_SYSTEM_PROMPT_MEMORY",
        validation_alias=AliasChoices("AVA_SYSTEM_PROMPT_MEMORY", "AVA_PROMPT_MEMORY"),
        description=(
            "Inject a 'Remembering across sessions' section: when and what to "
            "persist/recall for the long-term memory pool. Auto-suppressed when the "
            "memory SDK is disabled. Unset resolves the per-model default (shared "
            "floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    agent_reflection_enabled: bool | None = Field(
        default=None,
        alias="AVA_AGENT_REFLECTION",
        description=(
            "Inject the 'Beyond the task at hand' section: prompt the agent to "
            "surface follow-ups and candidate next steps after finishing a task. "
            "Unset resolves the per-model default (shared floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_output_conciseness_enabled: bool | None = Field(
        default=None,
        alias="AVA_SYSTEM_PROMPT_CONCISENESS",
        validation_alias=AliasChoices("AVA_SYSTEM_PROMPT_CONCISENESS", "AVA_PROMPT_CONCISENESS"),
        description=(
            "Inject an 'Output shape' section: keep replies matched to the task, "
            "answer-first, and reference paths/lines instead of pasting output "
            "back. Unset resolves the per-model default (shared floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_ui_delivery_enabled: bool | None = Field(
        default=None,
        alias="AVA_SYSTEM_PROMPT_UI_DELIVERY",
        validation_alias=AliasChoices("AVA_SYSTEM_PROMPT_UI_DELIVERY", "AVA_PROMPT_UI_DELIVERY"),
        description=(
            "Inject a 'Deliver through the UI' section: content for the user goes "
            "through the UI, not as a bare path to a Markdown file. Files stay "
            "for persistence and handoff. Unset resolves the per-model default "
            "(shared floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_outcome_reporting_enabled: bool | None = Field(
        default=None,
        alias="AVA_SYSTEM_PROMPT_REPORTING",
        validation_alias=AliasChoices("AVA_SYSTEM_PROMPT_REPORTING", "AVA_PROMPT_REPORTING"),
        description=(
            "Inject a 'Reporting honestly' section: state outcomes as they are, "
            "don't round a partial result up to success. Unset resolves the "
            "per-model default (shared floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_action_caution_enabled: bool | None = Field(
        default=None,
        alias="AVA_SYSTEM_PROMPT_CAUTION",
        validation_alias=AliasChoices("AVA_SYSTEM_PROMPT_CAUTION", "AVA_PROMPT_CAUTION"),
        description=(
            "Inject a 'Before irreversible or outward-facing actions' section: "
            "confirm before hard-to-reverse or outward actions, and treat sending "
            "to an external service as publishing. Unset resolves the per-model "
            "default (shared floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_align_before_action_enabled: bool | None = Field(
        default=None,
        alias="AVA_SYSTEM_PROMPT_ALIGN",
        validation_alias=AliasChoices("AVA_SYSTEM_PROMPT_ALIGN", "AVA_PROMPT_ALIGN"),
        description=(
            "Inject an 'Aligning before you commit to a direction' section: before "
            "large/ambiguous/hard-to-redo work, confirm scope + approach with the "
            "user instead of running on assumptions. Unset resolves the per-model "
            "default (shared floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_delegation_check_enabled: bool | None = Field(
        default=None,
        alias="AVA_SYSTEM_PROMPT_DELEGATION_CHECK",
        validation_alias=AliasChoices(
            "AVA_SYSTEM_PROMPT_DELEGATION_CHECK", "AVA_PROMPT_DELEGATION_CHECK"
        ),
        description=(
            "Inject a 'Before you act \u2014 check' section: does a skill already "
            "cover this, is someone else already responsible, do they have better "
            "tools, would delegation cost more than the work, can it be "
            "parallelized? The skill-index step is dropped when this agent renders "
            "no Capabilities section. Unset resolves the per-model default (shared "
            "floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_cross_machine_delegation_enabled: bool | None = Field(
        default=None,
        alias="AVA_SYSTEM_PROMPT_CROSS_MACHINE_DELEGATION",
        validation_alias=AliasChoices(
            "AVA_SYSTEM_PROMPT_CROSS_MACHINE_DELEGATION",
            "AVA_PROMPT_CROSS_MACHINE_DELEGATION",
        ),
        description=(
            "Inject a one-sentence cross-machine delegation hint: when working "
            "across different machines, consider spawning an agent on the "
            "target machine so it can use that machine's resources directly. "
            "Unset resolves the per-model default (shared floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_file_driven_work_enabled: bool | None = Field(
        default=None,
        alias="AVA_SYSTEM_PROMPT_FILE_DRIVEN_WORK",
        validation_alias=AliasChoices(
            "AVA_SYSTEM_PROMPT_FILE_DRIVEN_WORK", "AVA_PROMPT_FILE_DRIVEN_WORK"
        ),
        description=(
            "Inject a 'File-driven workflow for complex tasks' section: write "
            "intermediate results to files instead of holding everything in context; "
            "use worktrees for isolation; hand off to peers via handoff files. "
            "Unset resolves the per-model default (shared floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    prompt_temporal_awareness_enabled: bool | None = Field(
        default=None,
        alias="AVA_SYSTEM_PROMPT_TEMPORAL",
        validation_alias=AliasChoices("AVA_SYSTEM_PROMPT_TEMPORAL", "AVA_PROMPT_TEMPORAL"),
        description=(
            "Inject a 'Temporal awareness' section: for facts that may have changed "
            "after the training cutoff (product/model/framework versions, recent "
            "releases), search before answering. Unset resolves the per-model "
            "default (shared floor: on)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    @field_validator(
        "sdk_disable",
        "sdk_expand_in_system_prompt",
        "skills_to_inject_into_system_prompt",
        "skills_to_expand_at_start",
        "system_prompt_extra",
        mode="before",
    )
    @classmethod
    def _split_comma_list(cls, v: object) -> object:
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        return v

    @field_validator("agent_communication_style", mode="before")
    @classmethod
    def _legacy_progress_bool_as_style(cls, v: object) -> object:
        """Read the retired AVA_SYSTEM_PROMPT_PROGRESS boolean as a style name.

        The field still accepts that alias, so a value coming from a `.env`
        written before the enum existed is a boolean literal, not a style: the
        old off-state (no narration) becomes 'silent', the old default becomes
        'oriented'. Only the boolean spellings pydantic itself accepted are
        translated — anything else falls through to Literal validation and
        fails fast rather than being guessed at. That includes the string
        'off': it is the enum's own 'off' member now (see
        agent_communication_style), not a boolean spelling, so it passes
        through unchanged and lands on the section-omitting member rather
        than the old 'silent' translation.
        """
        if isinstance(v, bool):
            return "oriented" if v else "silent"
        if isinstance(v, str):
            token = v.strip().lower()
            if token in _LEGACY_PROGRESS_TRUE:
                return "oriented"
            if token in _LEGACY_PROGRESS_FALSE:
                return "silent"
        return v
