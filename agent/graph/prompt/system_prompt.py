"""System-prompt extension point for framework and plugin behavior sections.

`build_system_prompt()` combines the base prompt, SDK overview, the framework's own sections
(`FRAMEWORK_SECTIONS`, in reading order) and the sections plugins declare in `contribute()`
(`base.packages.plugins.extensions`), which arrive through the `ExtensionRegistry` the caller holds.
"""

import contextlib
import hashlib
import inspect
import io
import logging
from collections.abc import Callable, Sequence
from types import ModuleType, SimpleNamespace
from typing import Any

from agent.hooks.history_dump import workspace_section_hint
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugins import activation
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import workspace_dir

from ._codeact import _codeact_section
from .capabilities import (
    _CAPABILITY_SURFACES,
    _disabled_by_sdk_config,
    _is_capability_surface_member,
    capabilities_section,
    capability_index_is_empty,
)
from .conversation import COMMUNICATION_STYLE_SECTIONS, user_reply_section


def _resolved(setting: str, slices: AgentSlices, catalog: ModelCatalog) -> Any:
    """The per-model-resolved value of a prompt-behavior settings field for the
    agent's model: an explicit env/.env/overlay value wins, else the model's
    registry default, else the shared floor — see
    base/lm/registry.py:resolve_setting. The behavioral sections below read
    their toggles through this so a model family can default to a different
    guidance profile without any per-cluster config."""
    from base.lm.registry import resolve_setting

    return resolve_setting(
        setting,
        model=slices.brain.llm_model,
        models=catalog.models,
        explicit=slices.read("agent", setting),
    )


# Framework sections render by bucket: SDK detail -> Conversation -> Conduct -> Capabilities.


# --- SDK detail: expanded contracts for the highest-frequency namespaces ---
def _discover_all_namespaces(sdk_disable: Sequence[str]) -> list[str]:
    """Public ava namespaces the `"*"` expand entry stands for: every name in `help(ava)` that is
    itself a namespace (a module / namespace object), plus public nested submodules reachable
    through parent `__all_for_ava__` lists. Top-level functions (`help`, `understand`) are
    intentionally NOT discovered: the SDK overview already prints their full signature + docstring,
    so re-expanding them would only duplicate — the expanded reference earns its place only for
    namespaces, which the overview shows as a bare `from . import X` line. The capability surfaces
    (`_CAPABILITY_SURFACES`) are skipped for the same anti-duplication reason: `# Capabilities` is
    their index. Private names (leading underscore, e.g. a stray `_settings`) and any name removed
    via AVA_SDK_DISABLE are excluded too — a disabled namespace must never be expanded back into the
    prompt. Returned sorted so the rendered order is deterministic. Discovery is recursive: any
    module with a public `__all_for_ava__` is descended into, so `shell.sessions` (listed in
    `shell.__all_for_ava__`) is discovered without being listed explicitly alongside `"*"`."""
    import ava

    discovered: list[str] = []

    def _collect(parent: ModuleType, prefix: str) -> None:
        # agent_visible_names already drops underscore-prefixed names.
        for name in ava.agent_visible_names(parent):
            full = f"{prefix}.{name}" if prefix else name
            if full in _CAPABILITY_SURFACES or _disabled_by_sdk_config(full, sdk_disable):
                continue
            attr = getattr(parent, name, None)
            if inspect.ismodule(attr) or isinstance(attr, SimpleNamespace):
                discovered.append(full)
                # Recurse into public nested namespaces
                if inspect.ismodule(attr) and hasattr(attr, "__all_for_ava__"):
                    _collect(attr, full)

    _collect(ava, "")
    return sorted(discovered)


def effective_sdk_expand(sdk_disable: Sequence[str]) -> list[str]:
    """The merged expand list: plugin-declared paths (`sdk_namespaces(expand=True)` / `sdk_expansions`)
    first, then the configured framework list, deduped keep-first. Plugins lead
    because a plugin promotes its own highest-frequency surface (ava_code's cwd
    heads the coding namespaces); the framework default cannot name plugin
    namespaces, so the hook is their only way in. The ava_code prompt section
    consults this same view for its promote-vs-skip dedup.

    A `"*"` entry in the configured list is replaced in place by every public
    namespace discovered recursively through `_discover_all_namespaces`
    (sorted, minus the `_CAPABILITY_SURFACES` the Capabilities section indexes);
    explicit entries on either side of it survive and are deduped
    keep-first, so `["*", "shell.sessions"]` naturally dedupes — the wildcard
    already discovers `shell.sessions` and the explicit entry is dropped.
    An explicit `["*", "skills"]` is how an operator opts a capability surface
    back in — the wildcard skips it, the explicit entry does not. A member
    *inside* a surface (`skills.gmail`) is refused here with a warning, so the
    refusal holds for every consumer of this view and not only for the section
    that renders it.
    The literal `"*"` never reaches the returned list — it is resolved here,
    so every downstream consumer sees concrete paths only."""
    from ava.sdk_surface import install

    configured: list[str] = []
    for entry in settings.agent.sdk_expand_in_system_prompt:
        if entry == "*":
            configured.extend(_discover_all_namespaces(sdk_disable))
        else:
            configured.append(entry)

    merged = [*install.expansions(), *configured]
    seen: set[str] = set()
    resolved: list[str] = []
    for path in merged:
        if path in seen:
            continue
        seen.add(path)
        if _is_capability_surface_member(path):
            logging.getLogger(__name__).warning(
                "sdk_expand_in_system_prompt: refusing %r — expanding one member of a "
                "capability surface would render that skill's whole body (or that "
                "server's tool schemas) into every prompt and record a bogus 'loaded' "
                "attribution. Preload a skill with skills_to_expand_at_start; leave a "
                "server's tools to ava.help(ava.mcps.<server>)",
                path,
            )
            continue
        resolved.append(path)
    return resolved


def _sdk_expand_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Render the effective expand list (plugin registrations + env
    AVA_SDK_EXPAND, see `effective_sdk_expand`) as full `ava.help(ava.<path>)`
    stubs, directly after the SDK overview. Selection is frequency-driven
    (Huffman): always-on detail is paid only for the namespaces agents reach
    for most days — live error data showed guessed signatures cluster exactly
    there — while everything else keeps progressive disclosure via `ava.help`.
    The AVA_SDK_DISABLE config is consulted BEFORE attempting resolution: a
    disabled entry is skipped unconditionally — even if the name happened to
    still resolve, expanding a namespace the operator explicitly removed would
    leak it back into the prompt. A resolution failure after that filter has
    exactly one meaning (typo, or a plugin namespace whose plugin is not
    loaded): warn and skip.

    Each resolved namespace renders at most once. `effective_sdk_expand` already
    dedupes by path string, so two paths resolving to the SAME object (an alias,
    or a polluted expand list) is an anomaly — render it once (a repeated
    contract is pure prompt bloat) but WARN, so the upstream cause stays visible
    instead of being silently absorbed."""
    wanted = effective_sdk_expand(slices.prompt.sdk_disable)
    if not wanted:
        return ""
    import ava

    pieces: list[str] = []
    seen_targets: set[int] = set()
    # Text-only models drop media-gated members (`ava.self.attach`; ruling
    # 2026-08-28). Render parameters are arguments, passed per call — never set
    # process-wide. Render classes compactly in the system prompt: name +
    # docstring + field annotations + enum values, methods and nested classes
    # skipped — fields stay so the agent sees attribute names, and the full
    # contract (methods) is one `ava.help(ava.X.ClassName)` away.
    hidden: frozenset[str] = ava.sdk_surface.attachment_transport.media_gated_members(
        slices.brain.llm_model, catalog=catalog
    )
    for path in wanted:
        if _disabled_by_sdk_config(path, slices.prompt.sdk_disable):
            continue
        target: object = ava
        try:
            for segment in path.split("."):
                target = getattr(target, segment)
        except AttributeError:
            logging.getLogger(__name__).warning(
                "sdk_expand_in_system_prompt: ava.%s does not resolve and is not covered "
                "by AVA_SDK_DISABLE (typo, or a plugin namespace whose plugin is not "
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
            ava.help(target, compact_classes=True, hidden_members=hidden)
        pieces.append(buf.getvalue().rstrip())

    if not pieces:
        return ""
    body = "\n\n".join(pieces)
    return f"# Expanded SDK reference\n\nFull contracts for your most-used namespaces.\n\n{body}"


def _prefer_sdk_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_prefer_sdk_enabled (env AVA_SYSTEM_PROMPT_PREFER_SDK,
    default on). One line steering the agent to the SDK over plain-Python /
    raw-shell equivalents; deliberately example-free."""
    if not _resolved("prompt_prefer_sdk_enabled", slices, catalog):
        return ""
    return (
        "# Prefer your SDK\n\n"
        "When an `ava.*` tool covers an operation, use it over a plain-Python "
        "or raw-shell equivalent."
    )


# CodeAct batching lives in `_codeact.py` (this module is at its line ceiling);
# listed in `FRAMEWORK_SECTIONS` so the section order stays the reading order this module
# lays out — right after prefer-SDK, before keep-it-simple.


def _keep_it_simple_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_keep_it_simple_enabled (env
    AVA_SYSTEM_PROMPT_KEEP_IT_SIMPLE, default on). Prefer mechanically correct,
    conceptually simple solutions over clever shortcuts, relentlessly even when
    the principled path is tedious."""
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


# --- Conversation: how you talk to the user ---


def _communication_style_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Selected by agent_communication_style (env
    AVA_AGENT_COMMUNICATION_STYLE, default 'off'). Three styles carry the
    narration guidance and differ in how much the agent says while it
    works: 'oriented' interleaves brief updates, 'concise' speaks at milestones
    only, 'silent' stays quiet and reports once at the end. 'off' is the one
    gate in this set: the optional narration section is omitted. Reply routing
    and the initial human-response rule remain in conversation.user_reply_section."""
    style = _resolved("agent_communication_style", slices, catalog)
    if style == "off":
        return ""
    return COMMUNICATION_STYLE_SECTIONS[style]


_STRONG_USER_TONE = "You are a trusted peer to the user, not a cheerleader. Never open with empty praise or reassurance — answer the question, do not validate it. Assess the user's ideas on your own before responding; when you hold a real reservation or disagreement, say it and the reasoning behind it before carrying out the adopted course. Honest judgment is more useful than agreement, and unsupported approval is not helpful even when it feels polite. Keep the tone direct and equal — be the partner who keeps the user honest, not the echo that repeats them."
_LIGHT_USER_TONE = "State conclusions and judgments directly instead of hedging what you know. Declare uncertainty only where it is genuinely real; do not perform modesty or dress a known fact as a guess. Be direct and to the point — the useful answer is the specific one, not the careful one."
_VERY_LIGHT_USER_TONE = "Be honest and direct, and keep honesty from turning into lecturing. Disagree when it is warranted and give the reasoning — but stay a peer, not a schoolmaster. Warmth and candor both belong here; condescension does not."
_USER_TONE_SECTIONS = {"gemini": _STRONG_USER_TONE, "claude": _VERY_LIGHT_USER_TONE}


def _user_tone_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Independent from ``agent_communication_style`` (narration volume vs tone), with a per-family strength gradient; every Claude model defaults off unless explicitly enabled."""
    if not _resolved("prompt_user_tone_enabled", slices, catalog):
        return ""
    spec = catalog.models.get(slices.brain.llm_model)
    return f"# Communicating with the user\n\n{_USER_TONE_SECTIONS.get(spec.provider if spec is not None else '', _LIGHT_USER_TONE)}"


def _output_conciseness_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_output_conciseness_enabled (env
    AVA_SYSTEM_PROMPT_CONCISENESS, default on). Shape the text content: answer-first,
    matched to the task, reference rather than dump."""
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
        "filler — empty openers and wrap-up restatements that just repeat what "
        "you did. Say the thing and stop."
    )


def _ui_delivery_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_ui_delivery_enabled (env
    AVA_SYSTEM_PROMPT_UI_DELIVERY, default on). Content for the user goes through
    the UI — never as a bare path to a Markdown file the user would have to open
    themselves. Deliberately example-free: the section states the semantic rule;
    the concrete UI entry points (and their signatures, which change far more
    often than the rule) live in the SDK overview / expanded reference. Files
    remain fine as persistence and as handoff artifacts for other agents; the
    user-facing presentation is the UI's job."""
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


# --- Conduct: how you behave and what judgment to apply ---
def _outcome_reporting_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_outcome_reporting_enabled (env
    AVA_SYSTEM_PROMPT_REPORTING, default on). Report results honestly — no rounding a
    partial result up to success."""
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
    """Toggle via settings.agent.prompt_action_caution_enabled (env AVA_SYSTEM_PROMPT_CAUTION,
    default on). Confirm before hard-to-reverse or outward-facing actions; treat
    sending to an outside service as publishing."""
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


def _align_before_action_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_align_before_action_enabled (env AVA_SYSTEM_PROMPT_ALIGN,
    default on). Resolve material intent or authority choices without reopening
    settled instructions merely because exploration or planning finished."""
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


# The five steps of the pre-work check, as bodies without their numbers: step 1
# only renders when there is a `# Capabilities` section to read, so the
# numbering — and the cost step's back-reference to the two delegation steps —
# is computed rather than written in.
_STEP_SKILL_INDEX = (
    "Does a skill already cover this? Read the `# Capabilities` index "
    "and match the task against descriptions and expected outputs before "
    "starting work. Load applicable skills with ava.help(ava.skills.<name>). "
    "For non-trivial, ambiguous, consequential, sustained, or parallel work, "
    "load ava-workflow when available to choose how to work. Its lightweight "
    "entry selects only the needed methods; it does not mandate an interview, "
    "goal supervision, a plan document, or delegation."
)

_STEP_NEIGHBORS = (
    "Is someone else already responsible? Look at the agents around you "
    "and check existing responsibility before taking over shared work. "
    "Reuse a suitable peer when collaboration helps; an absent domain owner "
    "does not require spawning a worker. If you were assigned a specific "
    "task, finish it without expanding into adjacent domains."
)

_STEP_TOOLS = (
    "Does someone else have better tools? Tools are per-MACHINE, so this "
    "is about where an agent runs. Consider a peer on a machine with the "
    "needed email or headed-browser access, or ava-guide.external-agents for "
    "coding work when appropriate. Check actual capabilities and authority. "
    "A peer brief names relevant skills when needed, the outcome, and "
    "acceptance evidence; peers discover skills through their own index."
)

_STEP_COST = (
    "Would delegation improve the result? If steps {delegation_steps} named no better agent, "
    "choose working yourself, reusing a peer, or spawning peers based on "
    "context, cost, latency, and verification needs. Existing authorization "
    "and budget boundaries govern every choice."
)

_STEP_PARALLEL = (
    "Can the work be parallelized? Choose a few persistent peers for simple "
    "collaboration, or load ava-dynamic-workflow when available to generate "
    "an orchestration script for large fan-outs or repeated stages. Decide "
    "when to gather results and wake; parallelizable work does not require "
    "delegation or one model turn per peer completion."
)


def _delegation_check_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_delegation_check_enabled (env
    AVA_SYSTEM_PROMPT_DELEGATION_CHECK, default on). Before taking on any work, run a
    30-second capability and delegation check rather than rebuilding from
    general knowledge what a listed skill already covers. The skill-index step
    selects a working strategy without forcing delegation. It is the trigger
    for the `# Capabilities` index: an agent that never consults the index
    cannot know it is reinventing one of its own skills, so the obligation has
    to live in the one process the prompt marks as mandatory rather than in the
    index itself. It is dropped (and the rest renumbered) when this agent has no
    Capabilities section at all — see `capability_index_is_empty`."""
    if not _resolved("prompt_delegation_check_enabled", slices, catalog):
        return ""
    steps: list[str] = []
    if not capability_index_is_empty(slices.prompt):
        steps.append(_STEP_SKILL_INDEX)
    first_delegation_step = len(steps) + 1
    steps.append(_STEP_NEIGHBORS)
    steps.append(_STEP_TOOLS)
    steps.append(
        _STEP_COST.format(delegation_steps=f"{first_delegation_step}-{first_delegation_step + 1}")
    )
    steps.append(_STEP_PARALLEL)
    body = "\n".join(f"{n}. {step}" for n, step in enumerate(steps, start=1))
    return (
        "# Before you act — check\n\n"
        "You are one agent in a fleet. Before taking on work yourself, run this "
        "30-second check; skipping it is the most common failure mode in this "
        "fleet.\n\n"
        f"{body}\n\n"
        "Then proceed — delegate, or do it yourself as a conscious choice "
        "rather than a default."
    )


_CROSS_MACHINE_DELEGATION_HINT = (
    "When working across different machines, consider spawning an agent on "
    "the target machine and let it do the work for you, as it can access the "
    "machine's resources directly."
)


def _cross_machine_delegation_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_cross_machine_delegation_enabled (env
    AVA_SYSTEM_PROMPT_CROSS_MACHINE_DELEGATION, default on). One sentence,
    user-finalized wording verbatim: when work spans machines, let an agent on
    the target machine do it rather than reaching across. Semantic steer only —
    no API detail (no spawn parameters, no SSH), so it cannot go stale."""
    if not _resolved("prompt_cross_machine_delegation_enabled", slices, catalog):
        return ""
    return _CROSS_MACHINE_DELEGATION_HINT


def _file_driven_work_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_file_driven_work_enabled (env
    AVA_SYSTEM_PROMPT_FILE_DRIVEN_WORK, default on). When working on complex multi-step
    tasks, use files as working memory: write intermediate results to files,
    use worktrees for isolation, and hand off work to peer agents via handoff
    files rather than trying to fit everything into a message."""
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


def _temporal_awareness_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_temporal_awareness_enabled (env
    AVA_SYSTEM_PROMPT_TEMPORAL, default on). For events and releases after the training
    cutoff, assume you don't know — search before answering; don't guess from
    stale training data. At AI-capability scheduling, estimation, and feasibility
    moments, invoke the ava-workflow.capability-timescale skill for current cognition."""
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
        "such work, load ava.skills.ava_workflow.capability_timescale and check the shared "
        "memory pool and current primary sources for task-relevant evidence. "
        "State verified capability and uncertainty instead of assuming a fixed improvement."
    )


# --- Capabilities: resources you can reach for ---
# The memory discipline section lives in the ava_memory plugin, which owns both
# memory stores: disabling the plugin removes the stores and the section that
# describes them together (ava_builtins/plugins/ava_memory/plugin.py).


_INVEST_IN_THE_FUTURE_SECTION = (
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


def _invest_in_the_future_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_invest_future_enabled (env
    AVA_SYSTEM_PROMPT_INVEST_FUTURE, default on through the per-model floor).
    The framework's ONE cross-domain future-signal rule, merged from the former
    # Beyond the task at hand; its closing-presentation duty now lives in the
    final paragraph."""
    if not _resolved("prompt_invest_future_enabled", slices, catalog):
        return ""
    return _INVEST_IN_THE_FUTURE_SECTION


def _workspace_section(slices: AgentSlices, *, agent_id: int | None) -> str:
    """One-paragraph pointer to the per-agent workspace dir. Empty before a
    process identity is established (snapshot test / dev REPL renders) — the
    note that carries the concrete path is injected beside this prompt, so
    without an identity there is nothing to point at. Also empty when
    settings.agent.workspace_in_system_prompt is off (bench runners): only the
    section is gated — the folder still exists and relative-path resolution
    still targets it.

    The concrete path is deliberately NOT interpolated here: a fork copies the
    source agent's conversation (including the SystemMessage) into a new agent
    with a different id, so a baked-in id or path would name the source's
    folder. The section stays cluster-identical text; the per-agent path rides
    the ``agent_id_note`` context note instead — injected after each compact
    and at cold start, and regrafted by a fork, so it is always the reader's
    own path."""
    if agent_id is None or not settings.agent.workspace_in_system_prompt:
        return ""
    # Ensure the workspace directory exists (mkdir side effect).
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
        "absolute path." + workspace_section_hint(slices.history_dump)
    )


def _long_running_operation_section(_slices: AgentSlices) -> str:
    """Core lifecycle and cost discipline, independent of collaboration plugins."""
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
        "procedures, load the ava-being-a-long-running-agent skill when available."
    )


# Capabilities lives in `capabilities.py` (line budget) and is listed here so
# the section order stays the reading order this module lays out.


# The framework-owned sections, in reading order. Plugin sections follow them.
FRAMEWORK_SECTIONS: tuple[Callable[..., str], ...] = (
    _sdk_expand_section,
    _prefer_sdk_section,
    _codeact_section,
    _keep_it_simple_section,
    user_reply_section,
    _communication_style_section,
    _user_tone_section,
    _output_conciseness_section,
    _ui_delivery_section,
    _outcome_reporting_section,
    _action_caution_section,
    _align_before_action_section,
    _delegation_check_section,
    _cross_machine_delegation_section,
    _file_driven_work_section,
    _long_running_operation_section,
    _temporal_awareness_section,
    _invest_in_the_future_section,
)


def build_system_prompt(
    extensions: ExtensionRegistry,
    slices: AgentSlices,
    *,
    agent_id: int | None,
    catalog: ModelCatalog,
) -> str:
    """Build the full system prompt: base + SDK overview + plugin contributions.

    `_claim` node calls once when `state.messages` is empty; afterward
    SystemMessage is persisted into state[0] and reused across turn / restart
    — this function runs only once in an agent's lifetime. So the SDK
    overview is captured on-site via `_get_ava_overview()`, not cached.

    Call timing guarantees `load_extensions()` has run (per `build_graph()`
    flow order), so plugin namespaces (`ava.cwd` etc.) make it into the
    `help(ava)` output.
    """
    from base.config import settings

    from ._base_prompt import _BASE_SYSTEM_PROMPT, _get_ava_overview

    if settings.agent.prompt_sdk_overview_enabled:
        parts = [_BASE_SYSTEM_PROMPT.format(_AVA_OVERVIEW=_get_ava_overview())]
    else:
        # Bare identity — no SDK overview, just the one-paragraph preamble
        parts = [
            """\
You are Ava, an agent that acts by writing Python code — call the
`execute_code(code: str)` tool — each call runs in an ephemeral interpreter. To idle, do not output any
tool calls. Before using any `ava.*` function, you must explicitly `import ava` in your code.
"""
        ]

    def workspace_section(agent: AgentSlices) -> str:
        return _workspace_section(agent, agent_id=agent_id)

    framework_sections: tuple[Callable[..., str], ...] = (
        *FRAMEWORK_SECTIONS,
        workspace_section,
        capabilities_section,
    )
    catalog_sections = (
        _sdk_expand_section,
        _prefer_sdk_section,
        _keep_it_simple_section,
        _communication_style_section,
        _user_tone_section,
        _output_conciseness_section,
        _ui_delivery_section,
        _outcome_reporting_section,
        _action_caution_section,
        _align_before_action_section,
        _delegation_check_section,
        _cross_machine_delegation_section,
        _file_driven_work_section,
        _temporal_awareness_section,
        _invest_in_the_future_section,
        capabilities_section,
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
    # Model identity — per-model note telling the model what it runs on.
    identity = catalog.identities.get(slices.brain.llm_model)
    if identity:
        parts.append(identity)
    # Knowledge cutoff — tail line so the agent knows its training-data
    # temporal boundary. Looked up from the current model; a model not in the
    # table produces no line (future models drop in by adding one entry).
    # Suppressible because temporal metadata is noise in a benchmark run, where
    # the task is dated by its repo state rather than by wall-clock time.
    if settings.agent.prompt_knowledge_cutoff_enabled:
        cutoff = catalog.knowledge_cutoffs.get(slices.brain.llm_model)
        if cutoff:
            parts.append(f"Knowledge cutoff: {cutoff}")
    # Exactly one trailing newline regardless of which section lands last, so
    # the snapshot fixture is stable under the end-of-file-fixer pre-commit hook.
    return "\n\n".join(parts).rstrip("\n") + "\n"
