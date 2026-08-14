# R1-R4 design invariants — the concept layer a PR can violate

The repo's design concepts (okf/design/) define invariants that are
NOT mechanically linted. A diff that violates one is a contract break even
when every test is green. Check the change against each invariant it could
touch; the full documents are the authority — this is the quick reference.

## R1 — State & Liveness (r1-state-liveness.ava.okf.md)

- **State vs liveness separation**: `deployment_state` / `host_deploy_state`
  hold deployment posture; agent liveness = `status ∈ {starting, running,
  idling}` AND unexpired lease. No new signal that mixes the two (no new
  flag files, no reading log mtimes as state — that is a documented
  anti-pattern).
- **Single status machine**: new `UPDATE agents_meta SET status` write
  sites are suspect; transitions must be CAS-guarded (`WHERE status =
  expected`) and stamped with `termination_source` so reapers/resurrectors
  can classify.
- **Alive predicate has one answer**: if a change makes liveness answerable
  two ways (lease vs pid identity), it violates the model.
- **Watchers get a registry**: no new fire-and-forget watcher/session
  without a registry entry and a rebuild path.
- **Event stream records facts only**: no new code that treats the event
  table as a state register.

## R2 — Single Source of Truth (r2-single-source-of-truth.ava.okf.md)

- **One declaration registry per fact family**: env keys go through
  `shared/env_registry.py` (no raw `os.environ` — linted, but no new
  hand-maintained parallel sets); events through `shared/events/contract.py`;
  retry through `shared/resilience.py`.
- **The only retry loop**: `shared/resilience.py` claims to be it. New
  retry loops, new jitter formulas, new transient-classifier sets are
  violations (D1).
- **One idempotency mechanism**: two idempotency tables/implementations is
  a known open debt — do not add a third.
- **schema.sql is the truth**: any migration must be reflected in
  `db/schema.sql` in the same PR; the baseline is the rollback floor.
- **Derived views are generated, not hand-synced**: new hand-copied
  constants that duplicate a registry (provider keys, retention days,
  resilience params) are violations.

## R3 — Boundary Contracts (r3-boundary-contracts.ava.okf.md)

Every cross-process boundary is a door with a doorplate declaring:
idempotency, pause exemption, and truth source. Five doors: API calls,
pause exemption, pages, notices, skills.

- New HTTP endpoints: idempotency declared? pause-exempt or pause-aware?
  auth enforced?
- New cross-process calls (gateway → runner, daemon → DB/Redis/IM):
  what happens during a rollout pause / migration window?
- Pages: host/port registration must not create an SSRF surface; truth
  source is the `agent_pages` table, not agent self-report.
- Notices: one open-notice semantics; no new parallel pipes.
- Skills: new skill sources must not bypass the scan (project skills
  bypassing `skill_scan` is a documented open risk — do not widen it).

## R4 — Frontend Projection (r4-frontend-projection.ava.okf.md)

- The frontend is a projection of one server truth line: HTTP snapshot as
  baseline + SSE as deltas, reconciled by the fold layer. New per-hook
  hand-rolled snapshot×SSE reconciliation is a violation.
- Layout invariants need test defense (jsdom cannot see flex/min-width
  bugs — the two P0 layout failures were engine-level).
- Panel data contracts are declared once; no per-panel bespoke contracts.
- Defaults must match user rulings (inspector_open = false; no panel
  covering the timeline on entry).

## Standing user rulings (check every PR against these)

- Keep It Simple — the concept-simple option wins over the literally-simple
  one; no new frameworks where an existing primitive suffices.
- IM is the only Telegram frontend; no direct bot API calls outside
  im_bridge.
- UI changes go straight to main (preview cluster paused); OKF must be
  updated in the same PR.
- Skill content in English; Chinese only in notes and user-facing chat.
- Reports are delivered via `ava.ui.serve` HTML, never email.
- Everything in a worktree + PR; direct push forbidden; merge only through
  the queue with genuinely green CI.
