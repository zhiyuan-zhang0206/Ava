# Billing batch resurrect: one explicit, audited operator entry

## Context

On 2026-09-18 a DeepSeek balance exhaustion stopped the fleet in one blow: the
provider answered HTTP 402, `classify_error` classified the rejection as
PERMANENT, the recovery breaker halted automatic recovery after two consecutive
rejections, and the corpse reaper terminated the rows — 23 agents in all. The
only recovery was `ava agents resurrect <id>` per victim, a 5h43m manual loop
with no enumeration, no whitelist, no health gate, and no audit.

Two standing constraints bound the fix:

- The post-stoppage recovery trigger is an explicit human action (the parent
  line's ruling): a balance endpoint answering again is not an observation
  that the fleet is ready, and an automatic fleet-scale revive would race the
  account state that caused the halt.
- The closure fence (#3911) and the ordinary terminated semantics stay
  untouched: a closed agent is never auto-reopened, and `termination_source`
  `user` / `integrity` rows are deliberate deaths.

The gap is narrow: make the *explicit* action one operation — enumerate the
victims, verify the provider recovered, resurrect them, and leave an audit —
not automate the trigger.

## Decision

An explicitly triggered batch-resurrect entry, dry-run by default:

- **Surface.** `ava agents resurrect-billing` (CLI; `--execute` to act) over
  `POST /api/agents/resurrect-billing` — `execute=false` is a strictly
  read-only preview.
- **Whitelist.** `status='terminated'` + `closed_at IS NULL` +
  `permanent_reject_streak >= HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS` +
  `last_permanent_reject_reason = 'billing'` +
  `COALESCE(termination_source,'') NOT IN ('user','integrity')`. The reason
  column is new (`agents_meta.last_permanent_reject_reason`): written with the
  streak increment, cleared with it by the completed-turn reset, never
  backfilled by guess.
- **Balance gate (fail-closed).** Execution re-checks the provider balance
  endpoint (`/user/balance`; the account's largest `balance_infos` total) and
  refuses below `billing_recovery_min_balance` (default 1.0); any transport /
  HTTP / payload surprise, or a missing key, refuses the run, with the readout
  in the response. The preview shows the same readout.
- **Report-only survey.** Billing-halted rows that are still alive (idling /
  running) ride every response's `halted_alive` list — visibility only, never
  actioned; they park heartbeats and clear the halt on their next successful
  turn, and releasing their hold is a follow-up candidate, not part of this
  entry.
- **Idempotency & concurrency.** The per-agent flip is the existing row-locked
  `terminated -> idling` CAS (exactly one spawn); the whitelist self-clears
  after a run; a run-level advisory lock refuses (does not queue) a concurrent
  second run.
- **Per-machine dispatch.** One new versioned home action
  `resurrect-billing-v1`; under the metadata row lock it re-adjudicates
  terminated / not-closed / billing-halt and refuses otherwise, never clearing
  `closed_at`. Old runners reject the unknown path — fail-closed.
- **Audit.** One run-level `billing_resurrect` audit event (balance readout +
  the candidate / resurrected / refused / deferred / failed sets) plus the
  `billing_resurrect_run` telemetry event; each resurrected agent's own
  `resurrect` event carries `via='billing_recovery'`.

## Alternatives rejected

- **Automatic batch resurrect when the balance recovers.** Violates the
  explicit-human ruling; a fleet-scale automatic revive races the provider
  state that caused the halt, and a balance readout is not a fleet
  willingness signal. No auto path is added.
- **A script looping `ava agents resurrect <id>`.** No enumeration, no
  whitelist, no balance gate, no audit — the manual cascade the outage
  exposed; it also cannot separate billing victims from user-finalized rows.
- **Reusing `resurrect-explicit-v2` with a flag.** Embedding a new mutating
  semantic into an existing action version is fail-open against older
  runners (they would execute under the old semantics); a new versioned path
  is rejected wholesale by runners that do not know it — fail-closed by
  construction.
- **Per-agent CAS only, no run-level lock.** Concurrent duplicate runs would
  double-dispatch the same cohort and stack confusing audits; refusing the
  second run is the honest behavior.
- **A hold-release arm for alive-halted rows (in v1).** Replaced by the
  report-only survey; the arm stays a follow-up candidate the review
  explicitly kept out of v1.

## Consequences

- Operations gets one command: top up -> preview -> `--execute`; a rerun is
  an audited no-op. The first live use rides the next provider stoppage, or a
  staged drill.
- Pre-existing victims carry `last_permanent_reject_reason = NULL` and stay
  outside the whitelist; the 2026-09-18 rows need an approved single-row data
  repair (evidence in the incident's event stream) — never a guess-filled
  backfill. Whether to resurrect those rows is a separate explicit decision.
- All failure surfaces are fail-closed: a balance-endpoint drift refuses (fix
  via `AVA_BILLING_RECOVERY_*`); an unreachable home machine yields a
  per-agent `failed` and a rerun converges; a deferred exit settlement stays
  with the existing resurrection-retry machinery. Rollback = stop calling the
  entry (the migration's paired down migration drops the column).
- The per-agent `ava agents resurrect` contract is unchanged and remains the
  manual rescue path for user-terminated and closed rows.
