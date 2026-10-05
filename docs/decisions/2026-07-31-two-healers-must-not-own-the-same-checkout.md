# Two healers must not own the same checkout

## Context

On 2026-07-31 prod's Postgres was ahead of the commit `cluster_pin.target_sha` named.
(How it got there is #1071/#1073; this entry is about what happened next.) Two
controllers then held contradictory goals over one file — `~/.ava/source`'s HEAD — and
neither yielded:

- **schema controller**: "code must catch up to the DB" → spawn `ava update`, which
  force-checks-out `origin/main`.
- **pin controller**: "HEAD must equal `cluster_pin`" → force-check-out back to
  `1a90f95`, which lacks those migrations.

`git reflog`, six alternating resets in 111 minutes:

```
9b1343d2 HEAD@{04:45:15}: branch: Reset to origin/main
1a90f95d HEAD@{04:46:21}: branch: Reset to 1a90f95d…
09840c58 HEAD@{04:57:26}: branch: Reset to origin/main
1a90f95d HEAD@{05:16:34}: branch: Reset to 1a90f95d…
5647d8da HEAD@{05:27:39}: branch: Reset to origin/main
1a90f95d HEAD@{05:46:45}: branch: Reset to 1a90f95d…
```

Each `origin/main` leg's updater (`updater-1785498379.log` and siblings) ended:

```
→ apply pending migrations
applied 0 migration(s): []
→ verify schema version
  ✗ Schema ahead of code: DB has 5 migration(s) this checkout lacks: [...]
[session-exit] rc=1
```

which reads as absurd — that checkout *does* carry them. The 66-second gap between
each pair is the explanation: the pin controller's reset landed **while the updater
was still running**, between its `git checkout` and its trailing `ava start`, so the
start migrated and verified against a tree its own updater had not checked out.

Two amplifiers turned that into a two-hour outage rather than a puzzling log:

- The failing `ava start` returns at its schema-verify step, which is *before* the
  step that unlinks `cluster_paused`. So each failed leg left the flag set, and
  `ops.manager` reported `round blocked by pause, roster NOT fully reconciled` and
  revived nothing. `recover_stranded_pause` did lift it — after 600 s, by which time
  the next cycle re-paused.
- Nothing said why. Six identical `rc=1` rounds produced no line a human would read as
  an alarm; the outage was noticed by the frontend being down.

The issue names three constraints and explicitly does not propose a design.

## Decision

Three changes, one per constraint.

**1. Both writers of the checkout defer to any update in flight, not just a leased
one.** It
already declined while `update_lock_holder()` was set. That check is not wrong, it is
*blind*: a watchdog-spawned `spawn_update` — the schema controller's heal, this
controller's own local fallback, the code controller's restart — takes no lease at
all, which `ops/controllers/stalled_updater.py` states as a known property. So it now
also consults `ops.cluster.current_orchestration()`, exactly as the code controller
has since 2026-07-28. Being off-pin is a running update's *normal* mid-flight state,
and this removes the writer that could move HEAD underneath one.

The **schema** controller is the other half, and it was worse: it consulted *neither*
signal — the only acting controller that never asked whether a deploy was already
running — so it could fire an `ava update` into a rollout's Phase B, or into its own
previous updater. It now asks both (`_deploy_already_running`). An executing lease
scopes `DB_DEPENDENT` (nothing was spawned here); a local session scopes `ALL` (that
updater is about to replace every process). A **settle hold** is deliberately not a
deferral: nobody executes under one, and the convergence it waits for is what this heal
produces — issue #1020's argument in its narrowest form, `note IS NULL` only, chosen so
the two changes cannot deadlock each other in either merge order.

**2. A schema-ahead condition where the pin is the blocker escalates instead of
retrying.** `pin_is_the_blocker()` is one comparison: the `CodeBehindSchema` in hand
already proves *this checkout* lacks the DB's migrations, so if HEAD is also the
cluster pin, the pin is what lacks them. In that state the arm logs ERROR (which
`shared/log.py` routes to `agent_events`) naming both remedies, spawns nothing, and
returns its reason on `ReconcileResult.detail` so the manager's escalating
blocked-round line says which pin is behind rather than only "schema". It is checked
*ahead* of the backoff, because a backoff would turn the flap into one checkout
rewrite per half hour rather than end it — which is what the incident's 30-minute
cadence actually was. It is also checked **ahead of the deferral above**: both return
without spawning, so the order decides only which reason the operator gets, and an
updater was running for much of the incident's window — a deferral placed first would
have masked the terminal verdict behind a transient one, reproducing the very silence
the escalation exists to end.

This is the change that terminates the loop. Change 1 alone does not: the update would
complete, HEAD would move to `origin/main`, and the next pin round — with nothing in
flight — would pull it back, restoring the drift at a slower cadence.

**3. Who owns a pause is a two-signal question.** `recover_stranded_pause` asked only
the lease, so "nobody holds the lease" and "nothing is executing" were the same
reading, and the bound had to be long enough to cover a local self-update that might be
quietly working. It now asks the lease **and** `current_orchestration()`.

An **owned** pause is still declined outright — unchanged, and deliberately so: the
shortened bound must not become a way to unpause a host a rollout is working on. Those
states end on their own (a crashed holder stops being a *live* lease at its TTL; a hung
session is killed by the reaper that runs ahead of this controller), so both resolve
into the unowned case rather than needing a second timer. An **unowned** pause is now
the only kind the timer sees, so `STRANDED_PAUSE_TIMEOUT_S` drops from 600 s to 120 s —
sized only to outlast the gap inside `spawn_update` between `pause_local_cluster()` and
the session spawn a few statements later. That is the state a failed updater
leaves, and it costs two minutes of a blocked roster instead of ten.

This also dissolves an adjacent disagreement rather than papering over it:
`SETTLE_TTL_S` (900 s) exceeded the old 600 s bound, so a host paused under a settle
hold hit `ops.manager`'s ERROR escalation while still refusing to self-unpause. A settle
hold is a live lease, hence simply an owned pause, hence declined — the mismatch was in
the logic, not in the constants.

**4. Two heal-record defects, found in passing.** `check_pin_drift`'s bare-`Exception`
branch around the local spawn recorded no heal attempt at all, so that path armed no
backoff and retried at the cooldown cadence forever — the shape PR #879 removed from
the success-only record, surviving in the one branch it did not reach.

And `_heal_record.record_attempt` contradicted its own contract: it read the previous
`consecutive_failures` carry-over *outside* the `not ok` branch, so a success that
followed a failure wrote `ok=True` beside `consecutive_failures=1` — a record saying
the host both healed and is one round into being stuck. Nothing in the product branches
on the field (it is read by operator surfaces), so it misreported rather than
misbehaved; the module docstring, `schema.py`'s, the block-scope OKF's "counts rounds
that could not heal", and `update_trigger`'s in-process sibling counter all already
said a success resets it. Found by writing the module's first tests, which it had none
of despite three controllers depending on it.

An unreadable signal returns a placeholder owner, never None: an unpause taken on
missing evidence is the one mistake this function must not make.

## Alternatives rejected

**Auto-advance the pin when the DB is ahead of it.** It converges without a human and
it is the wrong trade by a wide margin: the pin is the cluster's statement of which
reviewed commit it runs, and letting a schema-ahead condition move it would make any
route to a drifted DB — including the one that caused this incident — a mechanism for
silently deploying unreviewed code. Escalating leaves the cluster stably behind and
loudly so, which is recoverable; the alternative is not.

**Have watchdog-spawned updates take the cluster update lease.** This is the tempting
one, because it would fix constraint 1 by making the existing check see everything, and
because "one intentionally-mid-transition signal" is the principle
`shared/cluster_lock.py` is built on. Rejected on lifecycle: the lease stays valid
because the *orchestration process* renews it (`renew_update_lock`, driven from the
Phase-B poll) and stops renewing when it dies. A detached `ava-updater` is a shell
pipeline with no such process; it would take a lease nobody renews and nobody reliably
releases, so a crashed updater would block every deploy for a full `LOCK_TTL_S` and a
slow one would lose its protection mid-flight — the exact coupling
`shared/deploy_timing.py` exists to remove. `current_orchestration()` is the signal
that already has the right lifetime, because the session *is* the process.

**Give the pin controller a longer backoff, or a lock file against the schema
controller.** Rate-limits a livelock instead of ending it. Six laps in 111 minutes was
already the backoff working as designed; the two goals still contradict, so the only
question a longer window changes is how long each lap takes.

**Order the controllers so pin runs before schema (or drop one).** The order is
load-bearing and already correct — `pin` before `code` because the checkout must be
right before the processes are judged, `schema` before `pin` because old-code daemons
crash on a new schema. Reordering would trade this livelock for a different one, and
each controller's dimension is genuinely its own.

**Detect the livelock by counting reflog flips.** Diagnoses the symptom, and needs a
threshold nobody can justify. `HEAD == pin` under `CodeBehindSchema` is the *state*,
available on the first round rather than the sixth, from facts both controllers
already read.

**Clear `cluster_paused` from the failing `ava start` itself.** The most direct
reading of constraint 3, and it is unsafe: the flag exists so the restarter cannot
respawn an agent onto a checkout that is mid-transition, and a start that fails its
schema check is precisely a checkout that cannot run. Unpausing there hands the host
back to a restarter that will respawn agents doomed to crash. Recovering the pause from
the controller instead keeps every other gate in front of the revive — the schema
controller still blocks the DB's users — so what comes back is the services that can
actually serve.

## Consequences

- **The cluster no longer converges on its own out of this state, by design.** It
  parks with HEAD on the pin, the DB ahead, an ERROR per round, and DB-dependent
  services held down until a human advances the pin (`shared.cluster_pin.advance_pin`,
  or a rollout that succeeds) or rolls the schema back
  (`shared.migrations.rollback_to`). That is an outage — a stable, named, single-cause
  one instead of a flapping checkout.
- **`pause`, `pin` and `code` now all defer to the local orchestration session, so all
  three sit behind the stalled-updater reaper.** The reaper's first position was
  already load-bearing; it is more so now, and the cost is that a hung session it
  cannot kill stalls three dimensions instead of one. That case already logs ERROR with
  a session-kill remedy. `ops/manager.py`'s ordering docstring and the ordering
  test both record the changed reason.
- An `ops` daemon on a host where `current_orchestration()` cannot be read (no session backend)
  will not pin-heal, will not schema-heal, and will not recover a stranded pause. All
  three fail closed, which is the right direction, and every host that runs the ops
  daemon runs its services in sessions.
- **A per-round ERROR is invisible to an operator who is not tailing logs.** The parked
  state is only *reachable* here; making it *visible* is the last-update-failed
  surfacing in #1110 and the health-probe alert path. The two compose into "parked, and
  the operator can see it"; this change alone gives the first half.
- `schema_reconcile()` returns `(BlockScope, detail)` rather than a bare scope. One
  caller; the detail is what makes the manager's existing escalation say something an
  operator can act on.

---

**Superseded in part (2026-07-31):** the paragraph above holding that a settle hold is
"a live lease, hence simply an owned pause, hence declined" was wrong about the one
lease shape it names. Nobody is coming back from a settle hold, and one naming *this*
host is the orchestration's own record that this pause lost its owner — so the
disagreement with `SETTLE_TTL_S` was relabelled rather than dissolved, and the host
still waited out the full settle window. See
[a settle hold does not own the pause it waits on](2026-07-31-a-settle-hold-does-not-own-the-pause-it-waits-on.md)
(issue #1116). Everything else here stands, including the two-signal rule and the
120 s bound, which that change reuses unaltered.
