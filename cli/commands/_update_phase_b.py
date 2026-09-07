"""
Phase B of the gateway `ava cluster update` — fan out self-updates, poll back.

Split out of `cli/commands/update.py` to keep that module within the file-size
budget. Phase B tells every remote agent-runner to self-update, then polls each
one's ops server until it reports paused=false, provably stops
(POLL_STALLED), the family's no-progress bound elapses (POLL_CONVERGING), or —
for a host alive and making progress — the converging patience elapses and the
host is handed to the settle hold (C3) — see `shared.deploy_timing` for why
that bound is not a number of its own:
- `PollVerdict` + `_probe_verdict` + `_probe_one_until_unpaused` +
  `_poll_until_unpaused` — the poll loop (unreachable = expected mid-restart
  reading, never a verdict; the early STALLED and NO_PROGRESS exits are what
  make the deadline affordable).
- `_renew_lease_while_polling` — renews the cluster deploy lease beside the
  poll (the long pole) so the lease outlives the operation it protects.
- `_gateway_ready_or_incomplete` — Phase B's gate: the gateway must actually be
  serving the endpoint each runner's own preflight probes.
- `_phase_b_payload` / `_phase_b_and_poll` / `_phase_b_outcome` /
  `_still_converging` — the fan-out + poll composition and the verdict.

Re-imported by `cli/commands/update.py` (and re-exported through `cli.commands`)
so `cli.commands(.update)._poll_until_unpaused` / `._probe_one_until_unpaused` /
`._renew_lease_while_polling` / `._POLL_TIMEOUT_S` / `PollVerdict` /
`POLL_*` keep resolving for tests and the ops controllers.

"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from typing import Any, NamedTuple

from cli.commands._gateway_ready import (
    GatewayReadiness,
    gateway_readiness_detail,
)
from cli.commands._update_fanout import (
    _PHASE_B_TIMEOUT_S,
    ClusterOpPayload,
    _print_fan_out_results,
)
from cli.commands._update_phase_b_capture import capture_host_stages as _capture_host_stages
from cli.commands._update_recover import RolloutOutcome
from shared.deploy_timing import (
    CONVERGING_POLL_TIMEOUT_S,
    HARVEST_GRACE_S,
    LEASE_ARM_GRACE_S,
    PHASE_B_ABSOLUTE_TIMEOUT_S,
    STAGE_NO_PROGRESS_TIMEOUT_S,
)
from shared.host_deploy_state import POSTURE_IDLE, POSTURE_PAUSED, read

_log = logging.getLogger(__name__)

# How long Phase B waits for one agent-runner to come back — the ABSOLUTE deadline
# (the family's no-progress definition, `shared.deploy_timing`), not a number of its
# own: the gateway must not declare a host degraded while that host still believes
# its own updater is slow-but-working, and the previous 120 s — 8x the measured
# POSIX leg but far under a Windows one — is what made the deploy lease expire
# mid-transition. A host that has provably stopped is cut loose in seconds
# regardless, which is what makes a bound this generous affordable
# (`_probe_one_until_unpaused`).
_POLL_TIMEOUT_S = PHASE_B_ABSOLUTE_TIMEOUT_S
_POLL_INTERVAL_S = 2.0
# How long the poll keeps waiting on a host that is ALIVE AND MAKING PROGRESS (its
# updater lease live, its stage evidence moving — the one shape that can otherwise
# spend the whole 900 s bound without proving anything) before it hands the rest of
# that host's convergence to the settle hold that follows the orchestration's exit
# (C3, 2026-08-30: the wsl runner polled 340 probes across 900 s while settle +
# watchdog already covered its convergence). The stalled / no-progress verdicts end
# a host's poll in seconds regardless of either bound, and `_POLL_TIMEOUT_S` stays
# the deadline for every host — this only spends less of it. A module-level alias
# read through the `cli.commands` namespace so tests can shrink it, exactly like
# `_POLL_TIMEOUT_S`.
_CONVERGING_TIMEOUT_S = CONVERGING_POLL_TIMEOUT_S
# How long one updater stage may be in flight before a probe's own evidence proves
# no-progress (the family's STAGE_NO_PROGRESS_TIMEOUT_S — see shared.deploy_timing).
# A module-level alias read through the `cli.commands` namespace so tests can shrink
# it, exactly like _POLL_TIMEOUT_S.
_STAGE_NO_PROGRESS_S = STAGE_NO_PROGRESS_TIMEOUT_S
# Alias of the deploy family's lease-arm grace — a registered clock
# (shared.deploy_timing); the poll's own elapsed clock anchors it (branch above).
_LEASE_ARM_GRACE_S = LEASE_ARM_GRACE_S


# Phase-B poll verdicts. Four facts, deliberately not one word: 'the poll ran out
# of patience', 'this host provably stopped trying' and 'this host's updater is
# alive but its stage evidence stopped moving' need different operator responses
# (wait / go look / check that machine), and none of them is 'the rollout finished
# cleanly'.
POLL_OK = "ok"  # reported paused=false: converged
POLL_CONVERGING = "converging"  # patience ran out while it may still be working
POLL_STALLED = "stalled"  # reachable, still paused, no updater running: not coming back
POLL_NO_PROGRESS = "no_progress"  # updater alive, but its stage evidence has stopped moving

# R1 (Task #1021): the stall fact is the host_deploy_state row, not a wire field.
# The probe's `paused` alone cannot carry it — the updater's lease touch moves
# posture to `converging` (read as "not paused") while the host is still
# mid-transition, and a declined restart leaves the host stuck on old code with
# no lease at all. The DB row is the authority:
# - posture `idle` (or no row) -> converged;
# - a LIVE updater lease -> still working, keep polling;
# - `paused` with NO lease -> the fan-out window while inside the arm grace,
#   a provable stop past it (the updater's clear ran without reaching idle);
# - `paused` with an EXPIRED lease, or `converging` with no live lease (the
#   updater finished or aborted and the host did not return to idle) -> the
#   provable-stall fact, confirmed `_STALL_CONFIRMATIONS` times.
# The two "keep polling" rows are lease readings, and a lease that is never
# cleared claims liveness for as long as this poll would wait — so a terminal
# `last_updater_outcome` (the updater wrote its own ending) overrides both.

# How many consecutive stall observations end the poll for a host. Two, not one: the
# op acks after the updater session is spawned, so a single contrary reading is a
# teardown/handoff race rather than evidence, and abandoning a host on it would be
# the same too-eager verdict this rewrite exists to remove — one interval apart it
# costs `_POLL_INTERVAL_S`, not a rollout.
_STALL_CONFIRMATIONS = 2


class PollVerdict(NamedTuple):
    """One host's terminal poll state, and the reason behind it when there is one.

    `status` is a POLL_* above (or a Phase-B fan-out status the caller folds in).
    `updater` is the `last_updater_outcome` the host reported on the probe that
    settled the verdict — the fact that turns `POLL_STALLED` from "this host
    stopped" into "this host *refused*, and is still serving" or "its updater died
    after moving the checkout". Those need opposite next actions and used to read
    identically (#995).

    None whenever the host did not send one: an older commit answering mid-rollout
    (the field did not exist), a host that converged normally, or one whose log did
    not speak for this update. "No record" is reported as itself, never as rc=0.
    """

    status: str
    updater: dict[str, Any] | None = None


def _probe_verdict(
    result: dict[str, Any] | None,
    stalls: int,
    machine: str,
    no_progress: int,
    *,
    poll_elapsed: float = 0.0,
) -> tuple[PollVerdict | None, int, int, bool]:
    """One status_probe response + this host's deploy-state row →
    (verdict, stalls, no_progress, progressing).

    Verdict None = keep polling. A non-dict result (unreachable / failed are
    swallowed by the caller and leave result None) clears both counters — silence is
    the expected mid-restart reading, never evidence. The verdict itself reads the
    host_deploy_state row (the R1 authority), not the probe's `paused` field: posture
    `idle` is convergence; a live updater lease is "still working"; `paused` with no
    lease is the fan-out window inside the arm grace and a provable stop past it
    (`_LEASE_ARM_GRACE_S`, the updater's clear ran without reaching idle); `paused`
    with an expired lease or `converging` with no live lease is likewise the
    provable-stall fact, confirmed `_STALL_CONFIRMATIONS` times before it ends the
    poll. A row read failure is "cannot tell" (fail-soft — the poll keeps its
    deadline, and neither counter advances).

    **The updater's own written verdict overrides both "keep polling" readings.**
    Those two read the lease, and a lease is one write at the run's start good for
    `UPDATER_LEASE_TTL_S` — the same 15 minutes this poll is willing to wait — so
    every way of not reaching its clear step (a chain that exits first, a clear that
    cannot reach the DB through the restart it is part of, a killed process) is a
    host that stopped in seconds and reads as busy for the whole bound. That is not
    a rare shape: it is what a *failing* Windows update looked like on prod, and it
    put the expensive reading on exactly the runs that had already failed. A
    terminal `last_updater_outcome` (`updater_outcome_terminal`) is the independent
    proof — the updater wrote its own ending — and it is only consulted once posture
    has already said this host is not converged.

    **The no-progress fact (P1, 2026-08-30) is the live lease's missing half.** A
    lease that never gets cleared reads as "still working" for the whole bound even
    while the updater's own stage evidence shows it has been stuck inside one stage —
    a hung `uv` download on the Windows runner is exactly that shape. The probe's
    `last_updater_outcome` now carries `current_stage` / `current_stage_s` (the
    tail's last `t=` marker and its age at read time — see `ops.updater_outcome`), so
    the poll can read progress itself instead of trusting the lease alone:
    consecutive probes reporting a current stage in flight beyond
    `STAGE_NO_PROGRESS_TIMEOUT_S` end the poll with POLL_NO_PROGRESS — the same
    bound, and the same evidence, the host's own reaper uses. A probe answering from
    an older commit carries no stage fields, which reads as "cannot tell", never as
    progress, so the judgment simply waits for the rollout that ships it.

    The verdict carries the host's `last_updater_outcome` from *this same
    probe response*, which is why the reason costs no extra dial: the probe that
    proves the host stopped is the probe that says why.

    **The fourth return value is the converging-patience fact (C3).** `progressing`
    is True exactly when this reading is "alive and making progress" — a live
    updater lease whose stage evidence is not stuck (an older commit's missing
    stage fields read as "cannot tell", which is NOT progress and resets the
    streak, mirroring the no-progress rule's polarity). The caller uses it to
    bound how long it keeps waiting on that shape before handing the host to the
    settle hold; every other reading (unreachable, stalled evidence, a paused
    pre-lease window, a DB read failure) is False, so a restart or a stall resets
    the clock rather than letting stale progress keep counting.
    """
    import cli.commands as _ns
    from ops.updater_outcome import UpdaterOutcome, updater_outcome_terminal

    if not isinstance(result, dict):
        return None, 0, 0, False
    try:
        state = read(machine)
    except Exception:
        # "cannot tell", never convergence: reporting a host converged because
        # its row could not be read would release the deploy lease while the
        # host is still mid-transition — the wrong polarity. Keep polling, and
        # keep the no-progress streak too: an unreadable row is evidence-free.
        return None, stalls, no_progress, False
    if state is None or state.posture == POSTURE_IDLE:
        return PollVerdict(POLL_OK), stalls, no_progress, False
    raw = result.get("last_updater_outcome")
    outcome = raw if isinstance(raw, dict) else None
    try:
        parsed = UpdaterOutcome.model_validate(outcome) if outcome else None
        finished = updater_outcome_terminal(parsed)
    except Exception:
        # Read leniently, and never raise: this runs inside the gathered poll, so an
        # exception here abandons every host's poll, not just this one's. The runner
        # answering is on a different commit from this orchestrator for the whole
        # rollout by construction, so a field it phrases differently costs this pass
        # its short-cut and nothing more.
        parsed = None
        finished = False
    if not finished:
        if state.updater_live:
            # lease live: the updater is still working — reset the stall counter.
            # But "working" is the lease's claim, and the lease is one write at the
            # run's start; the stage evidence is the progress fact. Two consecutive
            # probes naming a stage that has been in flight past the family's stage
            # bound (an age that only grows while the same stage runs — a stage
            # change resets it) prove the updater is not getting anywhere.
            if (
                parsed is not None
                and parsed.current_stage is not None
                and parsed.current_stage_s is not None
                and parsed.current_stage_s > _ns._STAGE_NO_PROGRESS_S
            ):
                no_progress += 1
                if no_progress >= _STALL_CONFIRMATIONS:
                    return PollVerdict(POLL_NO_PROGRESS, outcome), stalls, no_progress, False
                # One stuck-stage reading is not yet the verdict, but it is also
                # not progress — the converging clock must not keep counting while
                # the no-progress streak is being confirmed.
                return None, 0, no_progress, False
            no_progress = 0
            # Progress is the STAGE EVIDENCE, not the lease's claim: a lease is
            # one write at the run's start, so a live lease alone cannot prove a
            # host is getting anywhere. The converging clock (C3) counts only on
            # a reading that carries a stage in flight below the stuck bound; a
            # probe from an older commit with no stage fields is "cannot tell"
            # (never progress), exactly like the no-progress rule's polarity.
            if parsed is None or parsed.current_stage is None or parsed.current_stage_s is None:
                return None, 0, no_progress, False
            return None, 0, no_progress, True
        if (
            state.posture == POSTURE_PAUSED
            and not state.updater_expired
            and poll_elapsed < _LEASE_ARM_GRACE_S
        ):
            # Paused with no lease inside the arm grace: the updater has not
            # armed yet; past the grace it is a provable stop, never a wait.
            return None, 0, 0, False
    stalls += 1
    if stalls >= _STALL_CONFIRMATIONS:
        return PollVerdict(POLL_STALLED, outcome), stalls, no_progress, False
    return None, stalls, no_progress, False


async def _probe_one_until_unpaused(
    name: str,
    deadline: float,
    ops_url: str | None = None,
    host_outcomes: dict[str, dict[str, object]] | None = None,
) -> tuple[str, PollVerdict]:
    """Repeatedly POST status_probe to one agent-runner's ops server until it
    converges, provably stops, the shared deadline expires — or, for a host that
    has been alive and making progress for `_CONVERGING_TIMEOUT_S` straight, the
    converging-patience bound ends the poll early and hands the host to the
    settle hold (C3). Returns (name, verdict) — one of the four POLL_* above.

    `host_outcomes` (optional out-dict) collects the host's updater stage times
    from every probe response that carried them (Task #1820): each response's
    `last_updater_outcome.stages` is written under `name`, keeping the LAST
    non-empty reading — a mid-run probe catches the growing partial breakdown,
    and the final converged probe reports none (an idle host has no pause anchor),
    so the last non-empty one is the best snapshot the poll saw.

    Each probe carries one short timeout; an unreachable ops server means the
    agent-runner is mid-restart — `ops` is itself a service its own self-update
    stops, so silence is the *expected* reading through the middle of a healthy leg
    and is never a verdict. On that the loop simply retries until the deadline.
    `ops_url` (pre-resolved) keeps the probe from re-querying Postgres each pass.

    **The early `POLL_STALLED` exit is what makes the deadline affordable.** A host
    whose deploy-state row says "still mid-transition, and no live updater lease"
    has lost its updater without resuming: its checkout moved, its processes did
    not, and no amount of further waiting changes that — the forward path is its
    watchdog's re-trigger or an operator. Measured on prod: three consecutive rollouts marked
    a runner degraded after burning the full poll on it, when its updater had in
    fact exited in ~3 s (`updater-*.log` rc=3, preflight refused the restart because
    the gateway was not yet reachable). Waiting out a bound for a host that has
    already given up is what forced the bound to stay small, which is what made it
    expire under the hosts that genuinely needed it.
    """
    import cli.commands as _ns
    from ops import cluster_rpc as cr

    stalls = 0
    no_progress = 0
    # The moment this host was last read as "alive and making progress" (C3), or
    # None while the current reading is anything else. The converging bound is
    # measured on the CONTINUOUS streak: a restart (unreachable) or a stall
    # reading resets it, so the 300 s is patience spent on a host that keeps
    # proving it is working — not total poll time. `_POLL_TIMEOUT_S` stays the
    # absolute deadline either way.
    progressing_since: float | None = None
    started = time.monotonic()
    attempt = 0
    while time.monotonic() < deadline:
        slice_timeout = min(_ns._POLL_INTERVAL_S, deadline - time.monotonic())
        if slice_timeout <= 0:
            break
        attempt += 1
        attempt_started = time.monotonic()
        result: object | None = None
        outcome = "completed"
        try:
            result = await cr.dispatch_to_machine(
                target_machine=name,
                kind="status_probe",
                payload={},
                timeout_s=slice_timeout,
                ops_url=ops_url,
                # This loop is the retry policy. Letting cluster_rpc apply its
                # default four-attempt policy turns one two-second poll into an
                # opaque ~12-second nested retry and can enqueue several full
                # status snapshots on a runner that has only just restarted.
                retries=0,
            )
        except cr.ClusterOpUnreachable:
            # An unreachable ops server IS the expected mid-restart reading — `ops` is
            # a service its own self-update stops — so it is deliberately neither a
            # verdict nor evidence. `result` stays None and the loop retries.
            outcome = "unreachable"
        except cr.ClusterOpFailed:
            # The op ran but could not resolve status (also mid-restart). Same reading
            # as unreachable, and for the same reason.
            outcome = "failed"
        _log.info(
            "phase_b_probe machine=%s attempt=%d outcome=%s rpc_ms=%.1f elapsed_s=%.1f",
            name,
            attempt,
            outcome,
            (time.monotonic() - attempt_started) * 1000,
            time.monotonic() - started,
        )
        _capture_host_stages(host_outcomes, name, result)
        verdict, stalls, no_progress, progressing = _probe_verdict(
            result, stalls, name, no_progress, poll_elapsed=time.monotonic() - started
        )
        if verdict is not None:
            if (
                verdict.status == POLL_OK
                and host_outcomes is not None
                and (name not in host_outcomes or "start" not in host_outcomes[name])
            ):
                # One harvest re-probe after a short grace: the updater's final
                # `start` stage line lands after the posture row goes idle (a
                # convergence probe can beat it by milliseconds), and a host that
                # converged between probes carried no stages at all. The
                # fresh-idle read in `ops.updater_outcome` serves both, given the
                # moment for the last line to land. Best-effort: a failed or
                # empty harvest changes nothing about the verdict.
                await asyncio.sleep(HARVEST_GRACE_S)
                try:
                    harvest = await cr.dispatch_to_machine(
                        target_machine=name,
                        kind="status_probe",
                        payload={},
                        timeout_s=1.0,
                        ops_url=ops_url,
                        retries=0,
                    )
                except (cr.ClusterOpUnreachable, cr.ClusterOpFailed):
                    harvest = None
                _capture_host_stages(host_outcomes, name, harvest)
            print(
                f"  · {name}: Phase B {verdict.status} after {attempt} probe(s) "
                f"in {time.monotonic() - started:.1f}s",
                file=sys.stderr,
            )
            return name, verdict
        # The converging-patience bound (C3): a host that keeps reading as alive
        # and making progress for `_CONVERGING_TIMEOUT_S` straight is handed to
        # the settle hold instead of polling out the full 900 s deadline — the
        # remaining convergence is exactly what the hold + watchdog cover. The
        # verdict is the same POLL_CONVERGING the deadline produces, so the
        # settle set, the report and the compensating resume need no branching.
        now = time.monotonic()
        if progressing:
            if progressing_since is None:
                progressing_since = now
            elif now - progressing_since >= _ns._CONVERGING_TIMEOUT_S:
                print(
                    f"  · {name}: Phase B {POLL_CONVERGING} after {attempt} probe(s) "
                    f"in {now - started:.1f}s (alive and making progress past the "
                    f"converging bound — the settle hold takes over)",
                    file=sys.stderr,
                )
                return name, PollVerdict(POLL_CONVERGING)
        else:
            progressing_since = None
        # Pace the loop explicitly. `slice_timeout` bounds one *dial*, not one pass:
        # a host that answers instantly (which a paused-but-reachable one does) would
        # otherwise be re-dialled as fast as the event loop allows for the whole
        # bound — a hot loop against a host that is mid-restart, and one that starves
        # the lease-renewal task sharing this event loop of its turn.
        await asyncio.sleep(min(_ns._POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))
    print(
        f"  · {name}: Phase B {POLL_CONVERGING} after {attempt} probe(s) "
        f"in {time.monotonic() - started:.1f}s",
        file=sys.stderr,
    )
    return name, PollVerdict(POLL_CONVERGING)


async def _renew_lease_while_polling(holder: str) -> None:
    """Prove this orchestration is still executing, every `LEASE_RENEW_INTERVAL_S`, for
    as long as the caller keeps this task alive — to the deploy lease, and to the
    rollout log.

    Runs beside the Phase-B poll because the poll is the long pole: it is the phase
    that waits on other machines, and the phase whose bound just grew from a
    POSIX-era 120 s to the family's no-progress definition. Renewing here is what
    keeps **the lease outliving the operation it protects** independent of how long
    the operation takes — the invariant this whole family exists to hold. Cancelled
    (never awaited to completion) by the poll's `finally`, so the renewal stops the
    moment the orchestration stops executing and a crashed rollout still lapses.

    **The printed half is the same claim, written where an outside observer can read
    it.** `_probe_one_until_unpaused` narrates nothing per pass on purpose — one line
    per host per two seconds would bury the log — so Phase B was a legitimately silent
    stretch of up to `_POLL_TIMEOUT_S`, which is the *same* number
    `ops.controllers.stalled_rollout` reads log silence against before declaring the
    orchestration hung. A healthy slow fan-out would have sat exactly on that boundary.
    One line per renewal interval keeps the log's quietest healthy phase an order of
    magnitude inside the bound, and it costs nothing on the fast path: the poll returns
    the instant every host converges, usually before the first line is due.

    Never raises out: a DB hiccup here must not abort a rollout that is otherwise
    fine. `renew_update_lock` already logs a failed round at WARNING, and a missed
    round is survivable precisely because `LOCK_TTL_S` is many intervals wide.
    """
    from shared.cluster_lock import renew_update_lock
    from shared.deploy_timing import LEASE_RENEW_INTERVAL_S

    waited = 0.0
    while True:
        await asyncio.sleep(LEASE_RENEW_INTERVAL_S)
        waited += LEASE_RENEW_INTERVAL_S
        # Printed before the renewal, not after: a DB that has gone away is exactly
        # when the lease cannot be re-armed, and it is also exactly when the log line
        # saying this process is still alive matters most.
        print(f"  · still polling Phase B ({waited / 60:.0f}m)", file=sys.stderr)
        try:
            await asyncio.to_thread(renew_update_lock, holder)
        except Exception as exc:
            print(f"  · lease renewal round failed ({exc!r}); retrying", file=sys.stderr)


def _poll_until_unpaused(
    agent_runners: list[tuple[str, str | None]],
    host_outcomes: dict[str, dict[str, object]] | None = None,
) -> dict[str, PollVerdict]:
    """Poll each agent-runner's paused state via direct status_probe POSTs to its
    ops server until it converges, provably stops, `_POLL_TIMEOUT_S` elapses, or
    `_CONVERGING_TIMEOUT_S` of continuous progress hands a host to the settle
    hold early (C3).

    Returns name -> verdict (one of the POLL_* above). The `gateway_url` field
    in the input tuple is unused but kept for caller-signature compatibility.
    `host_outcomes`, when given, is filled per host with the updater stage times
    the probes carried (see `_probe_one_until_unpaused`).

    The lease renewal rides here rather than in the caller because this is the one
    stretch of the orchestration long enough to matter, and because the holder is
    derivable (`self_holder()`) from the process running it — the poll and the
    `acquire_update_lock` that opened the rollout are the same pid, so nothing has
    to be threaded down four frames to keep the protection alive.
    """
    # Dynamic lookup for the timeout constant + probe helper so tests can
    # shrink them.
    import cli.commands as _ns
    from shared.cluster_lock import self_holder

    deadline = time.monotonic() + _ns._POLL_TIMEOUT_S
    holder = self_holder()

    async def _run() -> dict[str, PollVerdict]:
        renewal = asyncio.ensure_future(_renew_lease_while_polling(holder))
        try:
            tasks = [
                _ns._probe_one_until_unpaused(
                    name, deadline, ops_url=url, host_outcomes=host_outcomes
                )
                for name, url in agent_runners
            ]
            return dict(await asyncio.gather(*tasks))
        finally:
            # Cancel AND await: `asyncio.run` closes the loop as soon as this returns,
            # and a cancelled-but-unawaited task there surfaces as a "Task was
            # destroyed but it is pending" warning on the rollout's own log — noise
            # in exactly the place an operator reads to find real problems.
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal

    return asyncio.run(_run())


def _gateway_ready_or_incomplete(
    fanout_targets: list[tuple[str, str | None]],
    paused_names: set[str],
    unconverged: list[str] | None,
) -> bool:
    """Phase B's gate: True when the gateway is serving and the fan-out may proceed.

    Every agent-runner's self-update begins by proving it can still reach the gateway
    before it stops anything (validate-before-kill), so a runner told to update while
    the gateway is not yet serving correctly *declines* — three prod rollouts' worth of
    "slow hosts" were in fact hosts that had refused in ~3 s with `gateway
    unreachable`. The local update returning 0 does not mean the gateway is serving:
    its `ava start` child's own readiness wait is 15 s and non-fatal. So the wait
    belongs here, once, against the same URL + endpoint + headers each runner will use.
    `cli/commands/_gateway_ready.py` carries the full reasoning — including why a
    bounded retry inside the runner's preflight is the weaker fix.

    On a non-SERVING verdict this fills `unconverged` with the hosts **Phase A
    paused**, so the caller's `finally` converts the lease into a settle hold over
    exactly them. That is the one place the settle set cannot come from a Phase-B ack,
    and the acked-only rule survives in the same form: only hosts this rollout actually
    put into a transition are held for, so a powered-off runner still cannot make every
    rollout idle out a settle window. The verdict is `INCOMPLETE`, never ABORTED — the
    gateway migrated and the pin advanced — and never CLEAN.

    `fanout_targets` excludes this host (`_phase_b_targets`), so on a single box the
    list is empty and there is no dependent to strand. The gate is still asked, because
    the local leg started the gateway with `--no-readiness-gate` — this is the only
    thing that checks the box came back serving, and a rollout whose gateway never
    rebound must not report CLEAN.
    """
    import cli.commands as _ns

    readiness, detail = _ns._await_gateway_serving()
    if readiness is GatewayReadiness.SERVING:
        return True
    if unconverged is not None:
        unconverged.extend(sorted(paused_names))
    left_behind = (
        f"{len(fanout_targets)} agent-runner(s) stay on the previous commit against the "
        f"migrated schema; none was told to self-update, so none is mid-checkout."
        if fanout_targets
        else "This host was the only rollout target, so no other host is affected — but "
        "its own gateway is not serving."
    )
    print(
        f"\n✗ rollout incomplete: Phase B not started — "
        f"{gateway_readiness_detail(readiness, detail)}. {left_behind}",
        file=sys.stderr,
    )
    return False


def _phase_b_payload(
    *, restart_only: bool, target_sha: str | None, force_reap: bool = False, mode: str = "smooth"
) -> ClusterOpPayload | None:
    """The optional params a Phase-B cluster op carries: `restart_only` (a
    restart-only bounce vs a full self-update), `target_sha` (the pinned rollout
    commit every node checks out), `mode` (the host updater's stop policy), and
    legacy `force_reap` (explicit interruption). Phase B verifies the existing
    drain completed by Phase A. None when all fields are absent."""
    payload: ClusterOpPayload = {}
    if restart_only:
        payload["restart_only"] = True
    if target_sha is not None:
        payload["target_sha"] = target_sha
    if mode != "smooth":
        payload["mode"] = mode
    if force_reap:
        payload["force_reap"] = True
    return payload or None


def _phase_b_and_poll(
    fanout_targets: list[tuple[str, str | None]],
    *,
    target_sha: str | None,
    restart_only: bool,
    force_reap: bool = False,
    mode: str = "smooth",
    host_outcomes: dict[str, dict[str, object]] | None = None,
) -> dict[str, PollVerdict]:
    """Phase B + poll: fan out each agent-runner's self-update (or restart-only
    bounce), then poll each back to healthy; return name -> terminal poll state
    ('ok' | 'degraded'). Each host's own `ava start` unlinks its flag (the natural
    resume, which also finalizes the host's pause-owner journal — a converged
    host must not keep a `paused` journal); the caller resumes whichever the poll
    still reports non-'ok'. No abort on a Phase-B 5xx (already migrated); a failed
    host is only marked degraded.

    `host_outcomes` is filled per host with the updater-stage breakdown plus its
    terminal wall time and outcome, when the poll captured them.

    `target_sha` (the pinned rollout commit) rides each host's `cluster_update`
    payload so every agent-runner force-checks-out the *same* commit the gateway
    did — not its own re-resolution of origin/main.

    `fanout_targets` is the rollout list minus this host (`_phase_b_targets`), so on a
    single box it is empty and Phase B is a no-op — the local leg already did the work
    this phase exists to dispatch.
    """
    import cli.commands as _ns

    if not fanout_targets:
        print(
            "\n→ Phase B: nothing to trigger — this host was the whole rollout and its "
            "local leg already checked it out, migrated it and restarted it"
        )
        return {}
    label = "restart" if restart_only else "self-update"
    print(f"\n→ Phase B: trigger {len(fanout_targets)} agent-runner {label}")
    results = _ns._fan_out(
        fanout_targets,
        "/api/cluster/update",
        _PHASE_B_TIMEOUT_S,
        payload=_phase_b_payload(
            restart_only=restart_only,
            target_sha=target_sha,
            force_reap=force_reap,
            mode=mode,
        ),
    )
    _print_fan_out_results("self-update spawn", results)
    # Poll only the hosts whose Phase-B spawn acked: a host that never received
    # the op (unreachable / 5xx) cannot reach paused=false through this update, so
    # polling it would just burn the full _POLL_TIMEOUT_S per host. Carry the
    # non-acked hosts straight into the result as still-paused — the caller's
    # compensating resume must still cover any that Phase A paused (resume coverage
    # is unchanged); their forward path is the watchdog re-trigger on return.
    acked_names = {name for name, status, _ in results if status == "ok"}
    to_poll = [(name, url) for name, url in fanout_targets if name in acked_names]
    print(
        f"\n→ Poll {len(to_poll)} acked agent-runner(s) until /api/cluster/status reports "
        f"paused=false (up to {_POLL_TIMEOUT_S / 60:.0f}m each — or "
        f"{_CONVERGING_TIMEOUT_S / 60:.0f}m of continuous progress, C3 — cut short the "
        f"moment a host provably stops)"
    )
    polls = _ns._poll_until_unpaused(to_poll, host_outcomes=host_outcomes)
    for name, status, _ in results:
        if status != "ok":
            polls.setdefault(name, PollVerdict(status))
    _print_poll_verdicts(polls)
    return polls


def _still_converging(polls: dict[str, PollVerdict]) -> list[str]:
    """The agent-runners left mid-transition now that the orchestration is done — the
    hosts whose Phase-B spawn was **acked** but which never reported `paused=false`.

    Every non-OK poll verdict qualifies, and for the same reason: whether the host is
    still working (`POLL_CONVERGING`), has provably stopped (`POLL_STALLED`), or is
    alive but stuck inside one stage (`POLL_NO_PROGRESS`), its checkout has moved and
    its processes have not, which is exactly the window a second deploy must not
    start into. The verdicts differ in what an operator does next, not in whether the
    cluster is mid-transition — so they differ in the report (`_poll_verdict_detail`)
    and not here.

    Acked-only is load-bearing — `_phase_b_and_poll` polls only acked hosts and folds
    the rest back in under their fan-out status (`'unreachable'` / `'fatal'`) — because
    a host that never acked never began transitioning, so a decommissioned or
    permanently-offline agent-runner must not hold the whole cluster for a settle
    window on every rollout.
    """
    return [
        name
        for name, verdict in polls.items()
        if verdict.status in (POLL_CONVERGING, POLL_STALLED, POLL_NO_PROGRESS)
    ]


def _phase_b_outcome(
    fanout_targets: list[tuple[str, str | None]],
    *,
    target_sha: str | None,
    restart_only: bool,
    runner_urls: dict[str, str | None],
    unconverged: list[str] | None,
    force_reap: bool = False,
    host_outcomes: dict[str, dict[str, object]] | None = None,
) -> tuple[int, RolloutOutcome, list[tuple[str, str | None]]]:
    """Phase B + poll, then the verdict.

    Returns (rc, outcome, hosts_to_resume) — outcome and hosts_to_resume feed
    the caller's compensating `finally`, which must run on every path. A poll
    that gave up on an acked host is not a clean finish, and the rollout must
    not report one. Only the hosts that *took* the op count: a host that was
    unreachable for the Phase-B fan-out never began transitioning (its forward
    path is the watchdog re-trigger on return), and letting a powered-off
    laptop fail every rollout would destroy the signal this distinction
    creates.

    This host is not in `fanout_targets` (`_phase_b_targets`) and so appears in
    neither the polls nor `hosts_to_resume`. It does not need to: the caller's
    `finally` unpauses it locally on every path.
    """
    from cli.commands import update as _up_mod

    polls = _up_mod._phase_b_and_poll(
        fanout_targets,
        target_sha=target_sha,
        restart_only=restart_only,
        force_reap=force_reap,
        host_outcomes=host_outcomes,
    )
    mid_transition = _up_mod._still_converging(polls)
    if unconverged is not None:
        unconverged.extend(mid_transition)
    # The hosts the poll still reports paused did not self-resume (each host's
    # own update unlinks its flag — the natural resume), and a paused host's
    # watchdog will not self-heal it, so the finally must. Carry each host's
    # pre-resolved ops URL through so the compensating resume stays
    # Postgres-free.
    hosts_to_resume = [
        (name, runner_urls.get(name))
        for name, verdict in polls.items()
        if verdict.status != POLL_OK
    ]
    if mid_transition:
        outcome = RolloutOutcome.INCOMPLETE
        # "did not come back within the poll window", never "never came back": a
        # CONVERGING host handed to the settle hold is still working (C3), and
        # reporting it as gone would misread the poll's early exit as a failure
        # of the host. The per-verdict detail lines below say which is which.
        print(
            f"\n✗ rollout incomplete: {len(mid_transition)} of {len(polls)} agent-runner(s) "
            f"acked the self-update and did not come back within the poll window "
            f"({', '.join(sorted(mid_transition))}). "
            f"The gateway migrated and the pin advanced; those hosts have not — the "
            f"deploy lease stays held over them while they settle.",
            file=sys.stderr,
        )
        return 1, outcome, hosts_to_resume
    return 0, RolloutOutcome.CLEAN, hosts_to_resume


def _print_poll_verdicts(polls: dict[str, PollVerdict]) -> None:
    """One line per polled host: ✓ back up, or ⚠ with the verdict's next-step
    sentence (`_poll_verdict_detail`) on stderr."""
    import cli.commands as _ns

    for name, verdict in polls.items():
        if verdict.status == POLL_OK:
            print(f"  ✓ {name}: back up")
        else:
            print(f"  ⚠ {name}: {_ns._poll_verdict_detail(verdict)}", file=sys.stderr)
