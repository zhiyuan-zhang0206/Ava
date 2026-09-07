"""The deploy timeout family — one definition of "this host stopped making
progress", and the rule that keeps the deploy lease alive across it.

These numbers used to live as three independent constants in three modules, each
calibrated when every host in the fleet was POSIX:

- `cli.commands.update._POLL_TIMEOUT_S` (120 s) — how long Phase B waits for an
  agent-runner to report `paused=false`.
- `shared.cluster_lock.SETTLE_TTL_S` (900 s) — how long the lease stays held after
  an orchestration exits with hosts still converging.
- `ops.updater_reap._UPDATER_STALL_TIMEOUT_S` (900 s) — when a host's own updater
  session is hung rather than slow.

**They are not independent.** All three answer the same question — "has this host
stopped making progress?" — and the smallest of them decided when the deploy lease
stopped protecting the deploy. A host slower than 120 s made the Phase-B poll give
up, the orchestration return, and the lease be released or downgraded *while that
host's checkout had moved and its processes had not*: the exact state a second
deploy must not start into (the 2026-07-29 incident). A protection that expires
mid-operation is worse than a visibly absent one, because everything downstream —
the pin controller, the code controller, the stranded-pause controller, the health
probe's auto-rollback suppression — reads the lease and believes it.

## The invariant

**The lease must not expire before the operation it protects can finish.**

It is held two ways, and the split matters:

1. **While the orchestration is executing**, the lease is *renewed* on a timer
   (`LEASE_RENEW_INTERVAL_S`) by the process running it — see
   `shared.cluster_lock.renew_update_lock`. So `LOCK_TTL_S` is no longer a ceiling
   on how long a deploy may take; it is purely the crash-reclaim bound its
   docstring always claimed it was. This is what decouples *how long is allowed*
   from *how long is normal*: a slower fleet no longer needs any constant here
   re-audited, and a rollout whose process dies still releases within one TTL
   because renewal dies with it.
2. **After the orchestration stops executing** with hosts still mid-transition, the
   lease converts to a bounded settle hold (`SETTLE_TTL_S`) that
   `ops.deploy_window` ends the moment those hosts reach the pin.

## Why one number, and why 900 s

`NO_PROGRESS_TIMEOUT_S` is the single definition of "stopped making progress", used
by every consumer above. Two clocks that disagree about that are two chances to
declare a host dead while it is working, or alive while it is not.

The value is inherited from `_UPDATER_STALL_TIMEOUT_S`, which was already this
repo's standing answer to the question, rather than re-derived — the point of this
module is to stop having several answers. It is **far** above the measured POSIX
leg: a full agent-runner self-update (fetch + force-checkout + `uv sync` + restart
to serving) took 2-15 s across 44 samples on two hosts (`updater-*.log`
epoch-to-mtime on a runner n=24 and a WSL host n=20, 2026-07-01..29).

It is generous because the *Windows* leg is two orders of magnitude slower, which
prod's rollout logs now show rather than guess: `win` converged through Phase B on
11 of the 32 rollouts of 2026-08-06..12, inside polls of 0-11 minutes — an upper
bound on its own leg, since a poll ends only when the slowest acked host returns.
Five of the remaining rounds spent the whole bound. So this number is not a
first-principles ceiling any more; it is roughly 1.5x the longest leg observed, and
the thing that has to stay true is the sentence below rather than the margin.

`STAGE_NO_PROGRESS_TIMEOUT_S` is the one other clock in this family, and it is also
a different question rather than a competitor: how long one *stage* may run, versus
how long the whole run may. It shares the whole-run number's calibration convention
(~1.5x the longest legitimate observation) and its consumer split — the host reaper
and the Phase-B poll judge it together (see the constant's doc).

`GATEWAY_READY_TIMEOUT_S` is **not** a fourth answer to that question and must not be
folded into it. It bounds one local uvicorn binding its port before Phase B is allowed
to fan out at all — a strictly smaller job than the remote checkout + sync + restart
the no-progress number covers, and a *precondition* of that work rather than a
measurement of it. Two clocks that disagree about "stopped making progress" are a bug;
two clocks measuring two different things are not.

Raising the poll from 120 s to this is only affordable because the poll no longer
*spends* it on a host that has provably stopped: a host that answers "still
paused, no orchestration running" has lost its updater and is not coming back on
its own, and the poll returns that verdict in seconds instead of waiting out the
bound (`cli.commands.update._probe_one_until_unpaused`). Patience for the slow case
and speed for the failed case are the same change.

**Which puts the whole weight on the failed case being *detectable*.** A bound
this generous is a liability for exactly as long as a stopped host can look busy,
and this number is also the updater lease's TTL — one write at the run's start —
so anything that keeps a run from clearing it buys the failure the patience meant
for the slow. That is why the updater states its own ending on both platforms now
(`ops.updater_outcome.native_exit_line`) and why the poll treats that ending as
outranking the lease.
"""

from __future__ import annotations

# The one definition of "this host has stopped making progress". Consumers:
# `PHASE_B_ABSOLUTE_TIMEOUT_S`, `shared.cluster_lock.SETTLE_TTL_S`, and
# `ops.updater_reap._UPDATER_STALL_TIMEOUT_S`.
NO_PROGRESS_TIMEOUT_S = 900.0

# The advertised per-host Phase-B absolute deadline. It is an alias rather than
# another calibration: the poll, settle hold, and updater reaper must share one
# definition of when a whole host update stopped making progress. C3's separate
# `CONVERGING_POLL_TIMEOUT_S` is only the earlier handoff for continuous progress.
PHASE_B_ABSOLUTE_TIMEOUT_S = NO_PROGRESS_TIMEOUT_S

# How long one production `uv sync` may run before the updater kills its whole
# process tree and reports a terminal failure (`cli.commands._update_uv_sync`).
# Sized for the slowest healthy legs observed — full self-update syncs measured
# 0.7-13.7 s on POSIX and ~76 s on the Windows agent-runner (2026-08 rollouts) —
# with headroom for a genuinely slow download of a new runtime wheel. Registered
# in the clock lattice BELOW NO_PROGRESS_TIMEOUT_S and BELOW
# STAGE_NO_PROGRESS_TIMEOUT_S: a hung sync must fail itself into a terminal
# outcome before the stage no-progress judgment reaps the updater, so the
# updater's own recovery ladder — not the reap — gets to the host first. The
# 2026-08-30 rollout's bare `uv sync` spent 449 s downloading a dev-only pyright
# wheel on Windows and would have been bounded by this clock.
UV_SYNC_TIMEOUT_S = 600.0

# How long the Phase-B poll keeps waiting on a host that is ALIVE AND MAKING
# PROGRESS before it hands the rest of that host's convergence to the settle hold
# (C3, 2026-08-30 rollout-1788074072: the wsl runner stayed 'converging' for the
# whole 900 s bound — 340 probes — while its convergence was already covered by
# the settle hold + watchdog that follow the orchestration's exit).
#
# The family's `NO_PROGRESS_TIMEOUT_S` stays the poll's ABSOLUTE deadline (and
# the stalled/no-progress verdicts still end a host's poll in seconds regardless
# of either bound); this is the patience for the one shape that can otherwise
# spend the whole bound without proving anything — a host whose updater lease is
# live and whose stage evidence keeps moving, i.e. one that may be slow-but-
# working. 900 s of *that* is the 15.7-minute CLI wait the operator cannot
# interrupt; the 300 s value is the CTO ruling for this patience (rollout timing
# report §3-C3, task #2189). Beyond it the remaining convergence is the settle
# hold's job — the hold was built for exactly this ('SETTLE_TTL_S' docstring: an
# orchestration that ends with hosts still converging calls settle_update_lock),
# and the settle window (900 s) is untouched, so the cluster stays guarded for
# the same total as before.
#
# Lattice: strictly inside NO_PROGRESS_TIMEOUT_S — the converging bound exists
# to spend LESS of the absolute deadline, so a value at or beyond it would make
# the early exit unreachable; and far above the poll interval + stall
# confirmations, so the stuck/no-progress verdicts (which end a host in seconds)
# always win the race to return before this bound does.
CONVERGING_POLL_TIMEOUT_S = 300.0

# How long ONE updater stage may be in flight before both its host and the Phase-B
# poll call it no-progress (P1, 2026-08-30 rollout-1788074072: the win uv stage).
#
# Deliberately NOT the whole-run `NO_PROGRESS_TIMEOUT_S`, and for the same reason
# `GATEWAY_READY_TIMEOUT_S` is not folded into it: a different question. That one
# bounds the whole leg (fetch + checkout + uv + restart, measured up to ~11 minutes
# on the Windows runner); this one bounds a single stage, whose observed worst —
# `uv` at 449.2s on 2026-08-30 — is one half of the leg. One definition, two
# consumers, same as the family number above:
#
# - the host's hung-updater reaper (`ops.updater_reap._updater_hung`) kills an
#   updater whose own log shows its current stage stuck beyond this bound;
# - the Phase-B poll (`cli.commands._update_phase_b`) returns POLL_NO_PROGRESS
#   for a host whose probes keep reporting that same stuck stage.
#
# The value is the family's own calibration convention — ~1.5x the longest
# legitimate observation (449.2s x 1.5 = 673.8, rounded up to 675.0) — applied to
# stages instead of legs, and must stay strictly inside NO_PROGRESS_TIMEOUT_S
# (lattice): the stage judgment has to fire while the whole-run patience it draws
# from is still there to matter. It must also stay ABOVE the longest stage a healthy
# host has ever shown — and ABOVE `UV_SYNC_TIMEOUT_S` (lattice), so the bounded
# sync's self-termination lands before the stage judgment does — which is the
# false-positive line: a host reaped at this bound is one that has not finished a
# stage for longer than any stage has ever legitimately taken.
STAGE_NO_PROGRESS_TIMEOUT_S = 675.0

# How long a Phase-B poll keeps reading a paused host with no live updater
# lease as "the updater has not armed yet". The updater arms its lease at the
# chain head within seconds of its spawn, and the spawn precedes the poll, so a
# paused host still lease-less past this grace is one whose updater provably
# ended without clearing posture — a stall candidate (2026-09-02 win: the
# updater's recovery `ava start` exited rc=1 under the executing deploy lease,
# its lease clear ran, and the poll then burned the whole 900 s bound on the
# never-stall "paused with no lease" reading). Anchored to the poll's OWN
# elapsed clock, never `paused_at`: the pause (Phase A fan-out) and the updater
# spawn (Phase B trigger) are minutes apart by design — the gateway's local leg
# sits between them. Legacy pre-lease chains predate this fleet; a working
# updater always arms inside the grace.
LEASE_ARM_GRACE_S = 90.0

# How long the Phase-B poll harvest waits before re-probing a host whose posture
# just went idle but whose updater stage capture is missing its final `start`
# stage line (Task #1820). The updater's `start` line lands a few ms AFTER the
# posture row flips idle, and a host that converged between probes carried no
# stages at all — one short wait makes the harvest deterministic instead of
# racy. Best-effort: a failed or empty harvest changes nothing about the poll
# verdict. Must stay far inside NO_PROGRESS_TIMEOUT_S: the re-probe reads the
# host's outcome through the same fresh-idle window, and a grace at or beyond
# the window would always find the reading stale, silently dropping a converged
# host's completed stage breakdown.
HARVEST_GRACE_S = 1.0

# The gateway waits this long for a newly detached rollout/restart child to
# publish its persistent UI owner. The HTTP client must outlive that wait plus
# rollout's worst-case read-only release preflight (three 15 s git commands),
# otherwise it reports a timeout after the orchestration actually started and
# invites a duplicate submission.
ORCHESTRATION_OWNER_WAIT_S = 30.0
CLUSTER_DISPATCH_TIMEOUT_S = 90.0

# How often the process running an orchestration re-arms its own lease. Small
# relative to `LOCK_TTL_S` so a missed round (a slow DB, one dropped connection) is
# never fatal, and large enough that a multi-minute rollout costs a handful of
# single-row UPDATEs rather than a poll-rate write stream.
LEASE_RENEW_INTERVAL_S = 60.0

# The hosted agent ownership lease: `agents_meta.lease_expires_at`.
# TTL = 10x the renewal interval, so transient DB renewal failures do not
# immediately relinquish a live turn. Expiry bounds crash recovery.
AGENT_LEASE_TTL_S = 600.0
AGENT_LEASE_RENEW_INTERVAL_S = 60.0
# How long the orchestration waits for its own gateway to be serving before it tells
# any agent-runner to update (`cli.commands._gateway_ready`). A different question
# from `NO_PROGRESS_TIMEOUT_S` — that one bounds a remote host doing
# checkout + sync + restart; this one bounds one local uvicorn binding its port — so
# it is deliberately its own number and deliberately much smaller.
#
# Not measured on the prod gateway, and it does not need to be, because nothing on
# the healthy path waits: the gate returns on its first probe when the gateway is
# already serving, and every *diagnosable* failure exits at once (the gateway
# session is gone, it answers on loopback but not off-box, it answers non-200). The
# bound is spent only by a gateway that is alive and has bound nothing — and 3
# minutes of that is a hung gateway, not a slow one. The one hard datum behind the
# magnitude: prod's 2026-07-29 05:09 rollout has an agent-runner taking
# `[Errno 61] Connection refused` from the gateway ~39 s into the rollout, so
# whatever bounds "one local uvicorn binds its port" is well above 15 s.
#
# It must also stay far under `LOCK_TTL_S`: this wait sits *before* the Phase-B
# poll, so — unlike the poll — it spends lease time with no renewal task armed.
GATEWAY_READY_TIMEOUT_S = 180.0

# How long an agent-runner's self-update preflight keeps re-dialing the gateway
# before it declines (`cli.commands._repo._probe_gateway_or_die`). Defense in depth
# behind `GATEWAY_READY_TIMEOUT_S`, not a competitor to it: the gate above decides
# whether the fan-out happens at all, this one decides whether a *single* refused
# dial, arriving after the gate already saw a 200, is enough to declare the gateway
# down and strand the host for a settle window.
#
# 30 s, because the hole it must survive is one gateway restart the rollout itself
# causes — measured at ~9 s on the 2026-08-01 rollout (`gateway.log` silent
# 00:14:45 -> 00:14:54). It is deliberately far below `NO_PROGRESS_TIMEOUT_S`: the
# preflight is one dial in a leg that number bounds whole, so spending 30 s here can
# never be what makes a host look stalled. And it is spent only on the failing path —
# a reachable gateway answers on the first dial, exactly as before.
#
# It is NOT sized to outlast a gateway that really died: that needs the watchdog's
# 60 s round plus a respawn, and waiting for it here would only convert a legible
# `INCOMPLETE` into a slow one (`decisions/2026-07-30-accept-readiness-gate-residual-race.md`
# priced that and refused it).
GATEWAY_PREFLIGHT_BUDGET_S = 30.0

# How long `ava start` waits for the services it just launched to pass their
# liveness probes before it reports them unready and exits
# `SERVICES_NOT_READY_EXIT_CODE` (`cli.commands._probe._wait_for_services_ready`).
#
# The same physical job as `GATEWAY_READY_TIMEOUT_S` — a local daemon binding its
# port — hence the same value, and for the same reasons: nothing healthy waits (the
# poll returns the instant every probe passes), and a service whose session has
# died ends the wait at once instead of spending the bound. It is a separate
# constant rather than a reuse because the two have different observers and
# different escalation paths: that one is the rollout orchestrator asking, off-box
# and authenticated, whether the *gateway* serves the address the cluster dials,
# and it alone carries the `LOCK_TTL_S` constraint above. This one is a host asking,
# on loopback, whether *every* service it just launched came up. Retuning one is not
# automatically a reason to retune the other, so they are stated separately — but
# they are stated adjacently, because a change in what "a daemon binds its port"
# costs would move both.
#
# They never nest: the rollout's local leg starts the gateway with
# `--no-readiness-gate` precisely so the readiness question is asked once, by the
# stronger off-box gate, rather than waited out twice.
SERVICE_READY_TIMEOUT_S = 180.0

# How long `ava start` waits for a NON-CRITICAL service to pass its liveness
# probe before it stops waiting on it (`cli.commands._probe`).
#
# The readiness gate is tiered: the critical roster keeps
# `SERVICE_READY_TIMEOUT_S`, because a start that cannot serve the core
# surface or the ops safety net is a failed start. The roster is the CTO
# ruling (Task #2183, C2): gateway / frontend / restarter / the hosted
# agent-runner / im-bridge / the two watchdogs (see
# `cli.commands._probe.CRITICAL_SERVICE_SESSIONS`). Everything else —
# pitr-uploader, labeler, the browser, ... — shares one short window instead,
# sized so a slow-but-healthy daemon still gets its beat to bind its port
# while a dead one stops taxing every start. 2026-08-30 rollout-1788074072
# spent 182 s of its 197.5 s local start waiting on a pitr-uploader healthz
# that never answered; the service's failure did not block the rollout's
# conclusion (the watchdog covers it), so the gate was waiting on a service
# whose verdict nothing depended on.
#
# Deliberately well below `SERVICE_READY_TIMEOUT_S`: the point of the tier is
# that the short window ends long before the critical bound, so a healthy start
# is never held to the long number by a straggling non-critical daemon. A
# non-critical service that misses the window does NOT fail the start — it is
# reported and posted as an alert instead (see
# `cli.commands._probe._notify_non_critical_unready_services`), so the
# downgrade never goes silent.
NON_CRITICAL_SERVICE_READY_TIMEOUT_S = 45.0
