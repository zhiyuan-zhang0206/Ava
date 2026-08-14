# Vet migration layout before an update stops anything (validate-before-kill)

> **Status 2026-08** — still in force; the one-time agent SDK entry
> (`ava.self.update()`, named as a `spawn_update` trigger below) was removed —
> the detached per-host `spawn_update` (watchdog self-heal) and the
> agent-runner self-update leg are the remaining triggers.

## Context

A self-update rolled prod onto an `origin/main` carrying two migrations numbered
`0049` (`event_log` from one PR, `agent_reports` from a sibling PR — each branched
off `0048`, each green in its own CI, the collision only existing once both
merged). The updater force-checked-out that main, killed every service, and *then*
failed the migrate pre-flight (`version 49 duplicated`) — leaving the whole cluster
down with no service left to recover it.

The migrate runs inside `ava start`, which runs *after* the stop. So the layout
error — a pure, static property of the checked-out tree (`_list_migration_files`,
no DB) — was only discovered after the blast had already landed. The kill happened
before the thing that could have refused it.

## Decision

Vet the rollout target's `migrations/` layout from git **before** any host is
paused, stopped, or checked out. `validate_migrations_at_ref(ref)` reads the
basenames at a git ref (`git ls-tree`, no checkout, no DB) and runs the same
duplicate / non-contiguous / bad-filename checks the on-disk loader runs. A broken
target is refused with every host still serving its current code.

Wired at the three points a rollout can take a host down, since they do not share
one chokepoint:
- **gateway orchestration** (`_vet_rollout_target`, after the target is pinned,
  before Phase A) — refuses the whole rollout, nothing paused;
- **detached `spawn_update`** (the prod path: `ava.self.update` / watchdog
  self-heal) — in-process fetch + vet before `pause_local_cluster`;
- **agent-runner self-update leg** — vets the checked-out tree before its stop, and
  reverts to the prior commit on a broken layout.

## Alternatives rejected

- **Catch it only in CI.** A per-PR lint cannot see a collision that exists only
  after two branches merge; the real prevention (merge queue / require-up-to-date)
  re-runs the *whole* test suite per merge — O(n²) with many PRs in flight. Worth
  doing as cheap prevention (a base-aware migration-number lint), but prevention
  alone still leaves a poisoned main able to take down the cluster. Containment is
  the load-bearing half: with validate-before-kill, a missed collision is a refused
  rollout, not an outage — so prevention is allowed to stay cheap and best-effort.
- **Validate inside `ava start` (where the migrate already is).** That is the
  existing behavior — too late, the stop already happened.
- **Pre-checkout vet using the running (old) code.** Cleanest "never even checkout"
  shape, but the validator must exist in the *old* code to run before the new code
  is on disk — a bootstrap gap for the rollout that introduces it. The agent-runner
  leg instead vets after checkout and reverts on failure; the gateway/spawn paths
  vet the target by git ref without checking out.

## Consequences

- A rollout to a structurally broken migration set now fails fast and idempotently
  with the cluster untouched, rather than mid-stop. A watchdog self-heal against a
  poisoned main loops on a logged refusal until main is fixed (benign) instead of
  bricking the host.
- The vet is a git read on every non-restart update — negligible, and it fails
  closed (an unreadable target ref is refused).
- `_list_migration_files` and the ref/name validators share one definition of the
  duplicate / version-jump rules (`_assert_contiguous_unique`), so the on-disk and
  pre-flight checks cannot drift.
