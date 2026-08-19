# 0002 — A "DB is down" test that patches one seam is testing nothing

**Date:** 2026-07-29
**Anchors:** PR #938, the restarter healthcheck (pre-cutover; the PR is not
reachable from public `main`). Surviving code:
`tests/services/test_healthcheck_restarter.py` (the `no_db` / `dead_db`
fixtures), `tests/conftest.py:_provisioned_db`, `tests/_containers.py`,
`shared/db.py:connect` and `shared/db.py:pool`.

## Summary

Two regression tests were written to prove that the restarter's healthcheck no
longer reaches the database on its alive and revived paths. They faked "the DB is
down" by monkeypatching the single `shared.db` helper the *new* code calls. Ava's
suite provisions a real throwaway Postgres per session, so every other route to
that database stayed live — and the deleted implementation had reached it through
the *other* helper, `connect`, which ran happily against an empty `agents_meta`.
Both tests would have gone green against the exact bug they were written to catch,
and would have shipped permanently unable to fail. The fix is two-part and both
parts are now in the repo: patch every route rather than the one in fashion, and
prove the test red by stashing the implementation before believing it green.

## Timeline

PR #938 removed the restarter healthcheck's dependency on the database: the
daemon's own `RespawnController` owns the dispatch, so a connection on the alive
or revived-successfully path is a regression toward coupling that had just been
removed. Two tests were added to lock that in.

The fixtures patched `shared.db.pool` — the helper the new code path uses — and
the tests passed. They were reviewed and were on their way to merge.

The check that caught it was the ordinary one: prove the test fails without the
fix. Stashing the implementation and re-running did not turn either test red. The
removed code, `_catchup_dispatch`, had used `shared.db.connect`, an untouched
door straight to the session's live throwaway Postgres. `agents_meta` was empty in
that database, so the catch-up query returned nothing and succeeded. Nothing
raised, nothing failed, and the test reported that the DB had not been touched.

## Root cause

`tests/conftest.py` provisions a **real** throwaway Postgres per session
(`_provisioned_db`, via `tests/_containers.py`), by design — the suite exercises
real SQL against a real server rather than a mock. The consequence is that in this
suite, absence of a database is never the ambient condition. It is something a
test has to manufacture, and it is only as complete as the set of doors it closes.

`shared/db.py` exposes two doors: `connect` (a single connection) and `pool` (the
pooled path). A fixture that names one has not made a statement about the
database. It has made a statement about which helper the code under test happened
to pick — and that is an implementation detail that moves under it.

The escape analysis:

- **The tests themselves.** They passed. That was the problem: a green result from
  a test that cannot go red is indistinguishable from a green result that means
  something, and nothing in the output tells them apart.
- **Coverage.** The lines executed. Coverage counts execution, not the ability of
  an assertion to fail.
- **Review.** `monkeypatch.setattr(shared.db, "pool", ...)` beside a change that
  uses `pool` reads as exactly right. Recognizing the gap requires knowing both
  that the deleted code used `connect` and that the suite has a live server behind
  it — two facts that sit in different files from the diff.
- **The `--ignore`-a-mock instinct.** In a suite that mocked the database
  wholesale, this class does not exist; the failure mode is specific to a suite
  that provisions real infrastructure, which is otherwise the better choice.

## Guardrails added

- **The fixtures close every door, and say why.**
  `tests/services/test_healthcheck_restarter.py` now carries `no_db` (any DB
  access fails the test) and `dead_db` (any DB access raises), each patching
  **both** `connect` and `pool`. The docstrings state the reason in place:
  "the DB is down" has to be a fact about the DB, not about which helper the code
  under test picked — and naming only today's helper would let the next
  implementation reintroduce the coupling through the other door.
- **CI cannot reach a real data plane by accident.** `.github/workflows/ci.yml`
  points `AVA_DB_URL` / `AVA_REDIS_URL` at unreachable sentinel ports on purpose,
  so a test that quietly dials a real data plane fails with a connection error
  instead of passing quietly. That is the same defense one level up: it makes an
  unintended live connection loud rather than green.
- **The rule, written down**: the `prove a guard red` and `a dead dependency is a
  fact about the dependency` entries in
  [`conventions/defensive-patterns.md`](../conventions/defensive-patterns.md), and
  the "prove it red" step in
  [`.agents/skills/run-local-tests/SKILL.md`](../.agents/skills/run-local-tests/SKILL.md).

**Still unguarded, deliberately.** No lint can tell a complete dependency-outage
fixture from a partial one; the set of doors is different per dependency and the
check is a reading. The enforcement is the red-before step, which costs one stash
and one re-run.

## Lessons

- **A test asserting behavior under a dead dependency must kill every route to
  it**, not the route the current implementation happens to take. Otherwise the
  test silently retargets itself the next time the implementation changes seam.
- **Prove it red.** `git stash push <implementation file>`, re-run, confirm the
  specific test fails, `git stash pop`. A test that passes against both the old
  and the new implementation is not testing what its name says.
- **A suite that provisions real infrastructure inverts the default.** Mocked
  suites fail closed — an unmocked call blows up. Real-infrastructure suites fail
  *open*: an unmocked call succeeds against an empty table. Reason about absence
  explicitly, because the environment will not do it for you.
- **An empty table is a successful query.** "It returned nothing" and "it could
  not reach the database" are different facts, and only one of them is what a
  DB-down test means to assert.
