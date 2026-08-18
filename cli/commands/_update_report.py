"""Aftermath reporting / verdict lookups for `ava cluster update`.

Split out of `cli/commands/update.py` to keep that module within the file-size
budget. These are the report-only surfaces — pure lookups and print blocks with
no orchestration side effects:
- `_local_leg_defect` / `_print_local_launch_failure_block` — the ROLLOUT
  INCOMPLETE block for sessions this host could not launch (the pin advanced,
  one local service process does not exist).

Re-imported by `cli/commands/update.py` (and re-exported through `cli.commands`)
so `cli.commands(.update)._local_leg_defect` keep resolving for tests.
"""

from __future__ import annotations

import sys

from cli.commands._update_phase_b import (
    POLL_CONVERGING,
    POLL_STALLED,
    PollVerdict,
)


def _local_leg_defect(failures: list[str]) -> str | None:
    """The `failing_step` a local launch failure names, or None when there was none.

    Named as a step because that is what the persisted update record and the status
    banner show: "the rollout ran and this is where it fell short" reads very
    differently from a rollout that reported nothing at all, which is what a swallowed
    `new-session` refusal produced.
    """
    if not failures:
        return None
    return f"the gateway's local `ava start` could not launch: {', '.join(failures)}"


def _print_local_launch_failure_block(sessions: list[str]) -> None:
    """The ROLLOUT block for sessions this host could not launch — the aftermath a
    rollout that only failed *locally* would otherwise never print.

    Separate from `_print_rollout_aftermath` (which is about hosts: who is still
    paused, whether the pin advanced) because this failure strands no host and
    needs no compensating resume. The pin advanced and the code landed; one local
    service process does not exist, and the operator needs its name and the one
    command that fixes it.
    """
    rule = "=" * 64
    print(
        "\n".join(
            [
                "",
                rule,
                "ROLLOUT INCOMPLETE — the code landed; a local service did not come up",
                rule,
                f"  sessions that failed to launch (retried once): {sessions}",
                "  cluster pin: advanced — this host is on the new commit",
                "  manual recovery:",
                "    · `ava start` on this host (idempotent; relaunches only what is missing)",
                "    · the watchdog keepalive also retries it every 60s — `ava status` to confirm",
                rule,
            ]
        ),
        file=sys.stderr,
    )


def _poll_verdict_detail(verdict: PollVerdict) -> str:
    """The sentence a non-converged host gets, keyed on which fact its verdict is.

    One line per verdict because the operator's next move differs: a host still
    converging is *waited* for (the settle hold is doing exactly that), a stalled one
    is looked at. Collapsing both into one "degraded" is what let a rollout in which
    three hosts had given up read the same as one in which three were still working.

    A stalled host also gets *its own* reason, from the `last_updater_outcome` the
    same probe carried. That is the second level of the same argument: "STALLED" is
    correct for both a preflight that refused (host untouched, still serving old
    code — fix what it named and re-run) and an updater that died after moving the
    checkout (host half-transitioned), and those want opposite next actions. Without
    it the only way to tell was to ssh in and read a log on the box — which on the
    Windows runner is the hardest place to do it, and in the refusal case there is
    nothing wrong to find.

    The closing "who fixes this" clause turns on the same fact. Telling every stalled
    host that its watchdog re-triggers the self-update is true of a death and wrong of
    a refusal, and wrong in the expensive direction — it reads as "wait", when a
    refusal is precisely the case that waiting does not clear. A declined host is
    still **paused** (the updater's rc=3 branch deliberately does not run `ava start`,
    which is what unlinks the flag), and `PauseController` blocks the tick with
    `BlockScope.ALL` ahead of `PinController` in the manager's fixed order — so the
    off-pin self-heal never runs while the decline stands. Even once stranded-pause
    recovery unpauses it, that heal converges by POSTing `/api/cluster/update` to the
    gateway, which is the reachability the preflight usually refused over in the first
    place.

    **The other clause needed the same treatment, which is issue #1114.** "Its watchdog
    re-triggers the self-update" was stated unconditionally, to a host this very poll
    has just classified as *still paused* — and the pause gate that blocks a declined
    host's heal blocks a dead updater's heal identically. The verdict is not wrong, it
    was missing its precondition. Naming what lifts the pause (this rollout's own
    compensating resume, in `finalize_rollout` moments from now) points the operator at
    the step that decides whether the promise lands, and at the block that shows up in
    the ops log if it does not. No duration is quoted: the flag stands until nothing
    owns the pause, and this rollout is about to hold the lease over exactly these
    hosts, so any number printed here would be the second wrong promise in one line.
    """
    if verdict.status == POLL_CONVERGING:
        return (
            "still converging — no paused=false before the poll's bound; the cluster "
            "deploy lease stays held while it finishes"
        )
    if verdict.status == POLL_STALLED:
        from ops.updater_outcome import (
            UpdaterOutcome,
            describe_updater_outcome,
            updater_outcome_declined,
        )

        outcome = UpdaterOutcome(**verdict.updater) if verdict.updater else None
        recovery = (
            "its watchdog will NOT self-heal a decline — the pause this left standing "
            "holds the off-pin heal back, and that heal dials the same gateway"
            if updater_outcome_declined(outcome)
            else "its watchdog re-triggers the self-update once the pause is lifted — "
            "this rollout's compensating resume is what normally does that, and until "
            "it lands the watchdog reconciles nothing here"
        )
        return (
            f"STALLED — reachable and still paused with no updater running: "
            f"{describe_updater_outcome(outcome)}. Its full updater log is under "
            f"$AVA_HOME/logs (`updater-<epoch>.log` on POSIX, `ava-updater.out.log` on "
            f"Windows); {recovery}"
        )
    # Phase-B fan-out statuses folded in by the caller ('unreachable' / 'fatal').
    return f"{verdict.status} — never took the self-update op; `ava cluster status` to verify"
