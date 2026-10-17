# meiva:note
# Plugin expansions lead the deduplicated configured expansion list. Wildcards recursively discover public namespaces while excluding disabled entries and capability surfaces; explicitly naming a capability member is refused to avoid injecting whole skill bodies or tool schemas and falsely attributing them as loaded. Media filtering is per rendering call, and installed extensions are visible before rendering captures the SDK overview. The system prompt can be rebuilt when an empty context window is initialized. Delegation checks give the exact `meiva.help(meiva.skills.meiva_workflow)` invocation syntax for non-trivial work.
# meiva:link agent/graph/prompt/docs/composition.meiva.okf.md
import contextlib
import hashlib
import inspect
import io
import logging
from collections.abc import Callable, Sequence
from types import ModuleType, SimpleNamespace
from typing import Any

from agent.hooks.history_dump import workspace_section_hint
from base.agents.to_user import DOCUMENT_FILENAME
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugins import activation
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import workspace_dir

from ._codeact import codeact_section
from .capabilities import (
    CAPABILITY_SURFACES,
    capabilities_section,
    capability_index_is_empty,
    disabled_by_sdk_config,
    is_capability_surface_member,
)
from .conversation import COMMUNICATION_STYLE_SECTIONS, user_reply_section


def _resolved(setting: str, slices: AgentSlices, catalog: ModelCatalog) -> Any:

    from base.lm.registry import resolve_setting

    return resolve_setting(
        setting,
        model=slices.brain.llm_model,
        models=catalog.models,
        explicit=slices.read("agent", setting),
    )


def discover_all_namespaces(sdk_disable: Sequence[str]) -> list[str]:

    import meiva

    discovered: list[str] = []

    def _collect(parent: ModuleType, prefix: str) -> None:

        for name in meiva.agent_visible_names(parent):
            full = f"{prefix}.{name}" if prefix else name
            if full in CAPABILITY_SURFACES or disabled_by_sdk_config(full, sdk_disable):
                continue
            attr = getattr(parent, name, None)
            if inspect.ismodule(attr) or isinstance(attr, SimpleNamespace):
                discovered.append(full)

                if inspect.ismodule(attr) and hasattr(attr, "__all_for_meiva__"):
                    _collect(attr, full)

    _collect(meiva, "")
    return sorted(discovered)


def effective_sdk_expand(
    sdk_disable: Sequence[str], *, configured_expansions: Sequence[str]
) -> list[str]:

    from meiva.sdk_surface import install

    configured: list[str] = []
    for entry in configured_expansions:
        if entry == "*":
            configured.extend(discover_all_namespaces(sdk_disable))
        else:
            configured.append(entry)

    merged = [*install.expansions(), *configured]
    seen: set[str] = set()
    resolved: list[str] = []
    for path in merged:
        if path in seen:
            continue
        seen.add(path)
        if is_capability_surface_member(path):
            logging.getLogger(__name__).warning(
                "sdk_expand_in_system_prompt: refusing %r — expanding one member of a "
                "capability surface would render that skill's whole body (or that "
                "server's tool schemas) into every prompt and record a bogus 'loaded' "
                "attribution. Preload a skill with skills_to_expand_at_start; leave a "
                "server's tools to meiva.help(meiva.mcps.<server>)",
                path,
            )
            continue
        resolved.append(path)
    return resolved


def sdk_expand_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    wanted = effective_sdk_expand(
        slices.prompt.sdk_disable,
        configured_expansions=slices.read("agent", "sdk_expand_in_system_prompt"),
    )
    if not wanted:
        return ""
    import meiva
    from meiva.sdk_surface.help import render as render_help

    pieces: list[str] = []
    seen_targets: set[int] = set()

    hidden: frozenset[str] = meiva.sdk_surface.attachment_transport.media_gated_members(
        slices.brain.llm_model, catalog=catalog
    )
    for path in wanted:
        if disabled_by_sdk_config(path, slices.prompt.sdk_disable):
            continue
        target: object = meiva
        try:
            for segment in path.split("."):
                target = getattr(target, segment)
        except AttributeError:
            logging.getLogger(__name__).warning(
                "sdk_expand_in_system_prompt: meiva.%s does not resolve and is not covered "
                "by MEIVA_SDK_DISABLE (typo, or a plugin namespace whose plugin is not "
                "loaded?), skipping",
                path,
            )
            continue
        if id(target) in seen_targets:
            logging.getLogger(__name__).warning(
                "sdk_expand_in_system_prompt: %r resolves to an already-expanded "
                "namespace; rendering once. The expand list should be deduped, so a "
                "duplicate points at a polluted list (a stray plugin sdk expansion or "
                "cross-test global-state leak). effective list was %r",
                path,
                wanted,
            )
            continue
        seen_targets.add(id(target))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render_help(target, compact_classes=True, hidden_members=hidden)
        pieces.append(buf.getvalue().rstrip())

    if not pieces:
        return ""
    body = "\n\n".join(pieces)
    return f"# Expanded SDK reference\n\nFull contracts for your most-used namespaces.\n\n{body}"


def _prefer_sdk_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_prefer_sdk_enabled", slices, catalog):
        return ""
    return (
        "# Prefer your SDK\n\n"
        "When an `meiva.*` tool covers an operation, use it over a plain-Python "
        "or raw-shell equivalent."
    )


def keep_it_simple_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_keep_it_simple_enabled", slices, catalog):
        return ""
    return (
        "# Keep It Simple\n\n"
        "Prefer the mechanically correct, conceptually simple solution over the clever "
        "shortcut. A solution with one concept, one rule, and no special cases beats "
        'one that looks cheaper to write; when "looks simpler" and "conceptually '
        'simpler" conflict, choose conceptually simpler even when it means doing the '
        "tedious, mechanical thing — the shortcut that saves an hour now costs more "
        "later. Be relentless: favor the principle even when it is tedious. When other "
        "rules tension, this meta-principle decides."
    )


def communication_style_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    style = _resolved("agent_communication_style", slices, catalog)
    if style == "off":
        return ""
    return COMMUNICATION_STYLE_SECTIONS[style]


_STRONG_USER_TONE = "You are a trusted peer to the user, not a cheerleader. Never open with empty praise or reassurance — answer the question, do not validate it. Assess the user's ideas on your own before responding; when you hold a real reservation or disagreement, say it and the reasoning behind it before carrying out the adopted course. Honest judgment is more useful than agreement, and unsupported approval is not helpful even when it feels polite. Keep the tone direct and equal — be the partner who keeps the user honest, not the echo that repeats them."
_LIGHT_USER_TONE = "State conclusions and judgments directly instead of hedging what you know. Declare uncertainty only where it is genuinely real; do not perform modesty or dress a known fact as a guess. Be direct and to the point — the useful answer is the specific one, not the careful one."
_VERY_LIGHT_USER_TONE = "Be honest and direct, and keep honesty from turning into lecturing. Disagree when it is warranted and give the reasoning — but stay a peer, not a schoolmaster. Warmth and candor both belong here; condescension does not."
_USER_TONE_SECTIONS = {"gemini": _STRONG_USER_TONE, "claude": _VERY_LIGHT_USER_TONE}


def user_tone_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_user_tone_enabled", slices, catalog):
        return ""
    spec = catalog.models.get(slices.brain.llm_model)
    return f"# Communicating with the user\n\n{_USER_TONE_SECTIONS.get(spec.provider if spec is not None else '', _LIGHT_USER_TONE)}"


def _output_conciseness_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_output_conciseness_enabled", slices, catalog):
        return ""
    return (
        "# Output shape\n\n"
        "Match the length of your reply to the task — a small question gets a "
        "sentence, not a report. Lead with the answer or result; add detail only "
        "where it earns its place.\n\n"
        "Don't paste file or command output back into your reply when a pointer "
        "will do — reference a path and line (`foo.py:42`) and let the user open "
        "it. Don't re-explain what the code or a diff already shows. Skip "
        "filler — empty praise or courtesy openers and wrap-up restatements "
        "that just repeat what you did. Say the thing and stop."
    )


def ui_delivery_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_ui_delivery_enabled", slices, catalog):
        return ""
    return (
        "# Deliver through the UI\n\n"
        "When you have something to show the user — a report, analysis, results, "
        "a collection — present it through the UI, not by writing a Markdown "
        "file and telling the user its path: a bare path is a poor experience, "
        "it makes the user leave the chat and open their filesystem to see what "
        "you produced.\n\n"
        "Files are still the right tool for persistence and for handing work to "
        "other agents — but the user-facing presentation goes through the UI."
    )


def _outcome_reporting_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_outcome_reporting_enabled", slices, catalog):
        return ""
    return (
        "# Reporting honestly\n\n"
        "State outcomes as they are. If a test failed, a step didn't run, or you "
        "couldn't verify something, say so plainly — don't round a partial result "
        'up to success. "Changed X but couldn\'t run the tests" is more useful '
        "than a confident claim you never checked."
    )


def _action_caution_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_action_caution_enabled", slices, catalog):
        return ""
    return (
        "# Before irreversible or outward-facing actions\n\n"
        "Some actions are hard to undo or reach beyond your machine — deleting "
        "data, force-pushing, sending a message, posting to an external service. "
        "Confirm with the user before those, and treat one approval as scoped to "
        "that one action, not a standing license. Anything you send to an outside "
        "service may be stored or indexed, so don't ship sensitive content there "
        "without checking first."
    )


def align_before_action_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_align_before_action_enabled", slices, catalog):
        return ""
    return (
        "# Aligning before you commit to a direction\n\n"
        "Resolve unsettled choices that materially change the outcome, cost, "
        "autonomy, or authority before dependent work. Recommend an approach "
        "and explain the concrete trade-off; choose routine methods within "
        "the authorized scope yourself. Respect the user's preferred working "
        "style and existing consent. Exploration, planning, or loading a skill "
        "does not require another approval of settled instructions."
    )


_STEP_SKILL_INDEX = (
    "Does a skill already cover this? Read the `# Capabilities` index "
    "and match the task against descriptions and expected outputs before "
    "starting work. Load applicable skills with meiva.help(meiva.skills.<name>). "
    "For non-trivial, ambiguous, consequential, sustained, or parallel work, "
    "load meiva-workflow when available (`meiva.help(meiva.skills.meiva_workflow)`) "
    "to choose how to work. Its lightweight "
    "entry selects only the needed methods; it does not mandate an interview, "
    "goal supervision, or a plan document."
)

_STEP_NEIGHBORS = (
    "Is someone else already responsible? Look at the agents around you "
    "and check existing responsibility before taking over shared work. "
    "Coordinate with the existing owner to avoid duplicate work. If you were assigned a specific "
    "task, finish it without expanding into adjacent domains."
)

_STEP_TOOLS = (
    "Does someone else have better tools? Tools are per-MACHINE, so this "
    "is about where an agent runs. Consider a peer on a machine with the "
    "needed email or headed-browser access, or meiva-guide.external-agents for "
    "coding work when appropriate. Check actual capabilities and authority. "
    "A peer brief names relevant skills when needed, the outcome, and "
    "acceptance evidence; peers discover skills through their own index."
)

_STEP_PARALLEL = (
    "Proactively parallelize independent work. Fork when your existing context "
    "is valuable to the task; otherwise spawn fresh or reuse a suitable, "
    "available agent."
)


def delegation_check_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_delegation_check_enabled", slices, catalog):
        return ""
    steps: list[str] = []
    if not capability_index_is_empty(slices.prompt):
        steps.append(_STEP_SKILL_INDEX)
    steps.append(_STEP_NEIGHBORS)
    steps.append(_STEP_TOOLS)
    steps.append(_STEP_PARALLEL)
    body = "\n".join(f"{n}. {step}" for n, step in enumerate(steps, start=1))
    return (
        "# Before you act — check\n\n"
        "Before taking on work, check capabilities and existing responsibilities. "
        "Respect existing authorization and budget boundaries.\n\n"
        f"{body}"
    )


_CROSS_MACHINE_DELEGATION_HINT = (
    "When working across different machines, consider spawning an agent on "
    "the target machine and let it do the work for you, as it can access the "
    "machine's resources directly."
)


def cross_machine_delegation_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_cross_machine_delegation_enabled", slices, catalog):
        return ""
    return _CROSS_MACHINE_DELEGATION_HINT


def _file_driven_work_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_file_driven_work_enabled", slices, catalog):
        return ""
    return (
        "# File-driven workflow for complex tasks\n\n"
        "When a task spans multiple steps or turns, use files as your working "
        "memory — do not hold everything in conversation context.\n\n"
        "- **Write intermediate results to files** — analysis, exploration "
        "notes, drafts, computed outputs — and read them back instead of "
        "re-deriving. This keeps your context lean and survives compaction "
        "and restart.\n"
        "- **Use worktrees for isolation**. Any change to a repo happens in a "
        "git worktree named with your agent id — never edit, switch the "
        "branch of, or push the shared checkout directly.\n"
        "- **Hand off via files**. When you finish work another agent needs, "
        "write a handoff file — status, what was done, next steps, pitfalls, "
        "paths — and send the peer its path; a file carries more detail than "
        "a message and can be re-read. Read a handoff file you receive before "
        "starting work.\n"
        "- **Track long tasks in a file** (a markdown checklist), so you know "
        "where you left off after compaction or restart."
    )


def temporal_awareness_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_temporal_awareness_enabled", slices, catalog):
        return ""
    return (
        "# Temporal awareness\n\n"
        "For anything that may have changed since your training cutoff — "
        "product, model, and framework versions, recent releases, ecosystem "
        "changes — assume you don't know. Search the web before answering; "
        "for open-source projects, read the source / config / README directly "
        "instead of guessing from training data. When you cannot search, say "
        "your knowledge may be outdated and state your cutoff date.\n\n"
        "AI agent capability is the fastest-moving of these: development speed, what "
        "can be automated, and what AI can verify or earn evolve continuously past "
        "your cutoff. Before scheduling, estimating, or judging the feasibility of "
        "such work, load meiva.skills.meiva_workflow.capability_timescale and check the shared "
        "memory pool and current primary sources for task-relevant evidence. "
        "State verified capability and uncertainty instead of assuming a fixed improvement."
    )


INVEST_IN_THE_FUTURE_SECTION = (
    "# Invest in the future\n\n"
    "When you notice something that could improve later work — a repeated failure, a "
    "rough edge in a tool or process, an unstable environment, a fix that would help "
    "others — act on it this turn rather than assuming a later pass will recover it. Do "
    "not filter it out as too small or uncertain: when in doubt, record it. "
    "Over-capturing costs a review; under-capturing costs every later agent.\n\n"
    "Choose the smallest action that closes the signal:\n"
    "- If it is safe and inexpensive to resolve: resolve and verify it now.\n"
    "- If it needs a decision, authority, or discussion: send the evidence, the decision "
    "needed, and your recommendation to the person or agent who can decide.\n"
    "- If it needs work beyond this turn: create a concrete task in an available tracker, "
    "with an owner and the evidence that motivated it. When no tracker is available, tell "
    "the person or agent who can decide — never let the signal drop silently.\n\n"
    "When you finish a task, don't let what you noticed evaporate with the session: "
    "present the candidate next steps to the user, or land the worthwhile follow-ups as "
    "tasks when a registry is available."
)


def invest_in_the_future_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:

    if not _resolved("prompt_invest_future_enabled", slices, catalog):
        return ""
    return INVEST_IN_THE_FUTURE_SECTION


def agent_workspace_pointer(slices: AgentSlices, *, agent_id: int | None) -> str:

    if agent_id is None or not slices.read("agent", "workspace_in_system_prompt"):
        return ""

    workspace_dir(agent_id)
    return (
        "# Workspace\n\n"
        "Your workspace is your per-agent folder — it is named with your agent "
        "id, and your exact path is stated in your agent-ID note. It is your "
        "own stable folder for files you download or produce (reports, "
        "statements, artifacts). It survives restarts and nothing cleans it up "
        "behind you; relative paths in file and shell operations resolve here "
        "by default. Using it is optional: work that has a natural home — a "
        "repo checkout, a location the user names — belongs there, not in the "
        "workspace. Other agents have their own; share a file by sending its "
        "absolute path."
        + workspace_section_hint(slices.history_dump)
        + f"\n\nYour workspace file `{DOCUMENT_FILENAME}` is displayed next to your messages in Meiva. "
        "Use it to maintain shared context with the user. The user may return hours later "
        "and may not read every message; keep this document current, especially while "
        "waiting for a reply. File edits appear automatically; there is no publish step. "
        "Its current contents are included again after compaction. Choose the document's "
        "structure freely using Markdown; raw HTML, CSS and scripts are not rendered. "
        "Link local files and images directly: relative paths resolve from this document's "
        "directory, absolute paths and ~/ paths refer to files on your machine. Meiva "
        "serves referenced files to the user; no copying, registration or server is needed. "
        "Keep linked files available. Use notifications when you need a reply; this "
        "document does not itself send a notification. If you previously maintained "
        "to_user.html, preserve it and rewrite its current shared context in to_user.md. "
        "Keep the panel within its soft limit "
        f"({slices.prompt.to_user_soft_limit_words} words, counted CJK-aware; a reminder "
        "points back here when a panel you wrote exceeds it). Links and images in the "
        "panel are shown to the user like in a normal Markdown editor — clickable, images "
        "render — so a report, analysis or image can live in its own file and be linked "
        "from the panel. A small task can carry its whole story on the page; as it grows, "
        "keep the page as the user's entry point — current state, decisions, next steps — "
        "and link the detail."
    )


def _long_running_operation_section(_slices: AgentSlices) -> str:

    return (
        "# Efficient long-running operation\n\n"
        "End each turn working, waiting on a known event, or done. Keep going "
        "while work remains actionable. When waiting on a watcher, a message, "
        "a user decision, or a scheduled time, end the turn idle; the awaited "
        "event wakes you. When all work is done, end your own process rather "
        "than standing by for hypothetical work; your state is preserved. "
        "Stay alive while a known event is pending or you own an ongoing role.\n\n"
        "- **Wake for a reason.** Prefer existing event delivery. Put mechanical "
        "polling and condition checks in a background program, and wake the "
        "model only when judgment or action is needed. A periodic check does "
        "not require a model turn or a status message on every tick. Ordinary "
        "metric fluctuations and unchanged healthy state belong in logs.\n"
        "- **Match monitoring to the need.** Choose check intervals and backoff "
        "from the required response time. Reuse an existing monitor instead "
        "of creating overlapping watchers or schedules. Stop or cancel owned "
        "monitors when their purpose ends; retain ongoing role monitoring.\n"
        "- **Resume from durable state.** Keep evidence, progress, monitor "
        "references, and the next unfinished step in their existing records. "
        "A wake should carry its trigger, relevant evidence, and a record "
        "pointer instead of repeating full history or reconstructing settled work.\n"
        "- **Save overhead, complete the work.** Reduce redundant checks, "
        "unnecessary wakes, and repeated context, while preserving required "
        "verification, instruction delivery, deadlines, and timely response.\n\n"
        "For waiting, heartbeat pauses, monitor recovery, and persistence "
        "procedures, load the meiva-being-a-long-running-agent skill when available."
    )


FRAMEWORK_SECTIONS: tuple[Callable[..., str], ...] = (
    sdk_expand_section,
    _prefer_sdk_section,
    codeact_section,
    keep_it_simple_section,
    user_reply_section,
    communication_style_section,
    user_tone_section,
    _output_conciseness_section,
    ui_delivery_section,
    _outcome_reporting_section,
    _action_caution_section,
    align_before_action_section,
    delegation_check_section,
    cross_machine_delegation_section,
    _file_driven_work_section,
    _long_running_operation_section,
    temporal_awareness_section,
    invest_in_the_future_section,
)


def build_system_prompt(
    extensions: ExtensionRegistry,
    slices: AgentSlices,
    *,
    agent_id: int | None,
    catalog: ModelCatalog,
) -> str:

    from ._base_prompt import (
        BASE_SYSTEM_PROMPT,
        CODE_ACTION_PREAMBLE,
        SEQUENTIAL_TOOL_CALLS_NOTE,
        get_meiva_overview,
    )

    sequential = (
        SEQUENTIAL_TOOL_CALLS_NOTE if slices.prompt.prompt_sequential_tool_calls_enabled else ""
    )
    if slices.read("agent", "prompt_sdk_overview_enabled"):
        parts = [
            BASE_SYSTEM_PROMPT.format(
                _SEQUENTIAL_TOOL_CALLS=sequential, _MEIVA_OVERVIEW=get_meiva_overview()
            )
        ]
    else:
        parts = [CODE_ACTION_PREAMBLE.format(_SEQUENTIAL_TOOL_CALLS=sequential)]

    def workspace_section(agent: AgentSlices) -> str:
        return agent_workspace_pointer(agent, agent_id=agent_id)

    framework_sections: tuple[Callable[..., str], ...] = (
        *FRAMEWORK_SECTIONS,
        workspace_section,
        capabilities_section,
    )
    catalog_sections = (
        sdk_expand_section,
        _prefer_sdk_section,
        keep_it_simple_section,
        communication_style_section,
        user_tone_section,
        _output_conciseness_section,
        ui_delivery_section,
        _outcome_reporting_section,
        _action_caution_section,
        align_before_action_section,
        delegation_check_section,
        cross_machine_delegation_section,
        _file_driven_work_section,
        temporal_awareness_section,
        invest_in_the_future_section,
    )
    for section_fn in framework_sections:
        from functools import partial

        bound_section: Callable[[AgentSlices], str] = (
            partial(section_fn, catalog=catalog)
            if section_fn in catalog_sections
            else partial(section_fn)
        )
        contribution = bound_section(slices)
        if contribution:
            parts.append(contribution)
    for plugin, section_fn in extensions.system_prompt_sections():
        contribution = section_fn(slices, catalog=catalog)
        if contribution:
            parts.append(contribution)
            activation.record(
                plugin,
                "systemPromptSections",
                section_fn.__name__,
                model=slices.brain.llm_model,
                detail=(
                    f"chars={len(contribution)} "
                    f"sha={hashlib.sha256(contribution.encode()).hexdigest()[:12]}"
                ),
            )

    identity = catalog.identities.get(slices.brain.llm_model)
    if identity:
        parts.append(identity)

    if slices.read("agent", "prompt_knowledge_cutoff_enabled"):
        cutoff = catalog.knowledge_cutoffs.get(slices.brain.llm_model)
        if cutoff:
            parts.append(f"Knowledge cutoff: {cutoff}")

    return "\n\n".join(parts).rstrip("\n") + "\n"
