# Migration timestamp IDs + schema re-baseline

2026-07-19. Replaced sequential-integer migration IDs (`0001_…`) with
second-precision timestamp IDs (`YYYYMMDDTHHMMSS_<kebab-name>.sql`), moved DB
tracking from a high-water integer to an **applied set** keyed by name, and
squashed the `0001..0081` history into `db/schema.sql` as a new baseline — all in
one PR.

## Problem: sequential integers force a coordinating counter

The `NNNN_` scheme requires every new migration to claim "the next number". That
is a shared mutable counter with no owner, so parallel branches collide by
construction. Three incidents:

- **0060** (`develop_main_0060_divergence`): a revert deleted a migration file,
  leaving `develop` at 59 while prod was at 60 — a divergence guaranteed to break
  the next release.
- **0062** (`prod_rollout_0062_duplicate_migration`): a cherry-pick reintroduced a
  duplicate `0062`, so a rollout's pre-flight refused the target and the cluster
  silently did not update.
- **0080**: two branches each took `0080`. Critically, `git rebase` merged both
  **without a conflict** — the files have different names (`0080_a` vs `0080_b`
  only if the names differ; when they share intent the trees still merge clean) —
  so nothing in the normal review/merge path caught it. Only `lint_migrations.py`'s
  bespoke cross-branch collision check (added after 0049) turned it red.

The lint check is a patch over a structural defect: the numbering namespace is
centralized, so every branch contends for it, and git — which is content-merging,
not counter-aware — cannot see the contention.

## Decision: timestamp names + applied-set tracking

- **Timestamp IDs** move the namespace from a shared counter to wall-clock. Two
  migrations authored on different branches get different names for free; a merge
  never has to renumber. Names sort ≈ chronologically, which is the only ordering
  the runner needs.
- **Applied set** (`schema_migrations` keyed by `name TEXT PRIMARY KEY`, not
  `version INT` high-water): apply = "run every file whose name is not in the
  set". An out-of-order merge (an older-timestamp migration merged after a
  newer-timestamp one is already applied) is handled by definition — each name is
  tracked independently, so there is no contiguity invariant to violate. The
  equality check every daemon runs at boot becomes set equality (both directions:
  DB-behind → `SchemaVersionMismatch`, DB-ahead → `CodeBehindSchema`), preserving
  the strict fail-fast contract.

### Rejected alternatives

- **Renumber bot / merge-queue that reassigns numbers on merge.** Keeps the
  central counter and adds a bot to arbitrate it — more machinery to defend the
  defect instead of removing it. A bot outage reintroduces the collision.
- **Django-style "merge migration".** When two heads exist, author a third
  migration that depends on both. This is the DAG model; it works, but it makes
  every parallel-branch merge a manual authoring step and needs a dependency graph
  the runner must topologically sort. Timestamps give collision-freedom without a
  DAG.
- **CI-gate only (keep integers, harden the lint).** This is what we had. The
  gate is necessary but not sufficient: it catches the collision *after* both
  branches are green and one is about to poison main, and it depends on a
  network-reachable `origin/main` at lint time. It defends the defect rather than
  removing it.

## Why re-baseline in the same PR

Converting live DBs from the integer high-water to the name set needs a rule for
what names an already-migrated DB "has applied". A prod DB at version 81 has
applied `{1..81}` — but those integers have no timestamp names (the old files
were `0001_…`). Migrating 81 historical rows into name-keyed rows would mean
inventing 81 synthetic names and backfilling them everywhere.

Squashing removes the problem instead of solving it. `db/schema.sql` is already
the full current schema; making it the **baseline** means the entire `0001..0081`
history collapses to **one** sentinel row (`00000000T000000_baseline`). A live DB
at exactly `{1..81}` converts to `{baseline}` in one step; a fresh DB stamps the
same sentinel from schema.sql. So the applied-set migration carries no history —
which is why the two changes ship together, not sequentially.

The baseline is also the **rollback floor**: it has no `.down.sql`, and a rollback
whose diff would remove it is refused (`RollbackBelowFloor`). This carries the old
`_DOWN_FLOOR=22` concept forward — "migrations at/below the floor are
irreversible" becomes "the squashed baseline is irreversible", with every
post-baseline timestamp migration required to ship a `.down.sql`.

## Cutover mechanics + the stepping-stone rule

`shared.migrations._ensure_cutover` (run inside `apply_pending_migrations`, under
the schema-mutation lock) is the one-way conversion:

- **legacy `version INT` at exactly `{1..81}`** → drop the table, recreate it
  name-keyed, stamp the baseline sentinel. Idempotent: a new-format or absent
  table is a no-op.
- **any other legacy state** (behind 81, or gaps) → `CutoverRefused`. Such a DB
  must first upgrade to the release immediately preceding this one (the last with
  sequential-integer migrations, schema version 81), which brings it to the full
  `{1..81}` baseline, and only then to this release. This is the DB **stepping
  stone**.
- A read path (a daemon's boot check) that meets an unconverted legacy table
  raises `CutoverRequired` rather than silently converting — a read must never
  mutate; it waits for the gateway's `ava update` to run the conversion.

## Deployment consequence (this PR is not `ava update`-deployable)

The running **old** code's rollout pre-flight (`_vet_rollout_target` →
`validate_migrations_at_ref`) validates the *target* commit's `migrations/` under
the *old* rules: it rejects an empty dir ("no migration files found") and rejects
timestamp names ("does not match `NNNN_`"). The cutover commit's `migrations/` is
exactly that — empty of integer files. So a normal `ava update` onto this commit
is refused by the code already in production, before anything is paused.

Therefore this cutover deploys **manually**, bypassing the old vet: on the gateway
(and each runner), `git fetch && git checkout <cutover-sha> && uv sync && ava
start`. The new code's `ava start` runs `apply_pending_migrations`, which converts
the DB via `_ensure_cutover` and then applies zero post-baseline deltas. Prod is a
single cluster (a macOS gateway + WSL runner on the shared DB), so the DB converts
once (whichever runs `apply` first) and the other sees the new format. This is a
one-time operational step, documented here because the "step through the
preceding release" rule applies to the **code** rollout path, not only the DB.

## Operational rule: migration files reach the DB only through git

A migration file **must be committed to git before it can ever be applied** —
the loader (`shared.migrations._list_migration_files`) applies only files git
tracks; an untracked `.sql` file in `migrations/` is warned about and skipped,
and the loader refuses to enumerate at all when `migrations/` is not inside a
git worktree (2026-08-07 incident: a migration written straight into the running
checkout's `migrations/` without a commit was auto-applied by the watchdog's
self-heal, wedging the cluster; fixed by the git-tracking gate, Task #998).

So the only sanctioned path for a new migration is: write it in a worktree,
commit it, land it via PR, and let the next rollout's `apply_pending_migrations`
execute it. Never copy a migration file into the running checkout's
`migrations/` — it is inert there (and was, before the gate, dangerously live).

## What this leaves for later

Nothing structural. Post-baseline migrations accrue under `migrations/` as normal;
if history ever grows unwieldy again, re-baselining is now a known, cheap move
(squash into schema.sql, stamp a fresh baseline sentinel, bump the legacy-cutover
constant — though after this PR there are no more legacy integer DBs to convert).
