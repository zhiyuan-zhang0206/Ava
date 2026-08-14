# Self-rolling release via canary-and-holdout

## Context

Ava needs to upgrade its own running fleet to new code on `main` without a
human babysitting every rollout. The hard problem is failure: if the new code
is broken, *something* must still be running the old, known-good code to detect
the regression and revert it. A naive "upgrade everything, then check" loses the
agent that would perform the rollback the moment the new code crashes it.

Constraints:
- Schema migrations run mid-rollout, so old and new code briefly coexist.
- "Healthy" is not binary — some signals are unambiguous failures, others are
  only degradations that need a human's call.
- Cron-driven rollout must survive the HTTP gateway dying; a dead gateway must
  not silently stop all scheduled releases.

## Decision

**Canary + holdout.** Worker agents upgrade first and run under observation
(the canary). One Release Agent stays on the *old* code as the holdout and is
the sole executor of rollback. Only after the canary is confirmed healthy does
the Release Agent restart itself onto the new code.

Rollout sequence: optionally notify workers → terminate all workers → confirm
shutdown (timeout escalates to a human) → run additive schema migration → pull
new code → resurrect workers on new code → monitor → on health, Release Agent
self-restarts; on failure, revert to the old commit and re-resurrect workers.

**Health monitoring is two-tier.** Hard indicators (unrequested crash,
ERROR/CRITICAL exception, stuck agent, whole-fleet silence) trigger immediate
rollback unconditionally — never softened by baseline. Soft indicators (warning
rate, cycle duration, concurrency) are compared against a baseline collected
from old-code runtime *before* the upgrade; meaningful degradation escalates to
manual confirmation rather than auto-reverting. Thin baseline data lowers
confidence but does not force a prompt.

**Monitoring carries no new machinery.** The monitoring period is a plain
polling loop in the Release Agent's own code — poll status, poll error logs,
`while + sleep + dict`. No new SDK surface, DB tables, or modules.

**Triggers, in descending priority:** manual (a human tells the Release Agent to
roll; skips all gates) > scheduled cron > threshold (N important PRs
accumulated). Cron splits into a read-only `check` tier (fetch + count
unapplied commits, no side effects) and an `apply` tier that runs the full
rollout behind three gates: busy guard (a release already running), time-window
gate, and confirmation gate. Cron is off by default.

**Scheduling is infrastructure, not an agent feature.** Cron dispatch lives in
an independent scheduler daemon (sub-second poll), decoupled from the HTTP
gateway. An OS-level cron keep-alive restarts the daemon if it dies and backfills
overdue schedules. The Release Agent and any monitor agents are *applications*
sitting on top of this shared scheduler.

Invariants: the Release Agent runs old code until the final self-restart; schema
changes are additive-only (add column/table, never drop/rename) so old and new
code coexist safely; workers reach `main` only via worktree + PR, never direct
push; the Release Agent orchestrates and never writes business code itself.

## Alternatives rejected

- **Upgrade the whole fleet at once, then verify.** Rejected: the agent that
  would roll back is itself running the suspect code. A crash takes out the
  rollback executor. The holdout exists precisely to keep one known-good
  executor outside the blast radius.
- **Single health verdict (auto-rollback on any anomaly).** Rejected:
  conflates outright failure with mild degradation. Reverting on every elevated
  warning rate makes rollout flap on noise; ignoring degradation hides real
  regressions. Splitting into hard (auto-revert) and soft (escalate) keeps both
  decisive on failure and human-gated on ambiguity.
- **Absolute thresholds for soft indicators.** Rejected: an agent fleet has no
  universal "normal" warning rate or cycle time. Judgments are made against a
  freshly captured pre-upgrade baseline, so the comparison is against *this*
  fleet's recent behavior, not a guessed constant.
- **Destructive (drop/rename) migrations during rollout.** Rejected: old and
  new code coexist while the fleet is being swapped and during a rollback.
  Non-additive schema changes break one side. Additive-only keeps the schema
  forward-compatible across the coexistence window.
- **Run cron inside the gateway.** Rejected: the gateway is an HTTP server; if
  it dies, every scheduled release dies with it and nothing notices. An
  independent daemon plus an OS-cron keep-alive makes scheduling a system-level
  guarantee, not a side effect of the API process being up.
- **Build dedicated monitoring infrastructure (SDK/tables/modules).** Rejected
  for the monitoring period: it is a temporary, bounded observation loop.
  Existing status and error-log queries in a plain loop suffice; new machinery
  would be standing infrastructure for a transient job.

## Consequences

- Rollback is reliable because a known-good executor always survives the
  upgrade — at the cost of the Release Agent lagging one version behind the
  fleet until it confirms health and restarts itself.
- Migrations are permanently constrained to additive-only within a single
  rollout. Lossy changes must be decomposed (expand now, contract in a later
  rollout) so any one upgrade stays reversible.
- Soft-indicator escalation means some rollouts pause for a human; full
  unattended rollout is reserved for clean hard-and-soft passes.
- Scheduling becomes a first-class subsystem with its own liveness story
  (daemon + keep-alive + backfill), decoupled from the gateway — more moving
  parts, but cron survives gateway outages.
- The Release Agent is strictly an orchestrator. Keeping it out of business code
  preserves the holdout's role and the worktree-plus-PR path to `main`.
