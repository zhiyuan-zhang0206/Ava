"""The rollout's closing section: fan-out set -> readiness -> Phase-B poll -> commit.

Split out of `cli/commands/update.py` (file-size budget) -- the largest single
contiguous block of `_run_gateway_orchestration_inner`. It runs after the
gateway local leg and decides the rollout's return: pick the Phase-B fan-out
set (6.4), ask the readiness gate (6.5), run the Phase-B poll + verdict (7-8),
then consume the enable point's decision at the managed-writer collection
(8.5, task #4128 E2-c) and P5 commit (9, task #4128 E2-a).

The five orchestration seams arrive as injected callables, resolved at the
call site from `update.py`'s namespace so the `cli.commands.update.*`
monkeypatch seams keep resolving for tests:

- `targets` -- `_phase_b_targets` (the fan-out set, this host excluded),
- `readiness` -- `_gateway_ready_or_incomplete` (Phase B's precondition),
- `poll_outcome` -- `_phase_b_outcome` (the poll + verdict),
- `collect` -- `_collect_managed_writer_publication` (the P2 collection),
- `commit` -- `_commit_managed_writer_publication` (the P5 commit).

Every exit is a `PhaseBVerdict` rather than a bare rc: the caller still reports
the rollout's aftermath through its compensating `finally`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from cli.commands._update_recover import RolloutOutcome
from shared.rollout_telemetry import record_host as _record_host_telemetry
from shared.rollout_telemetry import stage as _stage_telemetry


class PhaseBVerdict(NamedTuple):
    """What the closing section decided, handed back to the caller's `finally`.

    `failing_step` is an override only: None means nothing inside this section
    failed and the caller keeps its own (the local leg's defect, set before the
    call). `publication_refused` marks a managed-writer publication refusal
    (collection or commit) whose pending journal was retained for checked
    recovery.
    """

    rc: int
    outcome: RolloutOutcome
    hosts_to_resume: list[tuple[str, str | None]]
    failing_step: str | None
    publication_refused: bool


def _phase_b_and_commit(
    agent_runners: list[tuple[str, str | None]],
    *,
    paused_names: set[str],
    unconverged: list[str] | None,
    target_sha: str | None,
    restart_only: bool,
    runner_urls: dict[str, str | None],
    force_reap: bool,
    local_launch_failures: list[str],
    hosts_to_resume: list[tuple[str, str | None]],
    targets: Callable[[list[tuple[str, str | None]]], list[tuple[str, str | None]]],
    readiness: Callable[[list[tuple[str, str | None]], set[str], list[str] | None], bool],
    poll_outcome: Callable[..., tuple[int, RolloutOutcome, list[tuple[str, str | None]]]],
    collect: Callable[[], int],
    commit: Callable[[], int],
) -> PhaseBVerdict:
    """Steps 6.4 through 9 of the rollout: fan-out set, readiness gate, Phase-B
    poll + verdict, managed-writer collection + commit -- returning the verdict
    to report.

    The five injected seams are the real functions in production; tests patch
    them on `update.py`, and the call site resolves each name there.
    """
    # 6.4) Who Phase B actually fans out to: every rollout target except THIS
    #      host. A co-located gateway,agent-runner box was updated by the local
    #      leg above, and its redundant self-update would kill the gateway the
    #      readiness gate is about to bless — see `_phase_b_targets`. Phases 0
    #      and A keep the full list on purpose: their ops are idempotent with
    #      the local work, Phase B's is not.
    fanout_targets = targets(agent_runners)

    # 6.5) Phase B's precondition, checked instead of assumed (see
    #      `_gateway_ready_or_incomplete`). A non-SERVING gateway skips the fan-out
    #      entirely and reports INCOMPLETE rather than letting every runner decline.
    #      Still asked when this host is the only target: the local leg's `ava start`
    #      runs with `--no-readiness-gate`, so skipping here would leave a single-box
    #      rollout with the readiness question asked nowhere at all.
    with _stage_telemetry("readiness"):
        gateway_serving = readiness(fanout_targets, paused_names, unconverged)
    if not gateway_serving:
        return PhaseBVerdict(
            rc=1,
            outcome=RolloutOutcome.INCOMPLETE,
            hosts_to_resume=hosts_to_resume,
            failing_step="the gateway was not serving, so Phase B never fanned out",
            publication_refused=False,
        )

    # 7-8) Phase B + poll + verdict; hosts still mid-transition keep the lease
    #      as a settle hold. outcome / hosts_to_resume ride the verdict back to
    #      the caller so its `finally` reports the true aftermath, not the
    #      ABORTED default.
    # Per-host updater stage times, gathered by the Phase-B poll from the
    # `last_updater_outcome` each status probe carried; a converged host is
    # re-probed once (the fresh-idle read in `ops.updater_outcome` serves
    # its completed breakdown, `start` included). Land in the telemetry
    # summary so one rollout log shows every host's checkout/uv/stop/start.
    host_outcomes: dict[str, dict[str, object]] = {}
    # An override only; None keeps the caller's own failing_step.
    failing_step: str | None = None
    with _stage_telemetry("phase_b"):
        rc, outcome, hosts_to_resume = poll_outcome(
            fanout_targets,
            target_sha=target_sha,
            restart_only=restart_only,
            runner_urls=runner_urls,
            unconverged=unconverged,
            force_reap=force_reap,
            host_outcomes=host_outcomes,
        )
    for _host, _stages in host_outcomes.items():
        _record_host_telemetry(_host, _stages)
    if outcome is not RolloutOutcome.CLEAN:
        failing_step = "the Phase-B poll: acked agent-runners never reported back"
    elif local_launch_failures:
        # Every agent-runner converged, so Phase B has nothing to report — but
        # this host is short a service and the rollout is not clean. `failing_step`
        # already names the sessions (set right after the local leg; None here
        # keeps it) and the aftermath block lists them.
        outcome, rc = RolloutOutcome.INCOMPLETE, 1

    # 8.5-9) The managed-writer publication window (task #4128), reached only
    #    by a clean, non-restart-only rollout. The steps themselves consume
    #    the enable point's decision: under `active`, the collection (E2-c)
    #    first adopts the completed units' post-stop facts and the P5 commit
    #    (E2-a) then publishes them through their seats; every other decision
    #    skips the window without touching the publication seats. A refusal
    #    fails the rollout; the pending journal stays for checked recovery.
    if outcome is RolloutOutcome.CLEAN and not restart_only:
        collect_rc = collect()
        if collect_rc != 0:
            failing_step = (
                "the managed-writer collection refused; the pending journal "
                "remains for `ava cluster recover-pending`"
            )
            # The units converged and the pin advanced, but the collection
            # was never adopted: the record and the aftermath must read
            # INCOMPLETE, never the CLEAN this rollout still carried.
            outcome, rc = RolloutOutcome.INCOMPLETE, collect_rc
            return PhaseBVerdict(
                rc=rc,
                outcome=outcome,
                hosts_to_resume=hosts_to_resume,
                failing_step=failing_step,
                publication_refused=True,
            )

        commit_rc = commit()
        if commit_rc != 0:
            failing_step = (
                "the managed-writer publication commit refused; the pending "
                "journal remains for `ava cluster recover-pending`"
            )
            # The gateway landed and the pin advanced, but the activation did
            # not publish: the record and the aftermath must read INCOMPLETE,
            # never the CLEAN this rollout still carried one step ago.
            outcome, rc = RolloutOutcome.INCOMPLETE, commit_rc
            return PhaseBVerdict(
                rc=rc,
                outcome=outcome,
                hosts_to_resume=hosts_to_resume,
                failing_step=failing_step,
                publication_refused=True,
            )
    return PhaseBVerdict(
        rc=rc,
        outcome=outcome,
        hosts_to_resume=hosts_to_resume,
        failing_step=failing_step,
        publication_refused=False,
    )
