# 0010 — A failed restart must not look like an operator stop

**Date:** 2026-10-01
**Anchors:** `services/ava_root/health.py` (`_HELD_DOWN` branch),
`services/ava_root/supervisor.py` (`revival_deferral`); fixes: #3830, #3866,
#3909, #3937. Host-side records (supervisor log, agent event files, the
recovery logs) are not in this repo `(summarized)`; the drill VM and its logs
were destroyed, and its memory telemetry is summarized from a host-side monitor.

## Summary

A disaster-recovery drill running in a VM on an agent-runner host pushed the
host into memory pressure. The userspace network stacks that carry all of the
host's traffic stalled, the supervisor's automatic restart of the agent host
began, and its up half raised. The failed restart left the unit's `desired`
state non-RUNNING, so `revival_deferral` returned `"held down"` and the
health loop's `_HELD_DOWN` branch classified the unit as an operator stop:
expected state, no action, no counting, no alert, logged at debug level. The
runner then stood down silently for about three hours until a human forced a
restart. The guardrail: a unit that should be running but is held down is an
alertable state, and "expected" is derived from a recorded operator intent,
never from the absence of a desired-state transition.

## Timeline

Host-local times.

- 16:06–16:16 — the drill VM boots and starts a 16-way concurrent upload
  workload against a multi-GB dataset. The host goes memory-tight (swap
  ~3 of 4 GB used).
- 16:16 — the runner's client-pool stalls begin; pool "slow check" latency
  climbs from ~28 s to ~94 s over the next ~20 minutes while the database
  server side, host CPU and disk stay normal.
- 16:24 — the page server's health probe returns 503 (`loop: stale`); an
  automatic restart is attempted and deferred with "native custody requires
  reconciliation".
- 16:30 — the agent host reports 503 (`loop: stale`); its automatic restart is
  attempted and the up half raises. Five in-flight agent turns, blocked in
  non-interruptible calls, are abandoned.
- 16:31 — the health loop reclassifies the unit: `down (no listener) —
  expected (operator stop); no action`. No further restart is ever attempted.
- 16:33 — the agent host process exits; every hosted agent goes silent. The
  ops unit stalls and is stopped the same way shortly after.
- 16:33–19:37 — blackout. Wake deliveries retry a bounded number of times and
  are dropped (one message stranded). The supervisor logs "restart held for N s"
  locally only, reaching 10,132 s.
- 19:33–19:35 — a plain `ava start` fails against the locally dead services;
  `ava restart --mode smooth` refuses because an agent has no complete persisted
  cold END.
- 19:36–19:38 — operator-approved forced restart. Custody for the dead process
  group is released, the agent host and ops return, and the stranded wake
  delivery is replayed from the outbox.
- Impact: every agent on the runner stalled for about 3h05m; other machines of
  the cluster were unaffected.

## Root cause

Two mechanisms compounded.

**1 — Resource pressure became a network-stack stall.** The runner's egress
path is userspace: VM traffic crosses the VM manager's userspace network stack
and then the host proxy's gVisor-based TUN stack, two userspace TCP relays per
connection. Under memory pressure those relays stalled; the upload workload's
retries piled up over a hundred half-dead connections. Host CPU was not
saturated and the database server was healthy; the symptom "large uploads time
out" was a stalled relay, not link capacity or a remote service. Post-recovery
measurements on the same host were clean (8 MiB upstream in 3.6 s, 16-way
concurrency completing, flat ping under load).

**2 — A failed self-rescue was reclassified as an operator stop.** The
automatic restart of the agent host has a down half and an up half. The down
half ran; the up half raised. The failure left the unit's `desired` state
non-RUNNING, and `revival_deferral` treated any non-RUNNING `desired` as
intentional:

```python
if runtime.desired is not DesiredState.RUNNING:
    return "held down"
```

The `_HELD_DOWN` branch exists so a genuine operator stop generates no noise:
it resets the failure counter and logs at debug level. The system's own failed
rescue was therefore indistinguishable from a deliberate stop. The same failed
restart also retained a native custody record for the half-dead unit, so later
starts hit `EEXIST` and health reported "native custody requires
reconciliation" until the process group was verified gone.

**Escape analysis.**

- (a) Tests around `revival_deferral` covered the paths it names — explicit
  stop, retained custody, never-restart policy. No test or invariant required
  that a restart whose up half fails ends in an alertable state; the classifier
  saw only the residual `desired` value, not how it got there.
- (b) The custody record was a lock with no self-release path for an unclean
  death. A start that found it could only dead-end, which pushed recovery onto
  a force escalation nobody had been told was needed.
- (c) Wake delivery treats its bounded retry budget as terminal (about five
  attempts over ~7 minutes, then silence). That is acceptable only if host
  recovery is guaranteed within minutes, which is exactly what failed.
- (d) The supervisor's out-of-band visibility was zero: every "restart held"
  line lived in local logs.
- (e) The drill ran on the same host as the supervisor chain that lets the
  fleet observe it, with no host-level cap or pre-flight budget; the load that
  caused the collapse shared fate with the watchdogs that would have seen it.

## Guardrails added

- **Explicit unit intent (#3830).** `intent` (running or stopped) with a
  source (operator, self or selection) replaces `desired` as the only policy
  fact classification reads; it is recorded per unit in an atomic store and
  merged conservatively at boot. `revival_deferral` reads intent only.
- **A failed restart is a recorded, retryable state (#3830).** `restart()`
  works per member and records `restart_failed` with its stage (down or up)
  while intent stays running; a recorded failure is deliberately not a
  deferral, so the backoff retry it exists to trigger still fires. Integration
  tests drive the real supervisor with the real monitor
  (`services/ava_root/tests/test_ava_root_intent.py`,
  `services/ava_root/tests/test_ava_root_health.py`).
- **State-based alert episodes (#3866).** A unit that is meant to run but sits
  in an explicit failure state fires one durable alert episode: it survives a
  root restart, resolves when the condition clears, goes out on the event
  stream and posts to the platform's existing alert store. No numeric
  thresholds anywhere in the chain (`services/ava_root/alerts.py`,
  `services/ava_root/tests/test_ava_root_alerts.py`).
- **Custody reconciles before it refuses (#3909).** A stale custody
  record whose process group is provably gone is released before a cold start
  refuses; a record that keeps an unproven fact still refuses, naming its
  reconcile steps and evidence path (`services/ava_root/custody.py`,
  `services/ava_root/reconciling.py`).
- **Wake re-dispatch waits for a live host (#3937).** Wake re-dispatch and
  poisoning require a fresh healthy host verdict for the owner's machine — a
  `machine_probe` row inside the staleness window, machine graded online,
  agent host alive. A missing, stale or unreachable verdict freezes the
  pending row instead: no publish, no dispatch-count advance, no poison — a
  host outage cannot burn a row's dispatch budget — and redelivery resumes on
  the first fresh verdict (anchor: the host returning at 19:37:50, first
  delivery 19:37:55). One `delivery_poisoned` event still marks an exhaustion
  against a reachable host, and the stall report keeps frozen rows visible
  (`services/delivery_watchdog/dispatch_guard.py`).

Unguarded, relying on the rule alone: the wake-delivery fix (#3937) removes
the outage-driven corner of (c) — a host outage freezes a row instead of
burning its budget, and redelivery resumes on the first fresh verdict — but
exhaustion against a reachable host still ends in a recorded poison rather
than an escalation: one `delivery_poisoned` event, the row left pending and
claimable. The load-isolation rule (e) is an operating convention, not code:
drills with sustained multi-stream network I/O or multi-GB working sets run on
an isolated machine, or with hard caps recorded before starting.

## Lessons

- A failed self-repair must become a visible state; a classifier that maps
  "not as desired" to "the operator meant it" turns a rescue failure into
  silence.
- "Expected" must come from an explicit intent record; deriving it from
  residual state makes the system's own transient failures look deliberate.
- A lock taken by a failed restart needs a release path; a lock whose only
  exit is `--force` turns an incident into a manual-recovery dependency.
- A bounded retry that ends in silence has chosen to drop data; escalation is
  part of the retry design.
- When the watchdogs share fate with the workload, load isolation is
  observability.

The general rule is condensed in
[`conventions/defensive-patterns.md`](../conventions/defensive-patterns.md).

## Addendum (2026-10-02): the custody block on the browser unit

A second, milder instance of the same family occurred on the macmini host the
next day: the browser unit stayed down from 11:58:59 to 16:45:35 (~4h47m) on
the old code. The restart breaker opened after five rounds without a live
probe; later rounds reported `NOT REVIVABLE (port 9222 has no root-owned
generation)`, and the unit then sat in "restart held — native custody requires
reconciliation" until that counter reached 13,548 s. No automatic recovery
existed; the custody record was released by hand during the 16:45 maintenance
window, and the unit was healthy again by 16:49. The episode alert was not
routed anywhere — the running code had no router, a routing gap rather than a
delivery failure.

On the new code (ed3f8b0b4 and later) the covered behavior is the reverse: a
process group whose recorded births are gone is released automatically, and a
group that cannot be proven gone is retained with an alert plus its
documentation. No new-code instance has happened yet, so the alert path itself
still awaits its first real case (a tail item of #4872).

Read together with the guardrails above: the same lesson at smaller stakes — a
custody hold that cannot release itself turns a transient restart failure into
a manual-recovery dependency, and the fix set's release path is what retires
it.
