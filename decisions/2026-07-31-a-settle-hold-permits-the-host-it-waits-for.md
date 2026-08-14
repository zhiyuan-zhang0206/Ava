# A settle hold permits the host it is waiting for

## Context

`cluster_update_lock` is the cluster's single "intentionally mid-transition" signal,
and every automated healer defers to it. That is right for the reading it was built
for — while a rollout is executing, "checkout ahead of the processes" is the deploy's
own transient and healing it would fight Phase B.

A **settle hold** is the same row in a different state. When an orchestration exits
with hosts that acked their self-update and never came back, it calls
`settle_update_lock` instead of releasing: the lease stays held on `SETTLE_TTL_S`,
with `note` recording *which hosts* it waits for. Nobody is executing under it —
`SETTLE_TTL_S`'s own docstring says so — and its content is precisely "host X has not
converged".

The healers could not tell the two apart. On 2026-07-30's second rollout (pin
1a90f95d) a runner stalled mid self-update on a network flap: its checkout reached
the pin, its processes stayed on 7e571b49. The rollout took a settle hold naming it:

```
holder='gateway:pid65237'  note='settling, waiting for: a runner'
acquired 22:28:22   expires 22:44:32
```

and every 60 s the Air's `CodeController` — the one healer whose whole dimension is
"HEAD is the pin but the processes are not" — logged that it was deferring *because
that lease exists*:

```
[ops.code] on-pin 1a90f95d… but running 7e571b49…; a cluster update is in
progress (held by gateway:pid65237) — that update is what replaces these
processes, deferring
```

The hold named the very host whose convergence it blocked. It could only expire.
~16 minutes of a mixed-code host, and on a deployment where the settle window is
renewed rather than left to lapse it would not have cleared at all.

## Decision

The lease's `note` is what tells a healer which instruction it is receiving, and
`DeployLease.awaits(machine)` is the reading: **True only when the lease is a settle
hold whose recorded waiting set names that machine.** `ops.controllers.code` and
`ops.controllers.pin` consult it in place of `update_lock_holder() is not None`, so an
executing lease still defers and a settle hold naming this host proceeds.

Both, not just the one the incident happened to exercise. `update_lock_holder()` is a
**lossy read** — it collapses "an orchestration is executing" and "a 900-second stated
waiting period, nobody executing" into one truthy holder — and every healer that
consumes it inherits that loss. `settle_hosts_converged` will not release the hold
until the named host reports `head_sha == pin` **and** `running_sha == pin`, so the
checkout dimension (pin) and the process dimension (code) are *both* things the hold is
waiting for. Fixing only the dimension the Air happened to be stuck in would leave the
identical deadlock one incident away, in a controller whose deferral the same argument
condemns.

The note was already a machine-readable contract with one builder and one parser
(`settle_note` / `settle_hosts`), written that way so the release path could re-probe
exactly the population the hold was taken over. This adds a second consumer of the
same fact rather than a second source of truth for it — which is the whole reason
there is no separate "deploy in progress" flag.

Nothing else changes. The hold is not released, shortened, or renewed; a second `ava
update` is refused exactly as before; auto-rollback stays suppressed; and no host but
the named one is permitted anything. The heal keeps every other guard it had —
agent-runner only, prod-source only, no local orchestration session, the persistent
per-commit backoff, the shared process cooldown. `ops.deploy_window` then ends the
hold on its own terms, because the restart produces the `running_sha == pin` that
`settle_hosts_converged` is looking for.

## Alternatives rejected

**Extend the stalled-updater reaper to count as the update's local completion agent**
— the issue's second candidate: detect "checkout moved, updater dead, processes
stale" and treat it as the update finishing. It re-derives, in a controller that owns
session liveness, a state the code controller already owns and already detects
correctly. The code controller's reading was never wrong; only its deferral was. It
would also leave the deferral in place for every future healer that hits the same
hold.

**Release the settle hold when it names only hosts that are still trying.** It gives
back exactly the protection the hold exists for. The hold's job is to keep a second
deploy out of a half-transitioned cluster (2026-07-29, two agents force-terminated for
nothing); "the host is working on it" is when that is most true, not least.

**Have the settle hold carry a separate "healing permitted" column instead of reading
the note.** A second field describing the same fact is free to disagree with the
first, which is the bug class `cluster_update_lock` was made singular to close. The
note already answers the question, and `settle_hosts` already refuses to guess at one
it cannot parse.

**Let the permission apply to any healer on any host during a settle hold.** The hold
names the hosts it is waiting for; a host it does not name is not what it is waiting
for, and permitting one would let an unrelated box restart itself inside a window a
deploy is still responsible for.

**Shorten `SETTLE_TTL_S`.** It treats the symptom and breaks the constant's meaning:
it is deliberately the family's one no-progress definition, shared with the Phase-B
poll and the host-local stall reaper, and cutting it re-opens the clock disagreement
`shared.deploy_timing` exists to remove.

## Consequences

- A host named by a settle hold now converges in a watchdog round (~60 s) rather than
  on the hold's TTL, and — the part the TTL could never fix — it converges even when
  the window is renewed.
- The permission is a *narrowing of a deferral*, so its blast radius is bounded by
  what the two controllers could already do on their own host: the code controller's
  `spawn_update(restart_only=True)`, which bounces this host's services on a checkout
  that is already correct, and the pin controller's `spawn_update(target_sha=pin)`,
  which force-checks-out the commit the cluster has already pinned. Neither advances
  the pin — advancing it is a step of a *successful* rollout — and neither reaches
  another host. Each does exactly what this host would have done when the hold
  lapsed, and does it sooner.
- The **stranded-pause controller** keeps the unconditional lease deferral, and that
  asymmetry is deliberate rather than an omission. Pin and code *converge* a host;
  unpausing merely stops holding its services down, which is not what the hold is
  waiting for, and a settle hold is exactly the window in which a paused host's
  services should stay down. `ava stop`'s unpause discriminator is the same
  reasoning at a different call site.
- **#1074 landed first, and the two compose as predicted — but only because the
  exception is scoped to one guard.** Pin and code now consult *two* signals: the
  lease, where `awaits` narrows *which leases* defer, and `current_orchestration()`,
  which #1074 added because a watchdog-spawned `ava-updater` takes no lease at all and
  is therefore invisible to the lease read. A settle hold naming this host says nothing
  about whether an updater is mid-checkout here, so the permission stops at the lease
  guard: letting it carry past the second one would force a checkout back underneath a
  live updater, which is precisely the flap #1074 closed
  ([decision](2026-07-31-two-healers-must-not-own-the-same-checkout.md)). Both
  controllers are tested for that intersection — permitted at the lease, still
  deferring to a live orchestration.
- **The schema controller keeps the broader reading, and that is now a standing
  choice rather than a merge-order hedge.** It defers on `note IS NULL` only, so every
  settle hold passes through, including one naming another host. Pin's and code's heals
  move a checkout or bounce processes — a real action, which is why licensing one on a
  host the hold never named is the wrong direction. Schema's heal answers "my code is
  behind the DB the whole cluster shares", which no other host's settle window creates
  or clears; tightening it to `awaits` would make it wait, under a lease nobody is
  executing under, for a convergence that is not its own — importing the mutual wait
  this decision exists to remove.
- **The adjacent false-green is not addressed here.** `on_pin` is computed from HEAD
  vs pin, so every pin-based surface reported the Air aligned while it executed a
  different commit. `ava cluster status`' `code` column does show `⚠` for it, but
  nothing treats `running_sha != head_sha` as an alarm. That is the #1012 family and
  wants its own change.

---

**Superseded in part (2026-07-31):** the consequence above — that "the
stranded-pause controller keeps the unconditional lease deferral, and that asymmetry
is deliberate rather than an omission" — was wrong about that one controller. A
settle hold naming this host is not proof someone owns its pause; it is the
orchestration's own record that the pause already lost its owner, so the
stranded-pause controller now takes the same `DeployLease.awaits(machine_name())`
narrowing pin and code use here. See
[a settle hold does not own the pause it waits on](2026-07-31-a-settle-hold-does-not-own-the-pause-it-waits-on.md)
(issue #1116). Everything else here stands, including the `awaits` reading itself and
pin's and code's deferral to a live local updater, which that change reuses unaltered.
