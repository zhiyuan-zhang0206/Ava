# A cluster's schema may only be migrated by its own gateway unit

## Context

`ava start` step 2.5 applied pending migrations unconditionally, on every role.
An agent-runner does not own a data plane — its `AVA_DB_URL` points at the
gateway's central Postgres — so a runner whose checkout ran ahead of the
gateway's migrated the shared prod database.

That is what wedged prod on 2026-07-31 (issue #1059). Five migrations landed on
main between 00:59 and 02:13 PDT; the gateway stayed pinned at `1a90f95`, which
carried none of them; another unit reached the same DB and applied all five.
From then on the gateway failed its own startup `assert_schema_current` with
`CodeBehindSchema` and rejected every agent boot, and its watchdog's pin-driven
force-checkout kept re-asserting the old commit against the new schema.

The step's own comment showed the assumption that broke: it reasoned only about
a runner being *behind* the gateway ("`apply_pending_migrations` writes nothing —
the file isn't in this checkout's `migrations/`"), never about one running ahead.

A second path reaches the same place. Every agent process inherits
`AVA_HOME=~/.ava` from the prod session environment, and `resolve_ava_home()` ranks
that env var above the checkout's own `.ava_home` pointer. A bare `ava start` in
a dev worktree therefore resolves the *prod* home — while `MIGRATIONS_DIR`, being
anchored to `__file__`, still resolves the *worktree's* migration files.

## Decision

`apply_pending_migrations` refuses unless the executing checkout is the gateway
unit of the cluster whose database it is about to migrate.

The DB's identity is read from `machine_units` — the rows `register_self`
already writes at `ava start`, filtered to `serve_gateway`. The executing side
claims `(machine_name(), checkout_anchored_home())`. Both must match.

Two properties of that claim carry the design:

- It uses `checkout_anchored_home()`, a new sibling of `resolve_ava_home()` that
  applies the same rules **minus** the `AVA_HOME` override. The env var says
  which cluster a process was launched by; the checkout says which cluster its
  code belongs to. They agree everywhere except the poisoned-inheritance case,
  which is precisely the case that must be caught — a worktree with an inherited
  `AVA_HOME` has a prod DB URL *and* a prod-looking `ava_home()`, so nothing else
  distinguishes it.
- An unanchored checkout (a dev tree with no `.ava_home`) is refused outright.
  Its `~/.ava` fallback *looks* like the gateway's home but is not a claim of
  ownership.

The check runs only when there is actually something to apply. An agent-runner's
ordinary `ava start` calls this against the central DB and legitimately applies
nothing; the refusal fires exactly when a non-gateway checkout would write.

Exemptions fall out of the same rule rather than being special-cased: a fresh
birth has no `machine_units` rows yet (step 2.5 migrates before step 3
registers), so an empty result means "no owner yet", not "not you"; a restore
from dump carries the cluster's own rows and matches; the gateway's own
`ava update` migrate step matches.

## Alternatives rejected

**Compare the checkout's resolved `AVA_HOME` against the cluster's home path.**
The first shape proposed, and wrong for split deployments: a WSL runner, a macOS runner and a Windows runner have homes `/home/ava/.ava`,
`/Users/ava/.ava` and `C:\Users\ava\.ava` yet legitimately share one
central DB. Home equality would refuse every runner on every path, including the
ones that are fine. It also fails open on the worktree case, since an inherited
`AVA_HOME` makes the resolved home *equal* the gateway's.

**Gate on `machine_role()` — refuse when this host lacks the `gateway`
capability.** Cheap, and it would have stopped the runner. But it reads the
executing host's own env, which is exactly what was wrong in the worktree case:
a fleet agent on the gateway host *is* on a gateway-capable host, so the check passes
while the worktree's migrations still land on prod. Authority has to be
established against the database, not asserted by the caller.

**Record a new cluster-identity row (a `cluster_identity` table, or a home-path
column on `cluster_pin`) at provision time.** Rejected as invented storage:
`machine_units` already records exactly `(machine_name, home, serve_gateway)`
and is already written on every `ava start`. A second identity store would need
its own backfill and could drift from the first.

**Leave the runner's apply in place and only fix the pin/watchdog flapping.**
Treats the symptom. The flapping made the outage loud, but the schema was
already mutated by then; any host sharing the DB could do it again through a
different trigger.

## Consequences

- A runner that has genuinely run ahead now fails its `ava start` instead of
  migrating. That is the intended trade: it fails loudly on the host that is
  wrong, without touching the cluster everyone else depends on. The operator
  path is unchanged — `ava update` on the gateway.
- `apply_pending_migrations` now depends on `shared.machine` and
  `shared.dotenv_boot`, so a caller must have a resolvable machine name once the
  DB carries identity. A checkout with no machine name configured is reported as
  `<unset>` in the refusal rather than raising `MachineNameMissing` underneath.
- The guard is invisible on a fresh/bench database, which is what keeps
  testcontainer runs and first-birth installs working — but it also means an
  empty `machine_units` grants authority to anyone. That is acceptable only
  because an unprovisioned DB has nothing to strand.
- `resolve_ava_home()` keeps its `AVA_HOME`-first precedence. Narrowing it was
  considered out of scope here: too many launch paths depend on the env var
  winning, and the migration guard no longer needs it to.

---

Forward: the narrowing deferred in the last consequence above was taken up in
[2026-07-31-ava-home-vs-checkout-contradiction.md](2026-07-31-ava-home-vs-checkout-contradiction.md),
which enumerates the dependent launch paths and refuses only the contradiction.
Nothing decided here changed.
