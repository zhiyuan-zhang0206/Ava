"""
System Prompt Extension Point — plugins inject behavior conventions into the system prompt.

Plugins register section functions via `register_system_prompt_section(fn)`.
`build_system_prompt()` concatenates in registration order: base prompt + SDK overview + all sections.

Usage (in plugin's plugin.py):

    from agent.graph._system_prompt import register_system_prompt_section

    @register_system_prompt_section
    def my_conventions() -> str:
        return "## My Plugin Conventions\\n\\nAlways do X before Y."
"""

import contextlib
import hashlib
import inspect
import io
import logging
import weakref
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from shared import plugin_activation, plugin_contributions
from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.paths import workspace_dir
from shared.plugin_context import current_plugin_name

from ._capabilities import (
    _CAPABILITY_SURFACES,
    _disabled_by_sdk_config,
    _is_capability_surface_member,
    capabilities_section,
    capability_index_is_empty,
)
from ._codeact import _codeact_section


def _resolved(setting: str) -> Any:
    """The per-model-resolved value of a prompt-behavior settings field for the
    agent's model: an explicit env/.env/overlay value wins, else the model's
    registry default, else the shared floor — see
    shared/lm/registry.py:resolve_setting. The behavioral sections below read
    their toggles through this so a model family can default to a different
    guidance profile without any per-cluster config."""
    from shared.lm.registry import resolve_setting

    return resolve_setting(setting, model=turn_settings.lm.llm_model)


_SYSTEM_PROMPT_SECTIONS: list[Callable[[], str]] = []

# section fn -> the plugin that registered it, read by `build_system_prompt` to
# attribute a section that actually contributed text. Weak keys so an entry dies
# with the function object; framework sections are absent (they register outside
# a `PluginContext`) and therefore stay untelemetered.
_SECTION_PLUGIN: weakref.WeakKeyDictionary[Callable[[], str], str] = weakref.WeakKeyDictionary()


def register_system_prompt_section(fn: Callable[[], str]) -> Callable[[], str]:
    """Register a system prompt section contributor — spliced into the system prompt at boot.

    Function signature `() -> str`; empty return treated as no contribution.
    `build_system_prompt()` runs them in registration order when called.
    """
    _SYSTEM_PROMPT_SECTIONS.append(fn)
    plugin_contributions.record("systemPromptSections", fn.__name__, detail=fn.__module__)
    plugin = current_plugin_name()
    if plugin is not None:
        _SECTION_PLUGIN[fn] = plugin
    return fn


# Framework-owned behavioral sections, grouped by bucket and registered (=
# rendered) in bucket order: SDK detail -> Conversation -> Conduct ->
# Capabilities. Each stays an independent AVA_SYSTEM_PROMPT_* / AVA_SDK_* toggle; the
# grouping is reading-order only, not nested headings (deferred until the count
# outgrows flat `#` headings — see future/coding/prompt-architecture.md
# "Section taxonomy"). Plugin tool sections append after these, so the full
# flow is identity (base, ends with the SDK overview) -> SDK detail ->
# conversation -> conduct -> capabilities -> tools.


# --- SDK detail: expanded contracts for the highest-frequency namespaces ---
def _discover_all_namespaces() -> list[str]:
    """Public ava namespaces the `"*"` expand entry stands for: every
    name in `help(ava)` that is itself a namespace (a module / namespace
    object), plus public nested submodules reachable through parent
    `__all_for_ava__` lists. Top-level functions (`help`, `understand`) are intentionally NOT
    discovered: the SDK overview already prints their full signature +
    docstring, so re-expanding them would only duplicate — the expanded
    reference earns its place only for namespaces, which the overview shows as a
    bare `from . import X` line. The capability surfaces (`_CAPABILITY_SURFACES`)
    are skipped for the same anti-duplication reason: `# Capabilities` is their
    index. Private names (leading underscore, e.g. a stray
    `_extend`) and any name removed via AVA_SDK_DISABLE are excluded too — a
    disabled namespace must never be expanded back into the prompt. Returned
    sorted so the rendered order is deterministic. Discovery is recursive: any
    module with a public `__all_for_ava__` is descended into, so `shell.sessions`
    (listed in `shell.__all_for_ava__`) is discovered without being listed
    explicitly alongside `"*"`."""
    import ava

    discovered: list[str] = []

    def _collect(parent: ModuleType, prefix: str) -> None:
        # agent_visible_names already drops underscore-prefixed names.
        for name in ava.agent_visible_names(parent):
            full = f"{prefix}.{name}" if prefix else name
            if full in _CAPABILITY_SURFACES or _disabled_by_sdk_config(full):
                continue
            attr = getattr(parent, name, None)
            if inspect.ismodule(attr) or isinstance(attr, SimpleNamespace):
                discovered.append(full)
                # Recurse into public nested namespaces
                if inspect.ismodule(attr) and hasattr(attr, "__all_for_ava__"):
                    _collect(attr, full)

    _collect(ava, "")
    return sorted(discovered)


def effective_sdk_expand() -> list[str]:
    """The merged expand list: plugin-registered paths (`ava.register_sdk_expand`)
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
    import ava

    configured: list[str] = []
    for entry in settings.agent.sdk_expand_in_system_prompt:
        if entry == "*":
            configured.extend(_discover_all_namespaces())
        else:
            configured.append(entry)

    merged = [*ava._REGISTERED_SDK_EXPANSIONS, *configured]
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


@register_system_prompt_section
def _sdk_expand_section() -> str:
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
    wanted = effective_sdk_expand()
    if not wanted:
        return ""
    import ava

    pieces: list[str] = []
    seen_targets: set[int] = set()
    # Text-only models drop media-gated members (`ava.self.attach`; ruling 2026-08-28).
    hidden: frozenset[str] = ava._attach.media_gated_members()
    _hidden_token = ava._hidden_surface_members.set(hidden)
    # Render classes compactly in the system prompt: show name + docstring +
    # field annotations + enum values, skip methods and nested classes. Fields
    # stay so the agent sees attribute names; the full contract (methods) is one
    # `ava.help(ava.X.ClassName)` away.
    _compact_token = ava._COMPACT_CLASSES.set(True)
    try:
        for path in wanted:
            if _disabled_by_sdk_config(path):
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
                    "duplicate points at a polluted list (a stray register_sdk_expand or "
                    "cross-test global-state leak). effective list was %r",
                    path,
                    wanted,
                )
                continue
            seen_targets.add(id(target))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ava.help(target)
            pieces.append(buf.getvalue().rstrip())
    finally:
        ava._hidden_surface_members.reset(_hidden_token)
        ava._COMPACT_CLASSES.reset(_compact_token)

    if not pieces:
        return ""
    body = "\n\n".join(pieces)
    return f"# Expanded SDK reference\n\nFull contracts for your most-used namespaces.\n\n{body}"


@register_system_prompt_section
def _prefer_sdk_section() -> str:
    """Toggle via settings.agent.prompt_prefer_sdk_enabled (env AVA_SYSTEM_PROMPT_PREFER_SDK,
    default on). One line steering the agent to the SDK over plain-Python /
    raw-shell equivalents; deliberately example-free."""
    if not _resolved("prompt_prefer_sdk_enabled"):
        return ""
    return (
        "# Prefer your SDK\n\n"
        "When an `ava.*` tool covers an operation, use it over a plain-Python "
        "or raw-shell equivalent."
    )


# CodeAct batching lives in `_codeact.py` (this module is at its line ceiling);
# registered here so the section order stays the reading order this module lays
# out — right after prefer-SDK, before keep-it-simple.
register_system_prompt_section(_codeact_section)


@register_system_prompt_section
def _keep_it_simple_section() -> str:
    """Toggle via settings.agent.prompt_keep_it_simple_enabled (env
    AVA_SYSTEM_PROMPT_KEEP_IT_SIMPLE, default on). Prefer mechanically correct,
    conceptually simple solutions over clever shortcuts, relentlessly even when
    the principled path is tedious."""
    if not _resolved("prompt_keep_it_simple_enabled"):
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
# The channel map every communication style opens with: where each kind of output
# actually lands. A fact about the system, not a preference, so it is shared
# rather than restated per style.
_OUTPUT_CHANNELS = (
    "Your output goes to three different places. Know which is which:\n\n"
    "- **Code output** (what `execute_code` returns) — only you see this. It is "
    "a feedback loop for yourself, not a channel to the user.\n"
    "- **Text content** (what you write here) — goes to your per-agent timeline. "
    "The user *can* open your dialog and read it, but when supervising many agents "
    "at once they rarely do.\n"
    "- **`ava.ui.notify`** — the only channel that reliably reaches the user's "
    "aggregated notification feed. For results, completed work, and decisions the "
    "user must see, this is the channel — not text."
)

_ORIENTED_BODY = (
    "Most of what you do — reading files, running commands, exploring — is "
    "invisible to the user. So don't work in long silences. Use the text you "
    "emit alongside each action to keep the user oriented if they open your "
    "dialog, and `ava.ui.notify` when you have a result or a decision they "
    "must not miss:\n\n"
    "- Before a long exploration or a multi-step task, say what you're about "
    "to do in a sentence.\n"
    '- Surface findings and direction changes as they happen — "the bug is '
    'in X, not Y as I first assumed" is worth a line.\n'
    "- Flag blockers and surprises right away, not only at the very end.\n\n"
    "Keep it brief: a sentence at meaningful moments, not a running "
    "commentary on every thought. There is no required cadence and no "
    "required wording — a short, honest update when the situation changes is "
    "the whole point. When a task is quick and self-explanatory, a single "
    "reply is fine; don't manufacture narration for its own sake."
)

_CONCISE_BODY = (
    "Speak at milestones, not while working. A milestone is one of:\n\n"
    "- You are starting something long enough that silence would be "
    "confusing — one sentence on what you're about to do.\n"
    "- The direction changed, or you found something that invalidates what "
    "you said earlier.\n"
    "- You hit a blocker, or you are done.\n\n"
    "Between those, work without commenting: no step-by-step narration, no "
    "restating a plan you already gave. When you do speak, one or two "
    "sentences is the size. Results and decisions the user must see still go "
    "through `ava.ui.notify` — the milestone budget applies to text content, "
    "not to reaching the user when it matters."
)

_SILENT_BODY = (
    "Work without narrating. While a task is in flight, emit no running "
    "commentary — no announcing the next step, no reporting what a command "
    "returned, no thinking out loud. Silence here costs the user nothing: "
    "text content is not the channel they watch.\n\n"
    "When the work is finished, give one complete report: what you did, what "
    "you found, what you changed, and anything you could not do. It has to "
    "stand alone — the user did not watch you get there, so do not lean on "
    "context they never saw. Send it through `ava.ui.notify` if it is a "
    "result or a decision they must see.\n\n"
    "Two things override the silence: a blocker you cannot resolve yourself, "
    "and a question only the user can answer. Raise those immediately — "
    "waiting until the end would waste the whole run."
)

# One rendered section per narrating style. Keys match the Literal on
# settings.agent.agent_communication_style minus 'off', so an unknown style
# raises rather than silently rendering nothing. 'off' is not a key here — it
# is handled as a gate in _communication_style_section, below.
_COMMUNICATION_STYLE_SECTIONS = {
    "oriented": f"# Keeping the user oriented\n\n{_OUTPUT_CHANNELS}\n\n{_ORIENTED_BODY}",
    "concise": f"# Talking to the user\n\n{_OUTPUT_CHANNELS}\n\n{_CONCISE_BODY}",
    "silent": f"# Talking to the user\n\n{_OUTPUT_CHANNELS}\n\n{_SILENT_BODY}",
}


@register_system_prompt_section
def _communication_style_section() -> str:
    """Selected by turn_settings.agent.agent_communication_style (env
    AVA_AGENT_COMMUNICATION_STYLE, default 'off'). Three styles carry the
    same output-channel map and differ only in how much the agent says while it
    works: 'oriented' interleaves brief updates, 'concise' speaks at milestones
    only, 'silent' stays quiet and reports once at the end. 'off' is the one
    gate in this set — no channel map, no narration guidance, the section is
    omitted from the system prompt entirely."""
    style = _resolved("agent_communication_style")
    if style == "off":
        return ""
    return _COMMUNICATION_STYLE_SECTIONS[style]


@register_system_prompt_section
def _output_conciseness_section() -> str:
    """Toggle via settings.agent.prompt_output_conciseness_enabled (env
    AVA_SYSTEM_PROMPT_CONCISENESS, default on). Shape the text content: answer-first,
    matched to the task, reference rather than dump."""
    if not _resolved("prompt_output_conciseness_enabled"):
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


@register_system_prompt_section
def _ui_delivery_section() -> str:
    """Toggle via settings.agent.prompt_ui_delivery_enabled (env
    AVA_SYSTEM_PROMPT_UI_DELIVERY, default on). Content for the user goes through
    the UI — never as a bare path to a Markdown file the user would have to open
    themselves. Deliberately example-free: the section states the semantic rule;
    the concrete UI entry points (and their signatures, which change far more
    often than the rule) live in the SDK overview / expanded reference. Files
    remain fine as persistence and as handoff artifacts for other agents; the
    user-facing presentation is the UI's job."""
    if not _resolved("prompt_ui_delivery_enabled"):
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
@register_system_prompt_section
def _outcome_reporting_section() -> str:
    """Toggle via settings.agent.prompt_outcome_reporting_enabled (env
    AVA_SYSTEM_PROMPT_REPORTING, default on). Report results honestly — no rounding a
    partial result up to success."""
    if not _resolved("prompt_outcome_reporting_enabled"):
        return ""
    return (
        "# Reporting honestly\n\n"
        "State outcomes as they are. If a test failed, a step didn't run, or you "
        "couldn't verify something, say so plainly — don't round a partial result "
        'up to success. "Changed X but couldn\'t run the tests" is more useful '
        "than a confident claim you never checked."
    )


@register_system_prompt_section
def _action_caution_section() -> str:
    """Toggle via settings.agent.prompt_action_caution_enabled (env AVA_SYSTEM_PROMPT_CAUTION,
    default on). Confirm before hard-to-reverse or outward-facing actions; treat
    sending to an outside service as publishing."""
    if not _resolved("prompt_action_caution_enabled"):
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


@register_system_prompt_section
def _align_before_action_section() -> str:
    """Toggle via settings.agent.prompt_align_before_action_enabled (env AVA_SYSTEM_PROMPT_ALIGN,
    default on). Before large or hard-to-redo work, and right after exploring or
    planning, confirm direction with the user instead of running on assumptions."""
    if not _resolved("prompt_align_before_action_enabled"):
        return ""
    return (
        "# Aligning before you commit to a direction\n\n"
        "Before work that is large, ambiguous, or hard to redo — and especially "
        "right after you finish exploring or planning — stop and confirm the "
        "direction with the user instead of running on your own assumptions. "
        "Surface the scope, the approach, and any open trade-offs, and let them "
        "steer before you build. Where the work has a rhythm to set — how it is "
        "split, how often to check in — the user may pin it or hand it back to "
        "you; if they defer, decide and say what you chose. One quick alignment "
        "up front beats unwinding a wrong direction after the work is done."
    )


# The five steps of the pre-work check, as bodies without their numbers: step 1
# only renders when there is a `# Capabilities` section to read, so the
# numbering — and the cost step's back-reference to the two delegation steps —
# is computed rather than written in.
_STEP_SKILL_INDEX = (
    "Does a skill already cover this? Read the `# Capabilities` index "
    "and match the task against it before starting any work. If there is even "
    "a 1% chance a listed skill applies, load it now with "
    "ava.help(ava.skills.<name>) and follow it — never rationalize skipping "
    'the check with "this is simple enough" or "I already know how"; do not '
    "work from general knowledge instead."
)

_STEP_NEIGHBORS = (
    "Is someone else already responsible? Look at the agents around you "
    "and scan their labels; if one's label names this domain, hand it the "
    "work — do not take over. If no label clearly covers it, ask the "
    "closest peers; only if they cannot place it either, spawn a worker "
    "for it. If you were spawned for a specific sub-task, finish that "
    "sub-task without expanding into adjacent domains."
)

_STEP_TOOLS = (
    "Does someone else have better tools? Tools are per-MACHINE, so this "
    "is about where an agent runs, not what it was given: email → an agent "
    "on a machine with the gmail skill; login-required browser tasks → an "
    "agent on a headed machine with the chrome MCP server; long coding "
    "tasks → a worker, or claude/codex via the ava-use-claude-code-and-codex "
    "skill. A worker you spawn already indexes every skill this machine has "
    "— the spawn brief must name the skill you expect it to use (a brief that "
    "does not name one is incomplete), rather than trying to hand it skills."
)

_STEP_COST = (
    "Would delegation cost more than the work? Keep it yourself only "
    "when the task is a quick single-step fix, describing it would take "
    "longer than doing it, and steps {delegation_steps} named no better agent."
)

_STEP_PARALLEL = (
    "Can the work be parallelized? Spawn one worker per independent "
    "part — their messages wake you as each reports — the ava-fleet "
    "skill has the full pattern."
)


@register_system_prompt_section
def _delegation_check_section() -> str:
    """Toggle via settings.agent.prompt_delegation_check_enabled (env
    AVA_SYSTEM_PROMPT_DELEGATION_CHECK, default on). Before taking on any work, run a
    30-second check — the most common fleet failure modes are skipping it and
    doing everything yourself, and rebuilding from general knowledge what a
    listed skill already covers. The skill-index step is the mandatory trigger
    for the `# Capabilities` index: an agent that never consults the index
    cannot know it is reinventing one of its own skills, so the obligation has
    to live in the one process the prompt marks as mandatory rather than in the
    index itself. It is dropped (and the rest renumbered) when this agent has no
    Capabilities section at all — see `capability_index_is_empty`."""
    if not _resolved("prompt_delegation_check_enabled"):
        return ""
    steps: list[str] = []
    if not capability_index_is_empty():
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


@register_system_prompt_section
def _cross_machine_delegation_section() -> str:
    """Toggle via settings.agent.prompt_cross_machine_delegation_enabled (env
    AVA_SYSTEM_PROMPT_CROSS_MACHINE_DELEGATION, default on). One sentence,
    user-finalized wording verbatim: when work spans machines, let an agent on
    the target machine do it rather than reaching across. Semantic steer only —
    no API detail (no spawn parameters, no SSH), so it cannot go stale."""
    if not _resolved("prompt_cross_machine_delegation_enabled"):
        return ""
    return _CROSS_MACHINE_DELEGATION_HINT


@register_system_prompt_section
def _file_driven_work_section() -> str:
    """Toggle via settings.agent.prompt_file_driven_work_enabled (env
    AVA_SYSTEM_PROMPT_FILE_DRIVEN_WORK, default on). When working on complex multi-step
    tasks, use files as working memory: write intermediate results to files,
    use worktrees for isolation, and hand off work to peer agents via handoff
    files rather than trying to fit everything into a message."""
    if not _resolved("prompt_file_driven_work_enabled"):
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


@register_system_prompt_section
def _temporal_awareness_section() -> str:
    """Toggle via settings.agent.prompt_temporal_awareness_enabled (env
    AVA_SYSTEM_PROMPT_TEMPORAL, default on). For events and releases after the training
    cutoff, assume you don't know — search before answering; don't guess from
    stale training data. At AI-capability scheduling, estimation, and feasibility
    moments, invoke the ai-capability-timescale skill for current cognition."""
    if not _resolved("prompt_temporal_awareness_enabled"):
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
        "such work, load the ai-capability-timescale skill and check the shared "
        "memory pool for the latest cognition — current capability can be an order of "
        "magnitude beyond what your cutoff suggests."
    )


# --- Capabilities: resources you can reach for ---
# The memory discipline section lives in the ava_memory plugin, which owns both
# memory stores: disabling the plugin removes the stores and the section that
# describes them together (ava_builtins/plugins/ava_memory/plugin.py).


@register_system_prompt_section
def _beyond_task_section() -> str:
    """Toggle via settings.agent.agent_reflection_enabled (env AVA_AGENT_REFLECTION,
    default on). Turns a finite context window into a prompt to surface follow-ups
    and candidate next steps instead of dropping them — offered to the user, and,
    when a fleet surrounds the agent, landed as open tasks in the task registry
    so they outlast the session. About acting on what it noticed (this session's work
    and the task registry), not cross-session memory (that is the memory section's
    job)."""
    if not _resolved("agent_reflection_enabled"):
        return ""
    return (
        "# Beyond the task at hand\n\n"
        "Your context window is finite, so something you notice mid-task but "
        "don't act on is easily lost. Don't let these evaporate:\n\n"
        "- A procedure you'd want to repeat — it could become a skill.\n"
        "- A rough edge in your own prompts, tools, or skills — unclear, "
        "contradictory, or missing — worth filing or fixing in place.\n"
        "- A follow-up past the task's natural next step — especially the "
        "tangential ones the user wouldn't think to ask for.\n\n"
        "When you finish, don't just stop. Offer the user 2-3 candidate next "
        "steps drawn from what you noticed — each a concrete option with a "
        "one-line reason, and say which you would pick first and why. Favor "
        "the non-obvious over the step they already have in mind; these are "
        "forward-looking options, not a recap. If nothing is genuinely worth "
        "doing next, say so in a line — a manufactured suggestion costs more "
        "than it gives. Skip this when the user has signaled they are done or "
        "the request was self-contained.\n\n"
        "When a fleet surrounds you, also capture the follow-ups worth doing "
        "as open tasks in the registry so they outlast this session, each "
        "description pinned to the evidence that motivated it (the file, the "
        "failing check, the log line). A standalone agent has no registry and "
        "no one else to escalate to — the user is your only source of "
        "direction."
    )


@register_system_prompt_section
def _workspace_section() -> str:
    """One-paragraph pointer to the per-agent workspace dir. Empty before a
    process identity is established (snapshot test / dev REPL renders) — the
    path is per-agent, so there is nothing stable to say without one. Also
    empty when settings.agent.workspace_in_system_prompt is off (bench runners):
    only the section is gated — the folder still exists and relative-path
    resolution still targets it.

    Uses ``{YOUR_AGENT_ID}`` placeholder so the system prompt is fork-safe:
    a fork copies the source agent's conversation (including the
    SystemMessage) into a new agent with a different id. The actual id is
    injected as a context note (``agent_id_note``) after each compact and at
    cold start — it lives outside the SystemMessage, so a fork does not
    carry a stale id."""
    import ava

    aid = ava._boot.agent_id()
    if aid is None or not settings.agent.workspace_in_system_prompt:
        return ""
    # Ensure the workspace directory exists (mkdir side effect).
    ws = workspace_dir(aid)
    try:
        base = f"~/{ws.relative_to(Path.home())}"
    except ValueError:
        base = str(ws)
    # Replace the concrete agent id with placeholder so the SystemMessage
    # carries no per-agent id — fork-safe. The agent learns its real id
    # from the ``agent_id_note`` context note injected beside this prompt.
    ws_display = base.replace(str(aid), "{YOUR_AGENT_ID}")
    return (
        "# Workspace\n\n"
        f"Your workspace is `{ws_display}` — your own stable folder for "
        "files you download or produce (reports, statements, artifacts). It "
        "survives restarts and nothing cleans it up behind you; relative paths "
        "in file and shell operations resolve here by default. Using it is "
        "optional: work that has a natural home — a repo checkout, a location "
        "the user names — belongs there, not in the workspace. Other agents "
        "have their own; share a file by sending its absolute path."
    )


# Capabilities lives in `_capabilities.py` (line budget) and is registered here so
# the section order stays the reading order this module lays out.
register_system_prompt_section(capabilities_section)


# Sections registered above this line are framework-owned (registered at module
# import). Everything appended later comes from a plugin via _load_extensions.
# clear_plugin_registrations() truncates back to this count so a plugin reload
# drops only plugin sections — the framework ones are never re-registered
# in-process, so clearing them would silently lose them for the rest of the run.
_FRAMEWORK_SECTION_COUNT = len(_SYSTEM_PROMPT_SECTIONS)


def build_system_prompt() -> str:
    """Build the full system prompt: base + SDK overview + plugin contributions.

    `_claim` node calls once when `state.messages` is empty; afterward
    SystemMessage is persisted into state[0] and reused across turn / restart
    — this function runs only once in an agent's lifetime. So the SDK
    overview is captured on-site via `_get_ava_overview()`, not cached.

    Call timing guarantees `_load_extensions()` has run (per `build_graph()`
    flow order), so plugin namespaces (`ava.cwd` etc.) make it into the
    `help(ava)` output.
    """
    from shared.config import settings

    from ._llm import _BASE_SYSTEM_PROMPT, _get_ava_overview

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
    for section_fn in _SYSTEM_PROMPT_SECTIONS:
        contribution = section_fn()
        if contribution:
            parts.append(contribution)
            # Activation telemetry (philosophy §6): a plugin section that
            # rendered text is prompt real estate the plugin is spending. Length
            # + digest identify *which* variant landed without storing the text;
            # this runs at spawn/compact only, so there is no per-turn cost.
            plugin_activation.record(
                _SECTION_PLUGIN.get(section_fn),
                "systemPromptSections",
                section_fn.__name__,
                detail=(
                    f"chars={len(contribution)} "
                    f"sha={hashlib.sha256(contribution.encode()).hexdigest()[:12]}"
                ),
            )
    # Model identity — per-model note telling the model what it runs on.
    from shared.lm.factory import MODEL_IDENTITY

    identity = MODEL_IDENTITY.get(turn_settings.lm.llm_model)
    if identity:
        parts.append(identity)
    # Knowledge cutoff — tail line so the agent knows its training-data
    # temporal boundary. Looked up from the current model; a model not in the
    # table produces no line (future models drop in by adding one entry).
    # Suppressible because temporal metadata is noise in a benchmark run, where
    # the task is dated by its repo state rather than by wall-clock time.
    if settings.agent.prompt_knowledge_cutoff_enabled:
        from shared.lm.factory import MODEL_KNOWLEDGE_CUTOFF

        cutoff = MODEL_KNOWLEDGE_CUTOFF.get(turn_settings.lm.llm_model)
        if cutoff:
            parts.append(f"Knowledge cutoff: {cutoff}")
    # Exactly one trailing newline regardless of which section lands last, so
    # the snapshot fixture is stable under the end-of-file-fixer pre-commit hook.
    return "\n\n".join(parts).rstrip("\n") + "\n"
