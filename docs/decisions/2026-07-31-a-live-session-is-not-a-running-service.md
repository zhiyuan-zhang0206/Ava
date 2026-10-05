# A live session is not a running service

## Context

Two mechanisms in the stop/start pair each answered a question with a cheaper
question, and composed into a start that skipped a daemon and said `✓`.

`shared/session_backend.py:_graceful_kill_session` force branch ran a
kill-session with `check=False, capture_output=True` and returned `(True, "forced")` without
re-asking anything. `⚠ <session> (forced)` was printed whether or not the session
died. The graceful loop above it polls `has-session`; only the branch taken exactly
when the daemon was hardest to kill did not.

`cli/commands/_session_lifecycle.py:_launch_sessions` skipped any spec whose
session already existed. That is the right idempotence intent asked of the wrong
object: the server tracks the pane, and the pane's shell can outlive the daemon
that ran in it.

On 2026-07-30's 21:09 prod rollout both fired. The stop reported a forced kill of
`ava-gateway`; the session survived; the start 24 s later printed `✓ ava-gateway
already running` and launched everything else. `~/.ava/run/gateway.pid` and the
session's `session_created` both read 21:11:29 — a minute later, written by the watchdog, not by
the start. `gateway.log` has zero lines in between. Every prod rollout log inspected
(5 of 5) shows `⚠ ava-gateway (forced)`: the gateway always outlives the 15 s graceful
window, so the unconfirmed branch is taken every single time and the four runs that
did not break were luck, not difference.

## Decision

**A kill reports what it achieved, and the start's skip is conditioned on the
service.**

`ok` in `SessionBackend.kill_session`'s `(ok, mode)` is now defined as *the session is
confirmed gone*, not *the kill command was accepted*. `_force_kill` re-asks
`has-session` for up to `_FORCE_CONFIRM_TIMEOUT_S` and returns `(False, "forced")`
with the kill's stderr logged when the session is still there. This also fixes the
inverse: `kill-session` exits non-zero for a session that never existed, so the old
`returncode == 0` contradicted the documented idempotence. `shared/winproc.py` keeps
its session record when the process survives the kill, for the same reason — dropping
it would answer `has_session` False for something still holding its port.

The stop's printed marker consumes that answer rather than restating the mode:
`cli/commands/stop.py:_stop_sessions` prints `✗` for an unconfirmed kill, keeping
`⚠` for a *confirmed* forced one. Those are different events — a daemon that refused to
exit gracefully but did exit, versus a session that is still standing — and collapsing
them into the one `⚠` is what made the incident's rollout log read as normal.

`_launch_sessions` skips only when the session exists **and**
`_probe._husk_session_reason` returns None; otherwise it kills the session and
launches. The reason string is the probe's own detail, printed, so a torn-down session
is never a surprise.

Two services are deliberately still skipped on the session alone: one whose probe
cannot judge a launch this fresh (the frontend, whose `npm run build` runs ~30-60 s
before Next.js answers) and one that has no probe at all (`browser-mcp`, whose
transport only its own healthcheck dials — `alive is None` means *never observed*, not
*down*). The frontend exemption is `_probe._probe_judges_a_fresh_launch`, shared with
the readiness wait, which had the same carve-out for the same reason.

## Alternatives rejected

**Fix only one of the two.** Either alone would have prevented the outage. Both are
kept because they answer different questions and each is separately wrong: a stop that
misreports leaves no trace for the *next* consumer, and a start that trusts the session
would be re-broken by any other route to a husk (a killed pane shell, a
whole-server-kill race, an operator's stray session spawn).

**Ask the server whether the pane is dead (`#{pane_dead}`) instead of probing.** It
does not describe this husk. The server destroys a session when its pane process
exits, so a session that still exists has something alive in it — the `bash -lc` wrapper, not the daemon.
`pane_dead` would have read 0 throughout the incident.

**Gate the husk check on session age instead of the probe** — treat a session younger
than the boot budget as "still coming up" and only relaunch older ones. It discriminates
the one false positive the probe check has (a concurrent start catching a daemon
mid-bind) without any per-service exemption, and the incident's husk *was* hours old, so
it would have caught it. Rejected on cost: it needs a new `session_started_at` on both
backends (`#{session_created}` on one, a winproc record field on the other) for a false positive whose
entire damage is one extra restart of a service the start then waits for anyway. The
frontend exemption we do need is one shared predicate, already required by the readiness
wait.

**Relaunch via `shared.service_respawn.respawn_service`,** which already kills a stale
session first. It composes its own session name from the bare kebab and launches
through `forward_env_prefix` without the login shell, so the husk path would launch
daemons differently from the fresh path in the same function. Killing through the
module's own `_kill_session` keeps exactly one launch path.

**Make `_do_stop` return non-zero when a session survives.** It is the caller that
would suffer: `cmd_restart` returns early on a non-zero stop, so a stop that could not
kill one session would leave the host stopped and never start it. Reporting is the
right severity here; the start-side guard is what turns the report into recovery.

## Consequences

- `ava stop` / `ava update`'s stop now spends up to `_FORCE_CONFIRM_TIMEOUT_S` per
  session that outlives its kill. A session that dies confirms on the first poll, so a
  healthy stop pays one extra `has-session` per service and nothing else.
- `ava start` on a host whose sessions are all live and serving pays one probe per
  service — the same probes the status snapshot runs seconds later.
- A start can now tear down and relaunch a session that was merely slow to bind (a
  concurrent `ava start` catching a daemon mid-boot). The cost is one restart of a
  service the start then waits `SERVICE_READY_TIMEOUT_S` for; the cost of the opposite
  mistake is an unlaunched daemon reported as `✓`.
- Tests that fake `subprocess.run` with a blanket `returncode=0` have faked a server
  in which nothing ever dies, so every kill in their stop plan burns the confirm window.
  `tests/conftest.py:_guard_force_kill_confirmation` shrinks the bound suite-wide
  rather than stubbing the function, so what the confirmation decides stays exercised.
- This is the defect `decisions/2026-07-30-readiness-waiver-reads-the-lease.md`
  deferred; the waiver stopped a husk from costing a cluster-wide revert, and this
  stops the husk.
