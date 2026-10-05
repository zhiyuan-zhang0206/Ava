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
  moves from the Fleet reference script into `base/telemetry/usage.py`.

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
