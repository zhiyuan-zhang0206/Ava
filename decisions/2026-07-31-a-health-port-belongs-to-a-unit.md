# A daemon health port belongs to a unit, not to a cluster

## Context

One physical machine hosts two Ava units — a native Windows unit
(`C:\Users\ava\.ava`) and a WSL2 unit (`/home/ava/.ava`) — **both
agent-runners of the same cluster**. WSL2's NAT networking republishes the Linux
unit's loopback listeners on Windows' loopback, so the Windows unit probing
`http://localhost:8102/healthz` reached the WSL2 unit's restarter. Its watchdog
log holds 402 identity-mismatch lines from 2026-07-26, alternating
restarter/ops every ~2 minutes, each ending `manual intervention needed`.

Two obvious explanations were ruled out with evidence, because both are where
the eye goes first:

- **Not a port-allocation collision.** Both units sit at their platform's
  default `~/.ava`, so the 18000-20000 allocator never ran for either; an
  enrolled runner's health ports came from the *gateway's* `/api/bootstrap`
  payload, not from any registry. The two units were handed identical ports **by
  construction**. A shared registry would have changed nothing.
- **Not a bind-address defect.** The WSL2 restarter already binds `127.0.0.1`
  only — the narrowest bind available — and the relay republished it anyway.
  Binding loopback + `AVA_MACHINE_HOST` would not help; the relay reflects
  `127.0.0.1`-bound ports.

What it is: **a port block is a property of the cluster, and the collision
domain is a machine's localhost namespace.** Those two coincide until something
gives one machine two localhost namespaces — WSL2, containers, netns — and then
they diverge silently. It is also *nondeterministic rather than steady*: which
side owns the Windows loopback port turns on relay-vs-daemon bind ordering and
is re-decided on every restart of either unit and every WSL2 boot.

The diagnosis was aimed at allocation by the probe's own wording. It said
*"another cluster's daemon holds this port"* while comparing `home`, which is
the **unit** identity — and here both daemons were in one cluster. The check was
right; the sentence was wrong.

## Decision

Health ports become a **per-unit** fact, in three parts that depend on each
other.

1. **The gateway stops owning a runner's health ports.** The seven
   `AVA_*_HEALTH_PORT` keys leave `cli/enroll.py:_CLUSTER_ENV_KEYS`, and their
   config scope flips from `cluster-pinned` to `host`, so `/api/bootstrap` no
   longer serves them. Nothing about them was ever cluster-constrained: the
   runner computes its own ops URL from its own `health_port('ops')` and
   registers it (`shared/machines.py`), and the gateway reads that URL back off
   the machines row. This is also what makes a per-unit value **durable** —
   `cli/start_refresh.py` re-materializes the bootstrap keys on every `ava
   start`, so a hand-set port was silently reverted before.

2. **`ava enroll --health-port-base <N>`** writes the whole set, derived from
   the shared block layout (`shared/port_block.py:PORT_OFFSETS`, now a
   dependency-free module so the settings-free enroll path and `shared.cluster`
   read one table). No shared state and no locking: the operator states the fact
   the machine cannot discover, at the moment they know the co-location exists.

3. **`ava start` probes before it binds.** For each health port this unit is
   about to use it dials `/healthz`, and refuses to start when the answer is a
   terminal verdict — naming the occupant's `$AVA_HOME` and pointing at
   `--health-port-base`.

Part 3 is the part that survives a *third* unit. No arithmetic range can prevent
a future WSL2 install from re-taking 8102, because WSL2 can bind anything.
Detection is the only mechanism that does not depend on everyone having agreed
in advance.

The probe's message now says "another **unit's** daemon holds this port", and
the surrounding prose in `shared/daemon_health.py` says unit wherever it means
the `$AVA_HOME` boundary — including the per-home socket reasoning, since the socket
lives under `$AVA_HOME` and a foreign *unit* is exactly as unkillable as a
foreign cluster.

## Alternatives rejected

**Allocate health ports from the host registry instead of the gateway.** The
appealing fix, and it addresses nothing: both units sit at the default `~/.ava`,
where the allocator does not run, and a Windows unit and a WSL2 unit do not
share a `~/.ava/clusters.json` anyway — they are different filesystems. It would
also newly couple an enrolled runner to host-global state it currently has none
of.

**Have enroll pick a free base automatically.** Re-introduces the
nondeterminism the incident is made of: a port that probes free at enroll can be
taken later by a WSL2 boot, and the two sides of a relay do not observe the same
"free". An operator-stated base is a fact that stays true; a scan is a snapshot.

**Bind to `AVA_MACHINE_HOST` instead of loopback.** Ruled out by the incident
log — the relay reflects `127.0.0.1`-bound ports, so a wider bind changes
nothing except exposing daemon RPC to the LAN.

**Make the pre-bind gate refuse on any terminal verdict, not just health
ports.** It would fold in the browser, whose healthcheck *deliberately* tolerates
another unit's Chrome on the CDP port, so a headed box sharing hardware could no
longer start at all. It would also print `--health-port-base` as the remedy for
ports that flag does not move.

**Refuse on any port that merely fails to serve.** That is the readiness gate's
question, and answering it here would break the idempotent restart: every start
over a cold or crashed daemon would refuse instead of launching it. Only
`PORT_TAKEN` — reached solely when something outside this unit's reach answers —
distinguishes "launching is pointless" from "launching is the fix".

**Keep the topological workaround (never co-locate a WSL2 unit with a native
Windows one).** It was the standing advice and it is not enforceable: the two
installs are set up by different guides, months apart, and nothing on either
side can see the other.

## Consequences

- Two agent-runners of **different** clusters co-located on one host no longer
  get non-colliding ports for free — that was the property the gateway-served
  block bought. They now take the same defaults unless one enrolls with
  `--health-port-base`. The trade is deliberate: the automatic mechanism was
  solving the co-location problem with the wrong key, and part 3 converts the
  regression from a silent multi-hour misprobe into a refusal at start with the
  fix in the message.
- An already-enrolled runner keeps whatever ports its `.env` holds —
  `materialize_cluster_env` upserts, so dropping the keys from the payload
  removes nothing that is already written. A runner that should move needs a
  re-enroll with the flag (or a hand edit, which now survives `ava start`).
- `ava start` gains a failure mode that depends on what else is listening on the
  box. It is scoped to a terminal verdict on a port this start was about to
  bind, so the only way to trip it is the condition it names.
- **The refusal is all-or-nothing, and there is no flag that waives it.** One
  occupied health port stops the entire start — the gateway and the frontend,
  neither of which binds a gated port, do not come up either. That is on purpose:
  the remedy edits a `.env` and moves the whole block at once, so a "start the
  other six" mode would produce exactly the half-moved state the incident was
  made of. The de-facto workaround is `--disable-service <daemon>`, which drops
  the daemon from the roster the gate reads; the refusal message prints the
  ready-made flags. It is a stopgap — that daemon is then not running at all —
  and it is deliberately not a waiver: a rollout cannot reach it, because
  `--disable-service` is an operator decision recorded as durable intent, not
  something an automated leg passes to get past a gate.
- **The already-co-located WSL2 + native-Windows pair will hard-refuse `ava
  start` on the next attempt**, and a rollout to that host fails with it (the
  gate sits outside `_readiness_waiver` by design). That host is exactly the one
  the incident was reported from, so the first effect of this change on the fleet
  is a stop, not a fix: a human must re-enroll the WSL2 side with
  `--health-port-base` before either unit starts again. Chosen over letting it
  continue — it was already not working, it was merely reporting green while
  failing, and 402 lines of `manual intervention needed` is the same manual
  intervention arriving without a stop to force it.
- **The pre-bind probe sees only occupants that answer HTTP on `/healthz`.** The
  verdict is read out of a health payload, so a non-HTTP listener (a stray
  Postgres or redis), a socket that accepts and says nothing, and an HTTP server
  that 404s the path all read DOWN — the same as an empty port — and the start
  proceeds into the `EADDRINUSE` the gate was added to prevent. This closes
  #977's relay, which answers; the generic taken-port case is exactly where it
  was before.
- The identity check itself stays necessary and unchanged. A pytest-leaked
  daemon from the same checkout on the same fallback ports will always be able
  to answer, and a stray of a unit's *own* home is caught only by the `pid` arm.
  Per-unit ports remove one *source* of impostors; the identity check is what
  makes any impostor detectable; the terminal verdict
  ([`2026-07-29-identity-mismatch-is-terminal.md`](2026-07-29-identity-mismatch-is-terminal.md))
  is what stops a doomed respawn once one is detected. All three layers stay.
