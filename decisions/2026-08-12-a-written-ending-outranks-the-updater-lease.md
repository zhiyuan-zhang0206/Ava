# The Windows updater states its own exit verdict, and a written ending outranks the lease

## Context

Phase B decides when to stop waiting for an agent-runner by reading two facts off
that host: its `host_deploy_state` posture, and its updater lease. The lease is the
liveness half — `updater_live` means "the updater is still working, keep polling" —
and the poll's own log promises it is "cut short the moment a host provably stops".

The lease is a single write at the run's start, armed for `UPDATER_LEASE_TTL_S`,
which is the *same* 900 s the poll is willing to wait. So it can only ever say
"stopped" by being cleared, never by aging out inside the window it governs. Two
things kept it from being cleared on `win`:

- The cmd.exe update chain's abort branch ended in `exit /b 1`, which outside a
  batch script exits cmd.exe — taking the rest of the command line, including the
  chain's trailing `_updater_lease clear`, with it. The *cheapest* failure the chain
  has (a `git fetch` that cannot reach origin, over in seconds) therefore produced
  the most expensive reading: a host claiming a live updater for 15 minutes.
- The clear needs the DB, and it runs inside the very restart that may have taken
  the path to it down.

**And an uncleared lease outlives the update that armed it**, because nothing clears
the column on the way into a pause — `set_posture` owns the posture alone, on purpose
(a pause landing mid-rollout must not erase a live updater's claim, audit 2026-08-08
P2). So the *next* update inherits the expiry, and between its
`pause_local_cluster()` and its updater's first touch — a detached session spawn plus
a Python cold start, which on a Windows host is the slow part — the row reads exactly
like a host whose updater died. Two probes is four seconds. Both readers of that fact
act on it: Phase B abandons the host as `POLL_STALLED`, and
`ops.cluster_deploy._updater_hung` reports the session it just spawned as hung for
the controller to force-kill.

`win`'s own `ava-updater.out.log` has the whole chain in it, one run after the other:

```
  ✗ converge failed: watchdog-probe registration failed on Windows for agent-runner
[updater] checkout/sync or tree verification FAILED -- refusing to start services … [updater-run] 1786592095
```

The 2026-08-12 11:40 run took the abort branch and `exit /b`'d — the next run's start
marker is glued onto the end of its last line with no newline between them, and no
`_updater_lease clear` ever ran, so the lease stayed armed with an 11:55 expiry. The
20:34:55 run inherited it, was declared STALLED within seconds of its pause, and then
went on to finish normally: same log, `ava start` through to "cluster pin: c5f0539 —
this host HEAD c5f0539 [aligned]", last written 20:36:01 — while the rollout that had
given up on it was still printing, finishing at 20:36:22.

That abort branch is also mis-blaming itself. Nothing was wrong with the checkout,
sync or tree: `ava restart` failed inside converge, its recovering `ava start` failed
the same way, and the ladder's non-zero group fell through the `||` into a message
about a mixed tree.

The second signal that could have contradicted any of this did not exist on that
platform. `ops.updater_outcome` reads the updater's log for a verdict, but the POSIX
chain wrote `[session-exit] rc=$?` and the cmd.exe one wrote nothing — the reader
said so in its own message ("it died mid-flight, **or this is Windows, where no exit
line is written**"), one label over two opposite situations, on the box that is
hardest to ssh into and check by hand.

Prod, 2026-08-06..12, 32 rollouts: `win` converged 11 times inside polls of 0-11
minutes, 5 rounds spent the full 900 s poll, and 2 abandoned it in seconds on a lease
that was not its own. Every one of the 7 also held a 15-minute settle window on top.
The generous bound was doing its job for the slow case and being spent by the failed
one; the fast verdict was firing on the case that was working.

## Decision

**The lease is dated against the pause window, and both platforms write an exit
verdict the poll believes over the lease.**

- `HostDeployState.updater_expired` replaces "the column holds a past timestamp" as
  the provable-stop reading, for both of its consumers (`_probe_verdict`,
  `_updater_hung`). A lease armed during this window expires at
  `armed + UPDATER_LEASE_TTL_S`, so `expires - TTL` dates the arming; anything armed
  before `paused_at` is an earlier update's residue and is not evidence. An undatable
  row (no pause window) is not evidence either — one caller would kill a live
  updater, the other would strand a working host.
- **The whole row is stamped by one clock, Postgres'.** Dating a lease against
  `paused_at` is only meaningful if both come from the same source, and they did not:
  `paused_at` was SQL `now()` while the expiry was the *runner's* `datetime.now(UTC)
  + ttl`. A runner behind the DB by more than the pause-to-touch gap would have read
  its own live lease as a previous window's residue and never been reaped at all —
  trading the old false positive for a false negative, on exactly the host (a Windows
  box resuming from sleep) where clock drift is expected. The expiry is now
  `now() + make_interval(secs => ...)`, and `read`/`read_all` select `now()` into
  `db_now` so `updater_live` stops comparing a DB timestamp against the reader's
  clock as well.

- Every terminal arm of the cmd.exe ladder states its own rc literally
  (`ops.updater_outcome.native_exit_line`) — clean restart, preflight refusal,
  failed-then-recovered restart, and the checkout/sync abort. cmd.exe cannot expand
  the errorlevel at the end of a command line without delayed expansion, but it does
  not have to: the ladder has already branched on it, so each arm knows which
  outcome it is.
- The abort branch clears the lease itself, before `exit /b`.
- `_probe_verdict` treats a terminal `last_updater_outcome` (`exited` / `declined`)
  as proof the updater finished, overriding both lease-based "keep polling"
  readings. `unknown` deliberately does not qualify: that is what a *running*
  updater's log looks like.

## Alternatives rejected

**Capture the real errorlevel on Windows** (`setlocal enabledelayedexpansion`,
`call echo %%errorlevel%%`, a `/v:on` wrapper). It would report the exact rc instead
of a per-branch literal. Rejected because the exactness buys nothing a reader acts
on — the branch's own meaning is the distinction (`declined` vs failed vs clean) —
while the spelling is context-dependent cmd.exe trivia that this repo's CI cannot
execute, so a wrong variant would fail silently as the same missing verdict.

**Converge Windows onto the in-process updater entry**, as POSIX did in R1-6, which
would delete the ladder and the whole class of problem. Rejected for now: that entry
holds `python.exe` open out of the very venv `uv sync` is about to rewrite, which is
a Windows file-locking hazard with no way to test it from here.

**Renew the updater lease on a timer** so expiry becomes a real signal inside the
window. Rejected as the primary fix: it makes a *dead* updater detectable only after
a renewal interval elapses, where a written verdict is immediate, and it adds a
periodic DB write on the one platform whose leg is slowest. Worth revisiting if the
lease ever has to answer for a host that dies without writing anything.

**Clear the lease column when a pause window opens**, which would also stop a
previous run's expiry from being inherited, and at the writer rather than at two
readers. Rejected because the guard it would have to weaken is load-bearing:
`set_posture` deliberately does not touch the lease, so that a pause landing
*mid-rollout* cannot erase a live updater's claim (audit 2026-08-08 P2). Clearing
only an already-expired lease would thread that needle, but it makes the pause path
depend on wall-clock comparison to stay safe, where dating the row at the read makes
the same distinction without any writer being able to get it wrong.

**Drop provably-stalled hosts from the settle hold**, so a rollout that ends STALLED
does not also hold the cluster for 15 minutes. Rejected: the hold already ends early
the moment the held hosts reach the pin (`ops.deploy_window.settle_hosts_converged`),
and it already permits the self-heal of the host it names
(`decisions/2026-07-31-a-settle-hold-permits-the-host-it-waits-for.md`), so the
window is a ceiling rather than a wait. Removing it would trade a real protection
against a second deploy entering a half-transitioned host for a delay that
convergence already cancels.

## Consequences

- A genuinely dead updater that a *newer* pause re-stamps reads as "cannot tell"
  rather than stalled, because the re-stamp moves `paused_at` past the arming. The
  written verdict still catches it, and a re-pause means a newer rollout owns the
  host anyway — but it is the one case the dating rule reads conservatively.
- `UpdaterOutcome.kind == "unknown"` now means one thing — the session died before
  reaching any branch of its own chain — and is rendered as that. For exactly one
  rollout, the one that ships this, a Windows host still spawns the OLD chain from
  before its checkout moves, so its silence is read as a death it may not be.
- The rc a Windows recovery branch reports is the branch's meaning, not the `ava
  start` behind it: a restart that failed and was then rescued reports rc=1. The
  posture row is what says whether the host came back, and Phase B reads it first.
- Windows-only behaviour this repo's CI cannot execute. The ladder is pinned as a
  command **string** and the reader as pure text; that cmd.exe prints each arm's
  `echo` as its own line, and that `exit /b` outside a batch script ends the command
  line, are assumptions a real Windows box has to confirm.
