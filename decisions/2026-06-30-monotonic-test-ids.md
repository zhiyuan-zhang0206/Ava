# Monotonic test ids over per-test sequence reset

## Context

For weeks the CI backend suite carried a ghost flake: `tests/agent/test_claim.py`
would intermittently `assert 'allocated' == 'restarting'`, only under
`-n auto --dist worksteal`, never reproducible locally. A live diagnostic
(agent_events capture) proved the id-1 row genuinely held `'allocated'` on disk —
a fresh connection read the same value, so it was not an MVCC/visibility artifact.

A four-way audit of the test infrastructure found the cause was not one bug but
three independently-leaky axes that *compounded*:

1. **id reuse.** The autouse `_clean_state` truncated with `RESTART IDENTITY`, so
   every test's first `spawn_agent()` got id 1. Three incompatible id schemes
   coexisted (reset-to-1, a `900_000 + worker*10` reserved band, an e2e PID offset).
2. **lifecycle escape.** `asyncio_default_fixture_loop_scope = "function"` closes a
   test's event loop but does not cancel pending tasks; several `create_task` /
   thread / session paths were fire-and-forget.
3. **before-only reset.** `_clean_state` runs *before* each test, so it never
   quiesces the previous test — it just recreates the collision target.

Multiplied together: a background writer leaked from test N (the only `'allocated'`
writers are spawn / respawn / resurrect) lands on the *live* row that test N+1's
reset recreated at the same reused low id. Any one axis alone is survivable.

## Decision

Make serial ids **monotonic and never reused** within a worker-session:
`_clean_state` truncates *without* `RESTART IDENTITY`. A straggler write then lands
on a dead id, never a live one. The SDK self-identity, previously implicit ("the
first spawn is id 1, which equals the session-global `AGENT_ID = 1`"), becomes
explicit capture-and-pin: `ava._boot._agent_id = spawn_agent()`.

This is defense-in-depth against axis 2, not a fix that requires naming the exact
leaked writer — and it doubles as the experiment (if the flake dies, the
reused-id-collision theory is confirmed).

## Alternatives rejected

- **A generic teardown barrier that cancels all pending asyncio tasks + drains
  sessions + closes subscriptions (attack axis 2 directly).** Rejected as the primary
  fix: after monotonic ids a straggler is already harmless, and the DB-writing
  background surfaces are individually stubbed (`_guard_agent_launch` stubs the
  launch-confirm thread) or closed at source (SSE `pubsub.aclose`), and the session layer is
  handled by `_isolated_agent`'s poll-until-empty. Worse, an autouse *async*
  canceller runs pytest-asyncio's event-loop teardown even for **sync** tests,
  which breaks sync tests that monkeypatch loop-touching globals like
  `time.monotonic` (observed: a finite-iterator monkeypatch → `StopIteration`). High
  risk, ~zero remaining benefit.
- **Seed a fixed id-1 self-row every test, keep the sequence resetting.** Preserves
  the implicit `AGENT_ID = 1` idiom with near-zero test churn, but keeps the reuse
  hack alive (id 1 recycled every test) and pollutes count-based assertions with a
  phantom row. Rejected: it perpetuates the anti-pattern instead of removing it.
- **Per-test disjoint sequence offsets (extend the e2e PID-offset model to all
  tests).** Solves cross-worker collision, which per-worker databases already
  prevent — it does nothing for the *intra*-worker sequential reuse that is the
  actual flake.

## Consequences

- Tests may no longer assume a concrete id (`== 1`, `== (1, 2, 3)`). Self-identity
  tests capture-and-pin; sequential-allocation tests assert deltas. The migration
  touched ~35 `spawn_agent()  # placeholder` sites in `tests/ava` (matching the pattern
  already used in `tests/ava/test_ui.py`) plus one absolute-id assertion in
  `tests/agent/test_db.py`.
- The `900_000`-band (`tests/ava` sessions) and e2e PID-offset schemes stay — they are
  disjoint-and-non-recycling by design, not the reuse anti-pattern.
- The full contract lives in
  `docs/current/test-isolation.md` (since removed).
