# Ava / Ava Code prompt architecture

Living doc (opened 2026-06-01). Where coding-agent behavioral guidance belongs —
the Ava-core vs `ava_code` split, and the mechanics-vs-behavior axis under it.
**Most of the inventory below has since shipped**; see "Status / next" for what is
actually left.

## Why this doc

Benchmarking Ava's prompt against the current Claude Code (Opus 4.8) and Codex
(GPT-5.5) prompts surfaced two questions:

1. Several behavioral dimensions those agents spell out, Ava's prompt is silent
   on. Which are real gaps worth closing, and which are deliberately stripped or
   covered elsewhere (`AGENTS.md`, skills)?
2. When we do add guidance, where does it live — the Ava **core** system prompt,
   or the **`ava_code`** plugin section?

This doc answers (2) as a standing principle and records (1) as an inventory.

## Current prompt surface

### Section taxonomy (four buckets)

The prompt is assembled as **base (identity + action model) → framework
behavioral sections → plugin tool sections** (`build_system_prompt()`). The
framework sections are grouped into four buckets and registered in this reading
order. The grouping is **reading-order + a placement rule for new sections, not
nested headings** — nesting (an umbrella `#` with child `##`) is deferred until
the flat `#` list outgrows legibility; until then each section stays a flat `#`
block with its own `AVA_SYSTEM_PROMPT_*` toggle.

1. **Identity & action model** — who you are + the `execute_code` contract.
   Lives in the base prompt, always first.
2. **SDK detail** — full contracts (signatures + docstrings) for the configured
   namespaces, rendered directly after the SDK overview so the agent calls them
   without a drill-down turn. Config-selected (`AVA_SDK_EXPAND`), default `*` =
   every top-level public namespace **except the capability surfaces**
   (`ava.skills`, `ava.mcps`); an explicit list (no `*`) narrows to a
   frequency-driven subset (Huffman) when guessed signatures cluster in the
   most-used namespaces, leaving rare ones to progressive disclosure via
   `ava.help`. The two-bucket split is load-bearing: this bucket carries **call
   contracts**, Capabilities carries **what you can do**. Skills and MCP servers
   are members of a capability surface rather than SDK API, so expanding them
   here would render a full second index of exactly what Capabilities lists —
   which is what shipped by accident until the surfaces were excluded from `*`.
3. **Conversation** — how you talk to the user: progress narration, output shape.
4. **Conduct** — how you behave / what judgment to apply: reporting honestly,
   action caution, align-before-action.
5. **Capabilities** — resources you can reach: memory behavior, skills index,
   and (appended by plugins) tool-usage guidance.

A new behavioral section goes into the bucket it fits and is registered next to
its bucket-mates in `_system_prompt.py` (bucket-comment markers delimit them).
Note "tool calling" is **not** a peer bucket for Ava the way it is for a
multi-tool agent (e.g. Claude Code's per-tool `##` catalog): Ava has one tool
(`execute_code`) and exposes capability through the `ava.*` namespace, so tool
guidance is the *spine* (base + SDK overview + plugin sections), folded into
Capabilities rather than standing as its own section.

- **Core base prompt** — `agent/graph/_llm.py:_BASE_SYSTEM_PROMPT`: the
  code-as-action contract (`execute_code`, speak via text content, empty
  tool-call = idle) plus the `help(ava)` SDK overview.
- **Core expanded SDK reference** — `agent/graph/_system_prompt.py:_sdk_expand_section`,
  rendering `effective_sdk_expand()`: plugin registrations
  (`ava.register_sdk_expand`, e.g. ava_code's `cwd`) first, then
  `settings.sdk_expand_in_system_prompt` (env `AVA_SDK_EXPAND`, default `*`),
  deduped keep-first — full `ava.help(ava.<path>)` stubs right after the
  overview. `*` expands to every top-level public namespace (discovered from
  `help(ava)`, sorted; top-level functions like `help`/`understand` are skipped
  — the overview already prints them in full; `skills` / `mcps` are skipped as
  capability surfaces, `_CAPABILITY_SURFACES`; naming a surface explicitly
  overrides, but a path INSIDE one — `skills.gmail`, `mcps.chrome` — is refused
  with a warning even then, because resolving it inlines that skill's whole
  SKILL.md body / that server's tool schemas into every prompt and records a
  bogus `loaded` attribution; a skill body belongs in
  `skills_to_expand_at_start`) and merges with any explicit
  entries, so `*,shell.sessions` reaches a nested path `*` cannot. AVA_SDK_DISABLE
  is consulted before resolution (a disabled entry is skipped unconditionally
  and silently, and excluded from `*`); any other resolution miss warns. The
  `ava_code` plugin's `_coding_tools_section` consults the same effective view
  and skips a module already expanded (exact path match) — with the default `*`
  every framework module is expanded, so it stays preamble-only.
- **Core prefer-SDK nudge** — `agent/graph/_system_prompt.py:_prefer_sdk_section`,
  on by default via `settings.prompt_prefer_sdk_enabled` (env
  `AVA_SYSTEM_PROMPT_PREFER_SDK`): one line steering the agent to `ava.*` tools over
  plain-Python / raw-shell equivalents. Deliberately example-free — specific
  misuse patterns get addressed if logs show them.
- **Core CodeAct batching** — `agent/graph/_codeact.py:_codeact_section` (registered by `_system_prompt.py`),
  **off by default** via `settings.agent.prompt_codeact_enabled` (env
  `AVA_SYSTEM_PROMPT_CODEACT`): pack several operations into one `execute_code`
  call — batch file reads, fold branches into if-else logic — because each
  call is one LLM API round-trip. Opt-in (user ruling 2026-08-26): unlike the
  on-by-default behavioral sections, an unconfigured cluster never pays for it.
- **Core capabilities index** — `agent/graph/_capabilities.py:capabilities_section`:
  always-on name + one-line description of the capabilities the agent already
  has, under one `# Capabilities` heading — the prompt's ONE skill index.
  Two halves: injected skills (`_skill_index_lines`, keyed by
  `ava.skills.<...>` path, bodies load on demand) and configured MCP tool
  servers (`_mcp_index_lines`, keyed by `ava.mcps.<server>`, descriptions from
  each server's optional `description` field in its `.mcp.json`). Both keys use
  the same form the expanded-SDK section uses.
  `skills_to_inject_into_system_prompt` defaults to `*` (the whole catalog); an
  explicit list narrows one agent's index — hiding entries from this listing
  only, which is why the header says so and points at `ava.help(ava.skills)`.
  The section first tells the agent to match every task against the index before
  rebuilding something it already has. It then, by default, tells the agent to
  name the matching skill or skills and why before starting, and to include that
  reasoning in its final output. `AVA_SYSTEM_PROMPT_CAPABILITIES_MATCH_FIRST`
  can explicitly disable only that latter instruction for rollback. The
  delegation check still makes consultation mandatory as its first step and
  drops that step when this section renders nothing.
- **Core communication style** — `agent/graph/_system_prompt.py:_communication_style_section`,
  selected by `settings.agent.agent_communication_style` (env
  `AVA_AGENT_COMMUNICATION_STYLE`, default `off`): how much the agent narrates
  while it works — `oriented` interleaves brief progress updates in its text content,
  `concise` speaks at milestones only, `silent` works quietly and reports once at the
  end, `off` omits the section from the system prompt entirely. General-agent behavior
  (see the section below for the deliberate restraint vs Codex).
- **`ava_memory` behavior** — `ava_builtins/plugins/ava_memory/plugin.py:memory_discipline_section()`
  (registered via `@register_system_prompt_section`; the old `agent/graph/_system_prompt.py:_memory_behavior_section`
  moved out of core with the plugin split),
  on by default via `settings.agent.prompt_memory_behavior_enabled` (env
  `AVA_SYSTEM_PROMPT_MEMORY`): the *when / what to remember* behavioral layer over the
  `ava.memory` pool — including memory-maintenance priority: a stale or wrong note
  is corrected first, before the agent continues the task it was on. The *how*
  (read/write/search/commit) stays in the `ava.memory` SDK docstrings — this
  section does not repeat it. Auto-suppressed when both memory stores are
  switched off, so bench agents drop it without a bench-specific toggle.
- **Core output shape** — `agent/graph/_system_prompt.py:_output_conciseness_section`,
  on by default via `settings.prompt_output_conciseness_enabled` (env
  `AVA_SYSTEM_PROMPT_CONCISENESS`): reply matched to the task, answer-first, reference
  paths/lines instead of pasting output back. General-agent behavior.
- **Core UI delivery** — `agent/graph/_system_prompt.py:_ui_delivery_section`,
  on by default via `settings.prompt_ui_delivery_enabled` (env
  `AVA_SYSTEM_PROMPT_UI_DELIVERY`): content for the user goes through the UI,
  never as a bare path to a Markdown file; files keep their role as persistence
  and cross-agent handoff. Deliberately example-free — a semantic rule, not an
  API list (signatures drift; the SDK overview / expanded reference carry the
  concrete entry points). Complements output shape (how the *text* is written)
  with *where the deliverable lands*.
- **Core future signals** — `agent/graph/_system_prompt.py:_invest_in_the_future_section`,
  on by default via `settings.agent.prompt_invest_future_enabled` (env
  `AVA_SYSTEM_PROMPT_INVEST_FUTURE`): the framework's one cross-domain
  future-signal rule. It requires the smallest closing action for a signal that
  could improve later work and preserves worthwhile follow-ups at task close.
- **Core reporting honesty** — `agent/graph/_system_prompt.py:_outcome_reporting_section`,
  on by default via `settings.prompt_outcome_reporting_enabled` (env
  `AVA_SYSTEM_PROMPT_REPORTING`): state outcomes as they are, don't round a partial
  result up to success.
- **Core action caution** — `agent/graph/_system_prompt.py:_action_caution_section`,
  on by default via `settings.prompt_action_caution_enabled` (env
  `AVA_SYSTEM_PROMPT_CAUTION`): confirm before hard-to-reverse / outward-facing actions
  (one approval ≠ standing license), and treat sending to an external service as
  publishing. Combines the irreversible-action and external-send-privacy gaps.
- **Core align-before-action** — `agent/graph/_system_prompt.py:_align_before_action_section`,
  on by default via `settings.prompt_align_before_action_enabled` (env
  `AVA_SYSTEM_PROMPT_ALIGN`): before large / ambiguous / hard-to-redo work, and right
  after exploring or planning, confirm scope + approach with the user instead of
  running on assumptions; let the user pin or defer the working rhythm. A
  general-agent behavior, distinct from action-caution (which gates individual
  irreversible ops, not the direction of substantial work).
- **`ava_code` plugin sections** — both in `ava_builtins/plugins/ava_code/plugin.py`:
  - `_coding_tools_section`: "prefer `ava.cwd`/`files`/`shell`", set cwd first,
    read `AGENTS.md` before reading code, then the promoted module stubs. Its
    always-on preamble also carries the coding conventions: worktree+PR flow and
    verify-before-done (run the narrow check, then widen).
  - `_engineering_workflow_section`: the reproduce-then-map-the-failure-space
    debugging mindset, opt-in by adding `ava_code_workflow` to
    `settings.agent.system_prompt_extra` (env `AVA_SYSTEM_PROMPT_EXTRA`).
    Coding-specific, so it is owned by the plugin rather than the core prompt.

Everything else (git/PR protocol, scope discipline, doc discipline) is loaded at
runtime from the project's `AGENTS.md`, not baked into the framework prompt.

## Responsibility split: Ava core vs ava_code

Rule of thumb:

- **General-agent behavior → Ava core.** If any Ava agent (life helper, inbox
  sweeper, a non-coding session) would also benefit, it belongs in the core
  prompt. Examples: how to narrate progress, output conciseness, when to ask vs
  act, caution around irreversible actions in general.
- **Coding-specific behavior → `ava_code` plugin.** If it only matters when
  writing or running code, consolidate it into the plugin section so non-coding
  agents never carry it. Examples: worktree workflow, running/locating tests,
  editing discipline (don't revert the user's unrelated working-tree changes,
  prefer minimal focused diffs), reading `AGENTS.md`.
- **Memory: Ava core, and only Ava core. `ava_code` gets no memory layer of its
  own — rejected, not deferred.** Memory is a cross-cutting capability owned by
  the cluster's memory plugin; it does not hang off any one vertical scenario, and
  "coding" is not special enough to earn a private one. Giving `ava_code` its own
  memory layer would be a layering error: a second memory system, shaped by one
  use case, sitting beside the general one.

  The thing such a layer would have stored — how this repo's tests run, how it
  builds, its conventions — belongs in that repo's `AGENTS.md`: version-controlled,
  visible to humans *and* every agent, reviewable when wrong. Putting shareable
  repo facts into one agent's private memory converts a public fact into private
  property, and makes it un-reviewable. A coding agent re-exploring a repo at the
  start of a session is **not a defect** to be optimized away with a memory cache.

  So the general memory model (what to persist across sessions, user
  profile/preferences) lives with Ava core as `memory_discipline_section`, and that
  is the whole story. Do not re-propose a per-repo `ava_code` memory layer.

The test for any new guidance: "would a non-coding Ava agent want this?" Yes →
core. Only-when-coding → `ava_code`.

### SDK docstring vs system prompt — where guidance lives

A second axis runs orthogonal to core-vs-`ava_code`: **mechanics vs behavior.**

- **SDK docstrings** (`ava.*`) answer *how to use a capability* — the call
  signature, what it returns, how to commit a memory note, how `search` ranks.
  This is reference the agent pulls up while acting.
- **The system prompt** answers *when / whether to do something* — a behavioral
  convention that holds regardless of which call implements it. "Persist durable
  user preferences across sessions" is a convention; it does not belong in a
  function docstring.

So memory splits cleanly: the `ava.memory` docstrings already carry the
mechanics (commit-yourself, header-stamp, search→read, pull-rebase on miss), and
`memory_discipline_section` carries only the when/what. The prompt section
deliberately does not restate the mechanics, and defers to `ava.memory` for
them.

## Section ownership: framework vs plugin

Could an agent without this plugin execute this rule? Yes → framework; no →
plugin. The framework owns unconditional behavioral constraints and must not
assume a task, memory, or fleet capability exists. A plugin owns how to act
using that plugin's store, task surface, or hook.

| section | owner | toggle | unique content |
|---|---|---|---|
| `# Invest in the future` | framework | `AVA_SYSTEM_PROMPT_INVEST_FUTURE` (default on) | The ONE cross-domain future-signal rule: trigger, three closing actions, over-capture bias, and closing presentation. |
| `# Remembering across sessions` | `ava_memory` plugin | `AVA_SYSTEM_PROMPT_MEMORY` | Durable-knowledge domain instance: dual stores, fix stale, format, and verification; no generic rule restatement. |
| Fleet task-interaction instance (PR2) | `ava_fleet` plugin | plugin enabled | Lands separately with task-specific interaction guidance. |

Review every section with these questions:

- Is this a cross-capability principle (framework) or operational instructions
  for an already-loaded capability (plugin)?
- Would the rule still hold with every plugin disabled?
- Is the same rule restated generically anywhere else? (exactly-one-owner check)

## Communication style (implemented, config-selected)

A lightweight conversation section lives in the **Ava core** prompt
(`_communication_style_section`) — it is general-agent behavior, not
coding-specific. It is selected by an enum, `settings.agent.agent_communication_style`,
env `AVA_AGENT_COMMUNICATION_STYLE`, one of `off` (**the default**) / `oriented` /
`concise` / `silent`. The latter three carry the same output-channel map — which of code
output, text content, and `ava.ui.notify` actually reaches the user is a fact about
the system, not a preference — and differ only in how much the agent says while
working. `off` is the exception: it is a true on/off gate, not a wording choice —
the whole section, channel map included, is omitted from the prompt. The retired
boolean `AVA_SYSTEM_PROMPT_PROGRESS` is still read as an alias: `false` → `silent`,
`true` → `oriented`; its literal `off` spelling is deliberately excluded from that
translation and instead reaches the enum's own `off` member unchanged, now the
stronger of the two meanings (see `AgentSettings._legacy_progress_bool_as_style`).

Intent: Ava already emits assistant text content alongside the `execute_code`
tool call, so it is naturally suited to interleave short "here's what I'm doing"
updates instead of working silently and only speaking at the end. `oriented`
encourages that — state the first step before a long exploration, surface
findings and direction changes as they happen, flag blockers. The other two
styles exist because that is a preference, not a universal: `concise` limits
speech to milestones (start / direction change / blocker / done), and `silent`
asks for no narration at all and one complete standalone report at the end
(blockers and user-only questions still interrupt immediately). Bench runs
`silent`; a supervised fleet agent runs `oriented`.

Deliberately **not** as prescriptive as Codex. We do **not** want:

- a fixed cadence ("a user update every 20s");
- banning specific opener phrasings ("Done —", "Got it", "Great question");
- mandatory acknowledge-the-request-then-restate ceremony before every action.

That level of micromanagement is unnecessary and brittle. A few sentences of
intent ("keep the user oriented with brief updates at meaningful moments; don't
narrate every thought") is enough; trust the model for the rest.

## Gap inventory vs Claude Code + Codex

Dimensions the upstream prompts cover that Ava's is silent on, classified.

### Genuine gaps worth considering (model won't reliably self-correct, AGENTS.md doesn't cover)

| Dimension | Lands in | Status |
|-----------|----------|--------|
| Progress narration / interleaved updates | Ava core (config-selected style) | ✅ `_communication_style_section` |
| Tone / candor toward the user (honesty over flattery, directness, no condescension) | Ava core (per-model family gradient) | ✅ `_user_tone_section` |
| Output conciseness + final-answer shape (don't dump files, reference paths) | Ava core | ✅ `_output_conciseness_section` |
| Verify-before-claiming-done (run the narrow test, then widen) | `ava_code` | ✅ `_coding_tools_section` preamble |
| Editing discipline (minimal focused diffs, comment-only-when-why, don't revert working-tree changes) | — | ❌ dropped — the model already self-applies these, and Ava rarely edits a human-authored working tree; not worth prompt weight |
| Faithful outcome reporting (say so if tests failed / a step was skipped) | Ava core | ✅ `_outcome_reporting_section` |
| Caution before irreversible / outward-facing actions; external-send = publishing | Ava core | ✅ `_action_caution_section` (the two combined) |
| Align before committing to a direction (confirm scope/approach before large work; user pins or defers the rhythm) | Ava core | ✅ `_align_before_action_section` |
| Malicious-code refusal / security gating | Ava core | ⬜ open — Ava is the user's private agent, not a public product; a refusal stance could get in the way. Deferred pending a threat-model call |

### Deliberately stripped / covered elsewhere (not real gaps)

- Git/PR protocol, scope discipline, doc discipline — live in `AGENTS.md`, loaded
  at runtime. Codex bakes an `AGENTS.md` *spec* (scope/precedence/nesting) into
  its prompt; Ava could add a one-liner about nesting precedence if it proves
  necessary, but the bulk stays in `AGENTS.md`.
- Long-term memory **mechanics**, skill catalog — [`philosophy.md`](../conventions/philosophy.md)
  lists memory as a removable layer; the mechanics stay in the `ava.memory` SDK
  docstrings, not the framework prompt. (The memory **behavior** layer — when /
  what to remember — did land in core as `memory_discipline_section`; see the
  mechanics-vs-behavior split above.)

### Capability-dependent (only if/when Ava exposes the capability)

- Frontend "avoid AI slop" design guidance, browser automation, sub-agent/team
  dispatch, security-refusal posture. Add to `ava_code` (or the relevant plugin)
  only when the matching capability is surfaced.

## Status / next

Landed so far:

- The `_engineering_workflow_section` move into `ava_code` (responsibility split).
- The core conversation section, since generalized from an on/off progress-narration
  toggle into a four-way communication style (`AVA_AGENT_COMMUNICATION_STYLE`:
  `off` (default) / `oriented` / `concise` / `silent`, with `off` omitting the section
  entirely).
- The core user-tone section (`AVA_SYSTEM_PROMPT_USER_TONE`, default on), with a
  per-family strength gradient and the Claude family defaulting off.
- The `ava_memory` behavior section (`AVA_SYSTEM_PROMPT_MEMORY`, default on) —
  the durable-knowledge "when / what to remember" layer.
- The merged `# Invest in the future` section
  (`AVA_SYSTEM_PROMPT_INVEST_FUTURE`, default on), replacing the independent
  Beyond section; `agent_reflection_enabled` is retired.
- The core output-shape section (`AVA_SYSTEM_PROMPT_CONCISENESS`, default on).
- A verify-before-done convention in the `ava_code` `_coding_tools_section`
  preamble. (Editing-discipline guidance was considered and dropped — the model
  self-applies it and Ava rarely edits a human working tree.)
- Core reporting-honesty (`AVA_SYSTEM_PROMPT_REPORTING`) and action-caution
  (`AVA_SYSTEM_PROMPT_CAUTION`, the irreversible-action + external-send-privacy gaps
  combined), both default on. Kept deliberately short — this guidance is likely
  already baked into the model, so the sections are a light nudge, not a spec.
- Core align-before-action (`AVA_SYSTEM_PROMPT_ALIGN`, default on) — confirm scope +
  approach before large / ambiguous / hard-to-redo work, especially right after
  exploring or planning; the user pins or defers the working rhythm. Distinct
  from action-caution: the direction of substantial work, not individual
  irreversible ops.
- Expanded SDK reference (`AVA_SDK_EXPAND`, default `*` = every top-level public
  namespace bar the capability surfaces) — full contracts after the overview.
- One skill index, a default-on match-first instruction, and a mandatory step
  that reads it: `# Capabilities` lists the whole catalog
  (`skills_to_inject_into_system_prompt` default `*`), the expanded SDK
  reference no longer renders a second copy, and
  `AVA_SYSTEM_PROMPT_CAPABILITIES_MATCH_FIRST` tells the agent to name the
  matching skills and why before acting. The first step of the delegation check
  (`AVA_SYSTEM_PROMPT_DELEGATION_CHECK`, default on) still requires matching the
  task against that index and loading the covering skill before acting.

- A one-sentence cross-machine delegation hint (`AVA_SYSTEM_PROMPT_CROSS_MACHINE_DELEGATION`,
  default on) rendered right after the delegation check: when work spans
  machines, spawn an agent on the target machine so it can use that machine's
  resources directly. Semantic steer only — no API detail, so it cannot go
  stale. User-finalized wording, shipped verbatim.
- A CodeAct batching section (`AVA_SYSTEM_PROMPT_CODEACT`, **off by default**):
  pack several operations into one `execute_code` call to save LLM API calls
  (user ruling 2026-08-26). Opt-in, unlike the ablation-toggled sections above.

Closed without building:

- `ava_code`-specific memory layer — **rejected.** Memory is the memory plugin's
  cross-cutting capability, not something a vertical scenario layers its own copy
  of; repo facts belong in that repo's `AGENTS.md`. See the responsibility-split
  section above for the full reasoning.

Remaining:

1. Malicious-code refusal / security stance — open, pending a threat-model call
   (Ava is a private agent, not a public product).
2. (minor) Load-bearing-whitespace nudge for agent-authored text — decorative
   blank lines in the docstrings / prompts the agent writes are a model style
   prior (most pronounced in the Claude family). Evidence says it is *not* worth
   prioritizing on cost grounds: self-measured token cost of all docstring blank
   lines is ~0.5%, and the literature finds formatting non-essential to model
   performance (sometimes a slight gain when stripped) — see [The Hidden Cost of
   Readability](https://xiaoningdu.github.io/assets/pdf/format.pdf). So this is a
   human-readability call, not a perf/token one: only add a "blank lines should
   be load-bearing" style nudge if a human reader is bothered; otherwise leave it.

## Ablation

Every behavioral section above is a `bool` config toggle (the `AVA_SYSTEM_PROMPT_*`
envs), default on. `bench-entrypoint.sh` turns the whole general-agent set off
(progress, reporting, caution, conciseness, align; memory auto-suppresses via the
SDK disable) so bench is a clean baseline with none of them — comparable to
historical runs. That is intentional: the toggles are the ablation knobs.
Whether each section actually helps is an open question — a proper A/B /
ablation design is TBD, but the on/off surface is already in place to run it.
