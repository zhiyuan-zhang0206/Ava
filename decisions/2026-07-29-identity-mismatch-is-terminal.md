# An identity mismatch is a terminal state, not a respawn trigger

## Context

Two Ava units share one physical machine's localhost namespace on the `win` box:
a Windows unit (`C:\Users\ava\.ava`) and a WSL2 unit (`/home/ava/.ava`). WSL2
forwards the Linux unit's listeners onto Windows' localhost, so the Windows
unit's healthchecks probe `http://localhost:8102/healthz` (restarter) and `:8106`
(ops) and reach the **other unit's** daemons.

The identity check `probe_daemon` applies did exactly its job:

```
[restarter healthcheck] daemon dead (identity mismatch on http://localhost:8102/healthz:
  home='/home/ava/.ava' != 'C:\Users\ava\.ava' - another cluster's daemon holds this port)
[restarter healthcheck] daemon restart FAILED (identity mismatch ...)
```

What followed did not. `respawn_and_verify` respawned and then could never
verify: the occupant still held the port, so the freshly launched daemon died on
it and every probe in the 20s verify window returned the same mismatch. Measured
on that box: ~22s burned on the restarter plus ~23s on ops, every round, which
together with the 60s sleep is what set the observed ~2-minute watchdog cadence —
for hours.

The identity check itself is load-bearing and is not the problem. It exists
because a pytest-leaked restarter daemon on prod's default port kept a
healthcheck green for 98 minutes while the real restarter was dead. Ironically
that outage, once the identity check caught it, would have entered this same
loop: the leaked daemon lived in a *different* cluster's session server, so prod's
respawn could never have killed it either.

Every not-alive verdict was treated as one condition. It is two.

## Decision

The probe's verdict distinguishes them, and the healthchecks branch on the
verdict.

`ProbeVerdict` is `ALIVE` / `DOWN` / `PORT_TAKEN`; `DaemonProbe.alive` and
`DaemonProbe.terminal` derive from it, so the two questions a caller asks ("is it
serving?", "can I fix it?") cannot drift apart. A `PORT_TAKEN` verdict is
reported at ERROR naming the occupying home and **not respawned**, exiting
`EXIT_PORT_TAKEN` (3) — distinct from `EXIT_RESPAWN_FAILED` (1, "respawned and it
did not come up", still retried next round).

**The line is drawn at reach, not at wording.** A respawn runs a session kill
ava-<svc>` on *this cluster's* socket first, so the question is whether the
occupant is inside that reach. A mismatched `home` or `name`, or a body that is
not an Ava `/healthz` at all, is out of reach → terminal. A pid our pidfile does
not record, or a missing pidfile, is reached only *after* home and name matched,
so it is a stray of this same cluster that kill-session does clear → stays
`DOWN`, respawned as before. An unreachable port, a 503 from a wedged loop, and
an unforeseen probe exception all stay `DOWN`: fail closed *toward retrying*.

The policy lives once, in `shared.service_respawn.run_keepalive` — the shared
body of all eight `/healthz`-probed healthchecks' `main()`, which had been
hand-copied per module. Remediation that does not depend on the respawn still
runs on the terminal path: the restarter's stand-in dispatch is passed as
`on_unrevivable`, because with the daemon unrevivable it is the only thing left
moving `restarting` rows. That hook is by construction never called ahead of a
respawn attempt, which is where the restarter's "nothing failable between the
dead verdict and the respawn" ordering invariant now lives — a property of the
shared runner rather than of each module keeping its `main()` in the right
shape.

`respawn_and_verify` also ends its poll on a terminal verdict rather than
spending the deadline waiting for a process that will not yield.

## Alternatives rejected

**String-match the `detail`.** `probe.detail.startswith("identity mismatch")` is
a one-line change. Rejected: it makes the same fact true in two places, and the
detail string is operator prose that will be reworded. It is also wrong on its
own terms — the pid-mismatch detail *says* "identity mismatch" and is precisely
the respawnable case.

**Weaken the identity check so a foreign 200 reads as alive.** Removes the loop
by removing the detection. This is the 98-minute outage, restored.

**Track the terminal state in a file and back off.** A counter or marker under
`$AVA_HOME` would let the healthcheck escalate over rounds. Rejected: there is
nothing to remember. When the occupant leaves, the next probe reads `DOWN` and
the normal respawn runs — the state self-clears by construction, and persisting
it only adds a thing that can be stale.

**Reallocate the port automatically.** The healthcheck could ask for a free
health port and rewrite the cluster's config. Rejected outright: a keepalive
process silently changing a cluster's port block is a much larger blast radius
than the outage it would paper over, and the ports are also written into OS
scheduler jobs and peer config.

## Consequences

- The terminal state's observer is the capability's watchdog log: one ERROR line
  per round plus the watchdog's own `reported failure (exit 3)`. That is the same
  surface every other healthcheck failure already lands on, and the round now
  costs a single probe instead of ~45s of doomed work.
- **It is not observed by the alerting path**, and that is unchanged by this
  work. `ava cluster health-probe`'s per-service check and `ava status` both
  probe by plain HTTP 2xx (`cli/commands/_probe.py:_probe_service`), which an
  occupant satisfies — so the condition reads *green* there. The health probe is
  also gateway-only, so an agent-runner has no such observer at all. Making
  `ServiceSpec` probes identity-aware is the deferred "verify the real contract,
  not liveness" batch named in `ops/observe.py`; it is the natural next step and
  is where the Telegram owner alert would come from.
- The eight healthcheck `main()`s are now one shared function. A ninth
  `/healthz`-probed service gets the policy by construction rather than by
  copying it.
- **The root cause is untouched.** Two units sharing a localhost namespace across
  the WSL2 boundary is invisible to port-block allocation: the registry allocates
  per home path on one host, and neither host can see that the other's
  loopback is its own. Whether the durable answer is distinct port blocks for
  co-located units across that boundary is a separate design question, recorded
  here and not built.
