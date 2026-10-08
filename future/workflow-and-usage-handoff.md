# Workflow and usage handoff

## Agreed direction

Workflow selects working methods independently: alignment, goal definition,
sustained supervision, peer collaboration, script orchestration, and evaluation.
Existing authorization lets the agent proceed; selecting a method does not add
an approval interview. All collaborators remain persistent peers.

Workflow guides describe cooperation in natural language and require no Fleet
SDK. Fleet tasks and labels are optional conveniences. Execution recipes own
concrete SDK calls; the core dynamic-workflow templates work without Fleet.

Costs are observed by agent IDs, windows, and birth lineage, never attributed to
tasks. A threshold reminder leaves disposition to the receiving agent, like a
compaction reminder: preserve results, converge, hand off, or revise the budget.
The observer script never kills peers.

## Contribution

Workflow selection PR (merged during this work): https://github.com/zhiyuan-zhang0206/Ava/pull/4342

Follow-up PR: https://github.com/zhiyuan-zhang0206/Ava/pull/4349

Development checkout: `.worktrees/workflow-selection`, branch
`codex/workflow-usage-reminders`. The follow-up is submitted for review; no merge
or deployment is performed by this contribution.

- Workflow selection and system-prompt routing: optional methods, useful skill
  selection, persistent peer roles, and existing consent.
- Fleet decoupling: remove forced notification/task creation from Align and
  label arguments from core script-orchestration recipes.
- Task-cost removal: remove budget/usage SDK and API fields, turn attribution,
  metering side effects, and stale generated schemas and guidance. Task notes
  still retain timeline links to their task records.
- Usage script: explicit IDs, self/spawn/fork/mixed birth ancestry, timezone-aware
  windows, lifetime ledger + event tail, per-model usage, missing-price counts,
  and optional one-shot threshold messages to named peers. Accounting logic
  moves from the Fleet reference script into `base/telemetry/metrics/usage.py`.
- Deliberate pause guidance: record partial artifacts, outstanding work, peer
  and watcher IDs, and the resume condition. Goal supervisors check this before
  nudging; orchestration scripts check before a new wave or script re-entry.
  Late notices and a restart are not authorization to resume.

## Follow-up boundaries

Retired task-cost database columns remain inactive. Drop them only in a later
migration after incompatible readers/writers are retired; do not rewrite the
merged task-budget migration or erase historical events.

An arbitrary window reads retained events. Lifetime totals retain the existing
folded-ledger path. Birth ancestry is not workflow membership or authorization:
a reused peer can have unrelated consumption. Fork ancestry follows the context
source, not the requesting caller. No new ownership registry is introduced.

Recorded costs cover metered LLM calls only. In-flight/unreported usage, unknown
prices, and external business expenses need independent evidence. Watchers are
ordinary bounded sessions and are never automatically restarted. Notification
failure is visible; partial delivery needs targeted recovery. The main agent
owns continuation and graceful stopping.

No live-agent evaluation has established adherence to the changed instructions.
No new workflow runtime, agent type, hard-budget controller, or production
lifecycle action is part of this contribution.

## Verification

Local validation after rebasing onto `95ca2cb6a`: 418 affected tests passed,
including 18 focused usage tests and the real read-only CLI query and
notification path. New observer code passes Pyright without errors. Commit and pre-push checks
passed, including frontend type checking and eslint. Full frontend Vitest was
skipped because frontend changes only remove generated task-schema fields; API
regressions and type checking verify their consumers. CI is tracked on the PR. Tests
cover real SQL lineage selection, windows, lifetime folding, unpriced calls,
notification-only thresholds, task contracts, claim/timeline links, LLM usage,
and affected prompt routing. Generated OpenAPI, frontend types, and event
registry are regenerated from their owners.

## Budget-handoff verification follow-up

The initial CI run found an omitted event-contract expectation for the retired
`llm_usage.task_id` field. The declared-key regression is corrected; the SDK note
docstring and feature inventory now distinguish timeline task links from usage
attribution.

Additional selected checks: 25 event-contract tests and 40 usage/watcher tests
passed. Two browser-free e2e cases passed on real isolated Postgres, Redis,
gateway, agent host and exec children. They run the observer as a separate
process, observe a spawn lineage before a fork exists, verify discovery and
actual metered usage, deliver a threshold message, and preserve partial results
without termination. Scripted goal supervision checks the saved pause before
nudging; a generated orchestration script checks it before a second wave. Both
preserve notes after a late checkpoint and a cold restart. These scripted
responses verify runtime composition, not a language model's independent choice
or adherence to the skills. The observer's own automatic recovery is not tested
or implemented. Scoped Pyright reports zero errors and warnings. A fault
injection that bypassed both saved-pause checks failed both cases: the goal
supervisor added peer model calls and the orchestration script consumed the next
unit and added another peer. The injected code was restored before submission.

Next priorities:

1. Run bounded live-model evaluations of both roles with adversarial late notices
   and explicit observed token limits. Preserve prompts, artifacts and actual
   usage; judge whether the agent chooses preservation and honors its own resume
   condition, rather than merely reporting compliance.
2. Help agents select an optional observation plan at work opening: existing
   authorization, IDs, lineage, window, available accounting coverage, recipient
   and watcher recovery notes. Do not infer task cost or create mandatory gates.
3. Introduce externally evidenced expenses only when a real source is chosen.
   Keep incurred cost, estimates and commitments distinct, with source identity
   and deduplication; an LLM usage report must not claim all business costs.
