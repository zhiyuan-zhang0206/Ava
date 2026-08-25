---
name: adversarial-review
description: Hunts for correctness, security, contract, growth, and fake-green defects in Ava PRs. Use before any PR enters the merge queue, even when ordinary review and CI are already clean.
---

# Adversarial Review

Every PR merged into `main` must pass an adversarial review before it is
enqueued. This is a hunting exercise, not a walkthrough: assume the diff is
wrong and try to prove it. The compliance pass (`auto-review`) asks "does
this obey the rules?" — this skill asks "**what breaks, and when?**" Its
dimensions subsume auto-review's; where they overlap, this pass is the gate.

## The stance

- **Assume the diff is defective.** Every new line is guilty until traced.
- **Prefer the failure path.** The happy path is what tests exercise; the
  failure path is where Ava's incidents live (lost messages, killed agents,
  fake green, silent rollbacks). Read each change for what happens when what
  it depends on is unavailable, slow, restarted, or wrong.
- **Never accept a comment as evidence.** Comments lie — this repo has a
  documented class of "comment says fail-safe, code fails open". Verify
  behavior from code.
- **Severity discipline.** P0 = production risk (data loss / leak / outage /
  silent failure). P1 = clear bug, contract break, resource leak. P2 =
  hygiene. Don't inflate to be heard, don't deflate to be nice; an empty
  report on a clean diff is a good report. Never manufacture findings.

## Pre-flight: verification chain first

Before reading the diff, prove the PR's claimed verification is real — a
review of an unverified PR is theater. Full procedure:
`references/verification-chain.md`. Minimum:

1. `gh pr checks <n>` → dig into run durations and artifacts, not the check
   box. A full backend suite finishing in seconds is the documented
   fake-green signature (2026-08-06); backend-parallel genuinely takes
   >= 4 min.
2. Proof-of-work present: junitxml / artifacts / coverage gate parsed a
   real number. `NO_WORKFLOW_RUNS` and skipped checks are not green.
3. If CI is suspect or does not cover the touched layer, run the narrow
   local verification yourself and cite it in the report.

## Review dimensions

Walk all eight. Each finding records **file:line + evidence + why it
matters + suggested fix**. Match the diff against
`references/pattern-library.md` (defect classes this repo has shipped) and
`references/r1-r4-invariants.md` (design invariants a change can violate).

### D1 Correctness & data flow

- Trace every changed value to its consumers — what do they assume
  (ordering, uniqueness, nullability, scale)?
- Numeric fields compared as strings (`"9.5" > "10.1"` is true — the IM
  watermark P0). Any `>`/`<` on ids, versions, watermarks, cursors must be
  numeric or padded.
- Watermark/cursor/offset advances only on confirmed success — advancing on
  failure silently drops data.
- Idempotency: retried operations must not double-execute non-idempotent
  effects; keys stored and replayable; keys never permanently bricked.
- At-least-once paths: where is the ack, where is the retry, what happens at
  the edge of the retry window?
- State transitions CAS-guarded (`WHERE status = expected`); no new state
  nothing can recover from.

### D2 Boundary & polarity

- Fail-open vs fail-closed: when the check itself errors, which way does it
  fall? "Unknown" must never be reported as "OK" (the `_probe_verdict`
  class: a DB read failure returned POLL_OK and released a deploy lease).
  Silence is not convergence.
- Legacy aliases: a rename that flips semantics must map the VALUE, not
  just the key (`AVA_SKIP_AUTH=true` → `auth_middleware_enabled=True`).
- Condition polarity on new gates: `always()` chains with empty outputs,
  allow-lists accepting `skipped`, fail-safe comments whose condition does
  the opposite.
- Empty / null / oversized / negative inputs: zero rows, null fields, 1 GB
  body, negative delay, empty list.
- Scale assumptions: "at most a few dozen rows" ages into a lie. Does the
  code degrade linearly, quadratically, or catastrophically at 10x data?

### D3 Contracts

- Events: new events declare an EventSpec (category, payload TypedDict,
  retention) and match what the emitter writes; consumers read declared
  keys; retention has one source of truth.
- Schema vs migrations: new migrations reflected in `db/schema.sql` in the
  same PR; lossy operations expand-contract with a paired `.down.sql` whose
  preconditions still hold (a down that silently drops live data is P0).
- API: changed endpoints — every consumer updated in the same PR (frontend
  types, tests, services; the main-red-after-merge class).
- Process boundaries (R3 doorplates): idempotency declared, pause exemption
  correct, single truth source. `restart_required` must match the consumer's
  process.

### D4 Error handling & silent failure

- Swallowed exceptions: top-level `except Exception` that logs and
  continues — what does the caller believe happened? A caught exception
  that still advances state is data loss.
- Retry loops converge on `shared/resilience.py`; new hand-rolled loops
  with divergent backoff/jitter/classifier are violations.
- Async called as sync: a never-awaited coroutine silently never runs (the
  redis `disconnect()` class).
- Observer code must not crash or amplify: logging/telemetry that raises,
  blocks the hot path, or dials the DB exactly when the DB is down.
- New lifecycle code: crash mid-transition, restart, pid reuse, supervisor
  death (orphans)? Non-resurrectable termination sources? Rows stuck
  `status NULL` forever? Resources released in `finally`?

### D5 Security

- No credentials in the repo — ever (P0). Hardcoded DB URLs/tokens in
  tracked files; check the whole diff and history of modified files.
- Credential files `0o600` at creation — not chmod-after (window), not
  umask-dependent.
- New endpoints/surfaces: auth, size limits, quota, content-type checks,
  SSRF surface (host:port proxying), header leaks into logs.
- Prompt-injection surfaces: new skills, frontmatter, or other text injected
  into the system prompt — scanned? Untrusted content rendered as code/HTML?

### D6 Performance & unbounded growth

- New append-only stores need a retention or cleanup owner (36 GB
  checkpoint bloat — 95% held by terminated agents).
- N+1 / cartesian joins / missing LIMIT on list endpoints.
- Full reads where projections suffice; indexes not matching query order;
  per-request expensive construction.
- Uploads/bodies without caps; unbounded queues; unbounded in-memory reads.

### D7 Test quality & fake-green

- The new test fails on the pre-change code? A test green by editing
  fixtures/assertions to match broken output is a defect.
- Fake-green channels: `|| true`, flaky groups allowed to fail, skips on
  missing deps, coverage sets drifting from CI's, gates with fallbacks below
  the real threshold, e2e without proof-of-work.
- Tests that can reach the real world: Telegram, os_cron/launchd, migration
  application, prod `.env` leakage through the shell. Non-pytest scripts
  bypass conftest guards.
- The behavior change is locked by a test asserting the new behavior;
  boundary conditions covered (empty, null, oversized, concurrent,
  transient).

### D8 Concept alignment & discipline

- R1: state/liveness separation, lease-based liveness, single status
  machine, watcher registry, events record facts only.
- R2: single-source registries (EnvRegistry / EventSpec / resilience) — no
  duplicated constants, retry loops, or idempotency mechanisms; schema.sql
  is the truth.
- R3: every cross-process boundary declares idempotency / pause exemption /
  truth source; no new unregistered doors.
- R4: frontend is a projection of one server truth; no per-hook SSE
  reconciliation; defaults match user rulings.
- User rulings: IM is the only frontend; UI goes straight to main; Keep It
  Simple (concept-simple wins); skill content in English; docs in the same
  commit.
- Discipline: worktree-only changes; migrations only via worktree+PR, never
  written into the runtime `~/.ava/source/migrations/`; no new dependencies
  without need.

### D9 OKF documentation sync

Docs must track code — a feature that lands without a co-located
`.ava.okf.md` update is an invisible feature (the zombie-escalation class:
merged 2026-08-10, user ruled it out because no OKF record existed).
Check, for every functional PR (feat / refactor / behavior change):

- [ ] Does the diff include or update the co-located `.ava.okf.md` for the
      touched domain (`<dir>.ava.okf.md` next to the code it describes, or
      `okf/<domain>.ava.okf.md` for the domain overviews)?
- [ ] If not: is an exemption explicitly declared in the PR description?
      Legitimate exemptions only:
      - pure bug fix (no behavior / contract change)
      - documentation-only PR
      - skill addition (the SKILL.md is its own documentation)
      - pure internal implementation (no observable behavior)
      - tests / CI-internal mechanism
- [ ] A functional change merged with no OKF update within 7 days is a
      documentation gap by default — flag it even when the PR itself was
      exempt, because the exemption covers the change, not the drift.

The OKF graph is the only cross-cutting map of the system; a stale graph
is a stale understanding. When in doubt, require the doc.

## Adversarial techniques

When the dimensions yield nothing, attack with these moves:

1. **Impact-surface tracing.** `grep` every renamed/moved symbol and changed
   endpoint across the whole repo, tests included. Renames/relocations are
   the recurring regression class — no compiler sees string paths, daemon
   module paths, or `parents[N]` indices.
2. **Reverse scenario.** For each new behavior, write the scenario in which
   it is wrong: DB down, gateway restarted mid-request, network blip, two
   instances racing, process killed between claim and store, message during
   rollout. Does it survive?
3. **Data derivation.** Scale 10x, empty, invert (negative, descending,
   non-ASCII, very long); run comparisons mentally at boundary values.
4. **History probe.** Match the change against the pattern library — each
   class there shipped once; a structurally similar change is suspect until
   proven otherwise.
5. **Regression lock.** Behavior change covered by a test that fails on old
   code? If not, that is a P1 finding on any behavior change.

## Output format

Post as a PR comment (`gh pr comment <n> --body "..."`) or send to the
delegator. Every finding cites file:line; no location, no finding.

```
## Adversarial Review (Ava #<id>)

### Verdict
APPROVE / REQUEST CHANGES — <one sentence: change + overall risk>

### P0 — blocks merge, escalate
- <issue> — <file:line> — <evidence> — <suggested fix>

### P1 — blocks merge
- <issue> — <file:line> — <evidence> — <suggested fix>

### P2 — recommended
- <issue> — <file:line> — <suggestion>

### Verified clean
- <dimensions attacked and found clean> (silence is checked, not skipped)
```

## Process integration

In `ship-a-change` terms: **CI genuinely green → adversarial review → P0/P1
resolved (or user waiver) → enqueue → Mergify lands it.**

- **Who**: the repo steward, or a dedicated reviewer agent the delegator
  names. Never the PR author — reviewing your own diff is self-confirmation.
- **When**: PR open + CI green, before `@mergifyio queue` /
  `ci_utils.py --wait --merge`. Never enqueue a PR without an adversarial
  pass or an explicit user waiver.
- **Loop**: findings → author fixes or rebuts with evidence → reviewer
  re-checks only changed hunks and rebutted findings → enqueue.
- **Escalate**: any P0 also goes to the user via `ava.ui.notify` (P1) so it
  is not lost in the PR thread.
- **Record**: verdict line on the PR (`Adversarial Review: APPROVE @ <sha>`)
  so the merge trail shows the gate passed.

## Honesty rules

1. A clean verdict after genuine hunting beats a padded report; false
   positives train authors to ignore the gate.
2. Could not verify something (CI suspect, no time, tool missing)? Say so —
   an unverified pass is a fake green of its own.
3. Author rebuts a P0/P1: re-check the code, not the argument. The code is
   the evidence.
