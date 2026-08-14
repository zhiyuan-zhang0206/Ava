# A settle hold does not own the pause it is waiting on

**Issue:** #1116. **Related:** #1020 (the same lossy read, in the pin/code
controllers), #1074 (pause ownership is two signals), #1098 (rejected), #1115 (what
landed for #1074).

## The claim

`ops/controllers/stranded_pause.py` treated **any** live deploy lease as an owner of
this host's pause. One lease shape is not: a **settle hold naming this host**. That
hold is the orchestration's own written record that this host's pause has *lost* its
owner, and the controller was reading it back as proof that the pause *has* one.

The lease half of `_pause_owner()` now asks `DeployLease.awaits(machine_name())`
instead of "is it held" — the same discrimination #1020 gave the pin and code
controllers. Nothing else changes: the local-session signal still runs, and an
un-owned pause still serves the full `STRANDED_PAUSE_TIMEOUT_S`.

## Why the previous reasoning was wrong, not merely incomplete

#1115 closed #1074 by making `_pause_owner()` two signals and declining an **owned**
pause outright, on a stated justification: *a live lease or a live session means
someone is coming back.* It also recorded, as a resolved side effect, that
`SETTLE_TTL_S` (900 s) used to exceed the old 600 s bound — "a settle hold is a live
lease, so it is now simply an owned pause and declines; the disagreement was in the
logic, not the constants."

That is where the error is. For a settle hold, **nobody is coming back** — that is the
definition of the state. `SETTLE_TTL_S`'s own docstring says so: *"Nobody is executing
during a settle hold — it is a stated waiting period."* So #1115 did not remove the
900 s disagreement; it relabelled the host's 900 s wait from *"a timer that has not
expired"* to *"a pause that is owned"*, which reads as correct and is not. The
observable behaviour — a paused host refusing to self-unpause for the full settle
window while `ops.manager`'s ERROR escalation fires at ten rounds — was unchanged.

## The contradiction loop, exactly

The settle hold is not independent evidence of ownership. It is *derived from* the
absence of an owner:

1. Phase A pauses agent-runner X. Phase B triggers X's self-update; X acks.
2. X's updater dies — SIGKILL, killed session, machine crash — before the `ava start`
   that unlinks the flag.
3. The gateway polls X. `cli/commands/update.py:_probe_verdict` mints `POLL_STALLED`
   from `paused=true` **and** `current_orchestration=null`, confirmed
   `_STALL_CONFIRMATIONS` times. That predicate is `_pause_owner()`'s own unowned
   reading, evaluated remotely.
4. `_still_converging` folds `POLL_STALLED` hosts into the hold, so
   `settle_update_lock(hosts=[X])` records *"X stopped, and its checkout moved"*.
5. X's watchdog reads that row and concludes its pause is owned.

The gateway proved nobody is coming back for X's pause and wrote the proof down; X
read the proof as its refutation. The stronger the gateway's evidence, the longer X
stays stuck. A host whose updater dies is not an exotic corner of this path — per
step 4 it is the path's *designed destination*.

## Why the pause controller, and not somewhere downstream

#1020's exception already exists in `ops/controllers/pin.py` and
`ops/controllers/code.py`, and it is the right one. It was **unreachable** in this
shape: `PauseController` returns `BlockScope.ALL`, `ops/manager.py` short-circuits on
the first blocker, and `pause` runs ahead of `schema`, `pin` and `code`. So the one
host a settle hold waits for was the one host forbidden to run the controllers that
converge it.

That closes the loop on the release side too. `ops/deploy_window.py`'s
`settle_hosts_converged` re-probes the held hosts for `head_sha == pin` **and**
`running_sha == pin`. Neither can move on a host whose heals never run, so the early
release — the mechanism that exists precisely so a hold does not idle out its timer —
could never fire on this path. The hold lapsed on `SETTLE_TTL_S` every time.

Unpausing is not itself convergence, and this change does not claim it is. It removes
the gate in front of the controllers that do converge; they then take their own
#1020 exception on the following round, and `settle_hosts_converged` observes the
result and releases the hold early.

## What was deliberately *not* changed

**The bound stays a bound.** An awaited hold changes *who owns* the pause, never *how
long* an unowned one waits. `STRANDED_PAUSE_TIMEOUT_S` exists to outlast the gap
inside `ops.cluster.spawn_update` between `pause_local_cluster()` and the
the session spawn a few statements later — a gap this host can enter at any moment,
settle hold or not, and during which `current_orchestration()` is still `None`.
Treating an awaited hold as a licence to unpause *immediately* would reintroduce that
race for one lease shape and split one rule into two. Rejected.

**The local-session signal is untouched.** Only the lease half narrows. A
watchdog-spawned `ava-updater` takes no lease at all, so a settle hold says nothing
whatever about it.

**A crashed *executing* lease still owns the pause, up to `LOCK_TTL_S`.** A killed
rollout session leaves a `note IS NULL` lease that stops being renewed; `awaits()` is
False for it and this change does not touch it. That is correct and not a gap we can
close here: the holder is `<other-machine>:pid<N>`, and `ops/ops_cluster.py`'s
`_lock_holder_is_live` can only probe a pid on the machine it runs on. From X there is
no way to distinguish a crashed remote rollout from a working one, which is what the
TTL is for; `ava cluster recover`, run on the holder's machine, is the operator escape.
A settle hold is discriminable only because its `note` is *self-describing*.

**The round that unpauses still returns `BlockScope.ALL`.** The heals land one 60 s
tick later. Against a 900 s deadlock that is not the problem, and changing it would
alter the gate's contract for every pause, not just this one.

## Why #1098-style start/restart-clearing does not return

The Race-2 panel rejected #1098 — clearing `cluster_paused` at `cmd_start` /
`cmd_restart` failure points via `_release_self_heal_pause` — on three defects. None
of them reappears here, and the reason is the same in each case: **this change is made
by an out-of-process observer, not by the failing process.**

- **(a) Its discriminator was lease-only, blind to a watchdog-spawned updater.** This
  change touches *only* the lease half of `_pause_owner()`. The
  `current_orchestration()` half is exactly what an awaited hold now falls through
  *to*, so the watchdog-spawned updater — the #1074 blind spot — is seen precisely as
  before. `test_a_settle_hold_naming_this_host_still_defers_to_a_live_local_updater`
  pins it, and a mutation that lets an awaited hold answer "unowned" on its own turns
  that test red.

- **(b) `_RESTART_RECOVERY_SH` means the pause would be cleared while the
  ava-updater session is still alive with its recovery start pending.** The clearing
  decision here is made by the watchdog, whose second signal *is* that session — a
  live `ava-updater` makes `current_orchestration()` answer `"update"` and the
  unpause is declined. A process cannot see its own successor; an observer can. (For
  the record: both surviving `_release_self_heal_pause` calls are narrower than the
  defect describes — `cmd_restart` and the agent-runner self-update each call it only
  on a preflight refusal, returning `RESTART_DECLINED_EXIT_CODE`, which is the one
  branch of `_RESTART_RECOVERY_SH` that starts nothing afterwards. The defect stands
  against #1098's proposal, which added clearing at `cmd_start` failure points — and
  those *do* land in the recovery branch.)

- **(c) It covered only in-process failure; SIGKILL / killed rollout session /
  machine crash were untouched.** Those three are this change's entire subject
  matter. They are how the flag outlives its owner in the first place, and — via
  `POLL_STALLED` — how the settle hold that strands it comes to be written.

## The other lossy `update_lock_holder()` readers

#1116 asked for an audit rather than a blind conversion. Six call sites remain; one is
load-bearing-but-deferred and four are correct as they are.

| Call site | Verdict |
|---|---|
| `cli/commands/update.py`, `cli/commands/_cluster_rollback.py` | **Correct.** Diagnostic strings printed *after* `acquire_update_lock` already returned False. The refusal is the DB compare-and-set, not this read, and a settle hold must go on refusing a second deploy (the 2026-07-29 incident). |
| `cli/commands/_cluster_recover.py` | **Correct.** Display only; the gate is `cluster_recover_op`. An operator breaking a hold must be shown *any* hold. |
| `ops/ops_cluster.py` (`cluster_recover_op`) | **Correct, and not lossy.** It pairs the holder with `_lock_holder_is_live` (a pid probe) plus `current_orchestration()` — a strictly stronger discriminator than the note. A settle hold's holder is a dead pid, so a local operator already clears it at once. |
| `cli/commands/status.py` (`_update_in_flight`) | **Correct enough, advisory.** It downgrades pin-drift remedy hints during a deploy window. Under a hold naming this host the hint it suppresses ("this host has not converged yet") is the true reading anyway. Not a gate. |
| `cli/commands/stop.py` (`_release_self_heal_pause`) | **Lossy in the same way; deliberately left.** See below. |

`_release_self_heal_pause` is the in-process fast path that clears a self-heal's own
pause after a declined restart, and its discriminator is the same lossy holder read —
so under a hold naming this host it leaves the host paused. Converting it is
directionally right and is *not* done here, because it is not an independent gate: its
own docstring names the stranded-pause controller as its backstop. Fixing the backstop
is what makes leaving it alone affordable — that fallback now costs 120 s rather than
a full settle window. Changing pause-clearing at an in-process failure point is
exactly the move #1098's adjudication warns about, and it carries liveness
invariants (the updater session is alive and exiting at that instant, so simply adding
the `current_orchestration()` signal there would stop it firing at all) that deserve
their own change rather than a rider on this one. Its stale "10-minute
stranded-pause recovery" reference is corrected in passing.

## Alternatives rejected

1. **Close #1116 as already fixed by #1115.** The legitimate outcome the brief allows,
   and the evidence refutes it: on current `main`, with a settle hold naming this
   host, `recover_stranded_pause()` returns False at any age. The composed test file
   demonstrates the round blocking on `pause` twice over.
2. **Give an awaited hold an immediate unpause (no timer).** Rejected above — it
   reintroduces the `spawn_update` race and splits one bound into two.
3. **Shorten `SETTLE_TTL_S`, or order it under the pause bound.** This was a disagreement
   in the logic, not in the constants — #1115 said so and was right about that much.
   Tuning the timer leaves a host whose pause is provably ownerless waiting on a timer
   at all, which is the thing being removed.
4. **Move `PauseController` behind `pin`/`code` so their #1020 exception can fire.**
   Inverts a load-bearing order: a paused host must not have its services revived, and
   `pin`/`code` spawn updates. It would fix this shape by breaking the gate's purpose.
5. **Have the gateway clear the flag when it mints a `POLL_STALLED` verdict.** Puts
   the write on the machine that just proved it cannot reach the host reliably, and
   re-centralizes per-machine file state the pause deliberately keeps local
   (`ops/cluster_pause.py`). The host is the only party that can see its own
   `current_orchestration()`.
6. **A second, longer timer for "owned" pauses.** Restores the shape #1115 removed —
   two bounds where the question is really "who owns this", and a number that must be
   audited against every lease TTL.
