"""Multi-machine `ava cluster update` orchestration core.

The detached gateway rollout prepares and gates the pinned target before commit
(Phase A -> local -> readiness -> Phase B); upload detaches after finalization.

This module composes sibling `_update_*` helpers, re-exporting their seams
through `cli.commands` for the detached subprocess and focused tests.
"""

from __future__ import annotations

import subprocess as subprocess  # re-exported (tests patch _up.subprocess.run)
import sys
import time as time  # re-exported (tests patch _up.time.sleep / monotonic)
from pathlib import Path

# The orchestration steps live in sibling `_update_*` modules (split out to keep
# this module within the file-size budget). Everything below is re-imported so
# existing `cli.commands.update.*` references — the `cli.commands` re-exports,
# the tests' `_up.*` monkeypatch seams, `_cluster_rollback.py` and the detached
# rollout subprocess — keep resolving. Each name is `X as X` (an explicit
# re-export) so pyright does not flag it as unused here.
from cli.commands._repo import _repo_root as _repo_root
from cli.commands._update_agent_runner import (
    _run_agent_runner_self_update as _run_agent_runner_self_update,
)
from cli.commands._update_dispatch import (
    cmd_update as cmd_update,
)
from cli.commands._update_dryrun import (
    dry_run_checks,
    estimate_maintenance_window,
    maintenance_window_breakdown,
    maintenance_window_estimate_note,
)
from cli.commands._update_dryrun import (
    finalize_commit_telemetry as _finalize_commit_telemetry,
)
from cli.commands._update_dryrun import (
    prepare_commit as _prepare_commit,
)
from cli.commands._update_dryrun import (
    spawn_async_offsite_upload as _spawn_async_offsite_upload,
)
from cli.commands._update_fanout import (
    _PHASE_A_TIMEOUT_S as _PHASE_A_TIMEOUT_S,
)
from cli.commands._update_fanout import (
    _PHASE_B_TIMEOUT_S as _PHASE_B_TIMEOUT_S,
)
from cli.commands._update_fanout import (
    _PREFLIGHT_FETCH_TIMEOUT_S as _PREFLIGHT_FETCH_TIMEOUT_S,
)
from cli.commands._update_fanout import (
    ClusterOpPayload as ClusterOpPayload,
)
from cli.commands._update_fanout import (
    _dispatch_one_and_wait as _dispatch_one_and_wait,
)
from cli.commands._update_fanout import (
    _fan_out as _fan_out,
)
from cli.commands._update_fanout import (
    _fan_out_async as _fan_out_async,
)
from cli.commands._update_fanout import (
    _list_agent_runners as _list_agent_runners,
)
from cli.commands._update_fanout import (
    _print_fan_out_results as _print_fan_out_results,
)
from cli.commands._update_finalize import (
    finalize_orchestration as _finalize_orchestration,
)
from cli.commands._update_git import (
    GitPullFailed as GitPullFailed,
)
from cli.commands._update_git import (
    GitPullResult as GitPullResult,
)
from cli.commands._update_git import (
    _git as _git,
)
from cli.commands._update_git import (
    _vet_rollout_target as _vet_rollout_target,
)
from cli.commands._update_git import (
    apply_pending_migrations as apply_pending_migrations,
)
from cli.commands._update_git import (
    current_schema_state as current_schema_state,
)
from cli.commands._update_git import (
    git_checkout_sha as git_checkout_sha,
)
from cli.commands._update_git import (
    git_head_sha as git_head_sha,
)
from cli.commands._update_git import (
    git_pull_main as git_pull_main,
)
from cli.commands._update_git import (
    git_resolve_origin_main as git_resolve_origin_main,
)
from cli.commands._update_local import (
    _FRONTEND_SESSION as _FRONTEND_SESSION,
)
from cli.commands._update_local import (
    _boot_gateway_fresh as _boot_gateway_fresh,
)
from cli.commands._update_local import (
    _checkout_and_sync as _checkout_and_sync,
)
from cli.commands._update_local import (
    _restart_frontend_session as _restart_frontend_session,
)
from cli.commands._update_local import (
    _run_frontend_only_update as _run_frontend_only_update,
)
from cli.commands._update_local import (
    _run_gateway_local_update as _run_gateway_local_update,
)
from cli.commands._update_local import (
    _snapshot_known_good as _snapshot_known_good,
)
from cli.commands._update_orchestration import (
    _classify_rollout as _classify_rollout,
)
from cli.commands._update_orchestration import (
    _clear_stale_stop_marker as _clear_stale_stop_marker,
)
from cli.commands._update_orchestration import (
    _persist_cluster_pin,
    _phase_b_targets,
)
from cli.commands._update_orchestration import (
    _resolve_fanout_targets as _resolve_fanout_targets,
)
from cli.commands._update_pause import (
    _run_phase_a as _run_phase_a,
)
from cli.commands._update_pause import (
    _stop_the_world,
)
from cli.commands._update_phase_b import (
    _CONVERGING_TIMEOUT_S as _CONVERGING_TIMEOUT_S,
)
from cli.commands._update_phase_b import (
    _POLL_INTERVAL_S as _POLL_INTERVAL_S,
)
from cli.commands._update_phase_b import (
    _POLL_TIMEOUT_S as _POLL_TIMEOUT_S,
)
from cli.commands._update_phase_b import (
    _STAGE_NO_PROGRESS_S as _STAGE_NO_PROGRESS_S,
)
from cli.commands._update_phase_b import (
    POLL_CONVERGING as POLL_CONVERGING,
)
from cli.commands._update_phase_b import (
    POLL_NO_PROGRESS as POLL_NO_PROGRESS,
)
from cli.commands._update_phase_b import (
    POLL_OK as POLL_OK,
)
from cli.commands._update_phase_b import (
    POLL_STALLED as POLL_STALLED,
)
from cli.commands._update_phase_b import (
    PollVerdict as PollVerdict,
)
from cli.commands._update_phase_b import (
    _gateway_ready_or_incomplete,
    _phase_b_outcome,
)
from cli.commands._update_phase_b import (
    _phase_b_and_poll as _phase_b_and_poll,
)
from cli.commands._update_phase_b import (
    _phase_b_payload as _phase_b_payload,
)
from cli.commands._update_phase_b import (
    _poll_until_unpaused as _poll_until_unpaused,
)
from cli.commands._update_phase_b import (
    _probe_one_until_unpaused as _probe_one_until_unpaused,
)
from cli.commands._update_phase_b import (
    _probe_verdict as _probe_verdict,
)
from cli.commands._update_phase_b import (
    _renew_lease_while_polling as _renew_lease_while_polling,
)
from cli.commands._update_phase_b import (
    _still_converging as _still_converging,
)
from cli.commands._update_preflight import (
    _changed_paths_vs_origin as _changed_paths_vs_origin,
)
from cli.commands._update_preflight import (
    _refuse_target_sha_on_gateway as _refuse_target_sha_on_gateway,
)
from cli.commands._update_preflight import (
    _resolve_rollout_target as _resolve_rollout_target,
)
from cli.commands._update_preflight import (
    _rollout_preflight,
    _run_preflight_fetch,
)
from cli.commands._update_prepare import (
    build_prepare_gate as _build_prepare_gate,
)
from cli.commands._update_prepare import (
    print_dry_run_verdict as _print_dry_run_verdict,
)
from cli.commands._update_prepare import (
    refuse_normal_prepare as _refuse_normal_prepare,
)
from cli.commands._update_quiesce import (
    _quiesce_all_agents as _quiesce_all_agents,
)
from cli.commands._update_quiesce import (
    _quiesce_local_agents as _quiesce_local_agents,
)
from cli.commands._update_quiesce import (
    _quiesce_timeout_s as _quiesce_timeout_s,
)
from cli.commands._update_recover import (
    RolloutOutcome,
    finalize_rollout,
    local_update_failure_detail,
)
from cli.commands._update_recover import (
    _recover_rc as _recover_rc,
)
from cli.commands._update_report import (
    _local_leg_defect,
)
from cli.commands._update_report import (
    _poll_verdict_detail as _poll_verdict_detail,
)
from cli.commands._update_report import (
    _print_local_launch_failure_block as _print_local_launch_failure_block,
)
from cli.commands.stop import _do_stop as _do_stop
from shared import launch_failures, ui_update_state
from shared.cluster import get_record as get_record
from shared.cluster_lock import (
    SETTLE_TTL_S,
    acquire_update_lock,
    read_update_lease,
    release_update_lock,
    self_holder,
    settle_update_lock,
    update_lock_holder,
)
from shared.config import (
    refresh_data_plane_settings as refresh_data_plane_settings,
)
from shared.paths import ava_home as ava_home

# Re-exported as `cli.commands._classify_change` (cli/commands/__init__.py). The
# frontend/backend/doc partition lives in `shared.repo_change` so the gateway's
# read-only update preflight and the rollout classification (`_classify_rollout`,
# in `_update_orchestration`) share one source of truth.
from shared.repo_change import (
    classify_change as _classify_change,  # noqa: F401 # pyright: ignore[reportUnusedImport]  # re-export (accessed as cli.commands._classify_change)
)
from shared.rollout_telemetry import activate as _activate_telemetry
from shared.rollout_telemetry import record_host as _record_host_telemetry
from shared.rollout_telemetry import stage as _stage_telemetry


def _run_gateway_orchestration(  # noqa: PLR0915 — one transaction-shaped lifecycle
    repo: Path,
    *,
    restart_only: bool = False,
    origin: str,
    rollout_log: str | None = None,
    mode: str = "smooth",
    dry_run: bool = False,
) -> int:
    """`ava cluster update` on the gateway — take the cluster-wide update lock, then run
    the three-phase orchestration. A second update that finds the lock held by a *live*
    holder aborts (the 2026-06-01 collision was two gateway updates running at
    once, both advancing the schema). Held for the whole run, released in `finally`.

    **The lease is renewed while the run executes** (`_renew_lease_while_polling`,
    beside the Phase-B poll), so `LOCK_TTL_S` bounds only how long a *crashed* holder
    blocks the next rollout — it is not a budget the run has to finish inside. That
    decoupling is the invariant `shared.deploy_timing` exists to state: the lock must
    not expire before the operation it protects can finish.

    The `finally` does NOT always release. `unconverged` is the out-list the
    orchestration fills with the agent-runners that acked their Phase-B self-update
    but had not come back by the time the poll gave up: their checkout has moved and
    their processes have not, so the cluster is still mid-transition even though
    nothing is executing any more. Releasing there is what let a concurrent
    `ava cluster update` in on 2026-07-29 and cost two healthy agents, so that case converts
    the lease into a bounded settle hold instead — which `ops.deploy_window` ends the
    moment every host reaches the pin, rather than idling out the full window."""
    if dry_run:
        return _run_gateway_orchestration_inner(
            repo,
            restart_only=restart_only,
            origin=origin,
            rollout_log=rollout_log,
            mode=mode,
            dry_run=True,
            deploy_capability={"deploy_holder": "dry-run", "deploy_acquired_at": "dry-run"},
        )
    holder = self_holder()
    # kind: the explicit "what is happening" the old session-name probe used to
    # answer. A restart-only bounce is a `restart`; everything else that takes
    # the lease here is a full `rollout` (the runner-side `update` kind is the
    # updater's own lease in host_deploy_state, not this gateway row).
    owned_generation: str | None = None
    unconverged: list[str] = []
    expected_kind: ui_update_state.UiUpdateKind = "restart" if restart_only else "rollout"

    # Publish execution + UI ownership as one short critical section against
    # manual/automatic recovery. The parent releases this mutex immediately
    # after the detached session becomes visible, so this child can enter it.
    # Once both the DB lease and marker exist, recovery can safely observe an
    # owner without the mutex staying held for the multi-minute rollout.
    with ui_update_state.lifecycle_lock():
        if not acquire_update_lock(holder, kind=expected_kind):
            print(
                f"\n✗ another cluster update is in progress (held by "
                f"{update_lock_holder()}); aborting (the lock auto-expires after "
                "its TTL if that holder crashed)",
                file=sys.stderr,
            )
            return 1
        try:
            marker = ui_update_state.read()
            if marker.status == "inactive":
                marker = ui_update_state.begin(kind=expected_kind, origin=origin)
                owned_generation = marker.generation
            elif marker.status == "updating" and marker.legacy:
                # Introducing-rollout compatibility: an old in-memory parent
                # can launch the new-on-disk child after writing v1 posture.
                # The authoritative DB lease makes this child the safe adopter.
                owned_generation = marker.generation
            else:
                print(
                    "\n✗ orchestration refused: a persistent maintenance "
                    "generation is already active or invalid; recover it before retrying",
                    file=sys.stderr,
                )
                release_update_lock(holder)
                return 1
            if owned_generation is None:
                print(
                    "\n✗ active maintenance owner has no generation; aborting",
                    file=sys.stderr,
                )
                release_update_lock(holder)
                return 1
            if not ui_update_state.set_phase(owned_generation, "orchestrating", origin=origin):
                print(
                    "\n✗ orchestration lost UI generation ownership before pausing "
                    "the cluster; aborting",
                    file=sys.stderr,
                )
                ui_update_state.clear(owned_generation)
                release_update_lock(holder)
                return 1
            lease = read_update_lease()
            if (
                lease is None
                or lease.holder != holder
                or lease.acquired_at is None
                or lease.note is not None
            ):
                print(
                    "\n✗ orchestration could not capture its exact deploy lease "
                    "identity before Phase A; aborting",
                    file=sys.stderr,
                )
                ui_update_state.clear(owned_generation)
                release_update_lock(holder)
                return 1
            deploy_capability: ClusterOpPayload = {
                "deploy_holder": holder,
                "deploy_acquired_at": lease.acquired_at.isoformat(),
            }
        except BaseException:
            try:
                if owned_generation is not None:
                    ui_update_state.clear(owned_generation)
            finally:
                release_update_lock(holder)
            raise
    try:
        return _run_gateway_orchestration_inner(
            repo,
            restart_only=restart_only,
            origin=origin,
            rollout_log=rollout_log,
            unconverged=unconverged,
            mode=mode,
            deploy_capability=deploy_capability,
        )
    finally:
        try:
            if unconverged:
                # The hosts go in structurally, not as prose: the release path re-probes
                # exactly this set, read back out of the lease's note.
                settle_update_lock(holder, hosts=unconverged)
                note = f"waiting for {', '.join(sorted(unconverged))} to reach the pin"
                print(
                    f"\n⚠ holding the cluster deploy lease for up to a "
                    f"{SETTLE_TTL_S / 60:.0f}m settle window: {note}. No new deploy can start "
                    f"until those hosts reach the pin or the window lapses; `ava cluster status` "
                    f"to watch, `ava cluster recover` to break the hold.",
                    file=sys.stderr,
                )
            else:
                release_update_lock(holder)
        finally:
            ui_update_state.clear(owned_generation)


def _begin_update_record(
    target_sha: str | None, *, origin: str, rollout_log: str | None = None
) -> None:
    """Open the last-update record, or say on the rollout log that it could not be.

    `rollout_log` is the detached session's log path, handed down by the
    `spawn_rollout` that created the file (`ava cluster update --rollout-log`). This is the
    only write that records it, so the record names the log this run is teeing into
    rather than a guess made later from the newest `rollout-*.log` on disk — and no
    subsequent writer can attach a different one.

    Best-effort for the same reason `_record_outcome` is: this writes to the
    cluster's Postgres, and a rollout is exactly the operation that may find it
    unhealthy. Failing to open the row does not fail the rollout — it leaves the
    *previous* record standing, which the surfaces label with its own timestamp, so
    the worst case is a stale-but-honest banner rather than a wrong new one.
    """
    from shared.cluster_lock import self_holder
    from shared.last_update import begin_update

    try:
        begin_update(
            target_sha=target_sha, origin=origin, holder=self_holder(), log_path=rollout_log
        )
    except Exception as exc:  # fail-fast-ok: observability must not abort a rollout
        print(
            f"  · could not open the last-update record ({exc!r}); the status surfaces "
            f"will keep showing the previous update until this one lands",
            file=sys.stderr,
        )


def _run_gateway_orchestration_inner(  # noqa: PLR0915 (three-phase orchestration; each step is one statement)
    repo: Path,
    *,
    restart_only: bool = False,
    origin: str,
    rollout_log: str | None = None,
    unconverged: list[str] | None = None,
    mode: str = "smooth",
    dry_run: bool = False,
    deploy_capability: ClusterOpPayload,
) -> int:
    """`ava cluster update` three-phase orchestration body (runs under the update lock — see
    the wrapper `_run_gateway_orchestration`).

    `restart_only=True` skips classification entirely and always runs the full
    orchestration with no pull (bounce every service on the current code).

    On any failing/abnormal exit after Phase A, a compensating `cluster/resume`
    fan-out (the `hosts_to_resume` finally) resumes the hosts still paused —
    every host on a pre-Phase-B abort, or the ones the Phase-B poll left paused
    — so a failed rollout self-heals instead of stranding hosts paused. The
    `finally` also unpauses and finalizes the LOCAL host's pause-owner journal
    (generation-scoped), so a co-located gateway,agent-runner box does not keep
    a `paused` journal after a rollout that finished (2026-08-26 residue).
    """
    # Dynamic lookup for fan-out helpers + local-update so tests can stub.
    import cli.commands as _ns

    # 0) Classify + pin: fast paths return their rc now; otherwise the single
    #    rollout target every node checks out. The collector activated here makes
    #    every `stage()` below record into the one summary line the rollout log
    #    ends with (Task #1820 — phase durations must be numbers, not a log to
    #    re-read by hand).
    telemetry = _activate_telemetry()
    with _stage_telemetry("preflight"):
        early_rc, restart_frontend, target_sha = _rollout_preflight(
            repo, restart_only=restart_only, origin=origin, prepare_only=dry_run
        )
    if early_rc is not None:
        telemetry.print_summary()
        return early_rc

    # Open the last-update record now: the target is resolved, nothing has been
    # paused, and from here on any exit — including one that kills this process —
    # is an outcome an operator has to be able to read. Written ahead of the work
    # on purpose (`shared.last_update`): the orchestration that dies cannot file
    # its own report, so the row is opened while it still can and closed in the
    # `finally`. Best-effort, because a rollout against a sick data plane is worth
    # attempting anyway — and an unwritten row simply leaves the previous record
    # standing, which the surfaces label honestly.
    if not dry_run:
        _begin_update_record(target_sha, origin=origin, rollout_log=rollout_log)

    # A stale stopped marker is reconciled for safety, but never cleared until
    # after a commit decision: prepare itself must not mutate rollout state.
    agent_runners = _ns._resolve_fanout_targets(clear_stale_markers=False)
    # Report state for the aftermath summary the `finally` prints when the rollout did
    # not finish clean: how it ended (three outcomes, not a bool — see
    # `RolloutOutcome`), and whether the pin advanced (the gateway landed the new
    # commit) before we bailed.
    outcome = RolloutOutcome.ABORTED
    pin_advanced = False
    # The step named in the persisted record + the status banner. None until
    # something fails, and left None on the paths that abort before they can say
    # which step it was — "no step recorded" is a truthful answer there.
    failing_step: str | None = None
    # Whether this rollout's own gateway leg failed and rolled ITSELF back to
    # last-known-good. It is the difference between a failure that left a working
    # cluster and one that left a broken one, and only the leg that did the rolling
    # back can report it first-hand — see `finalize_rollout`.
    recovered = False
    # Sessions the local leg's `ava start` could not launch. Declared out here
    # because the `finally` names them in the aftermath block, and the local leg
    # that fills it in is inside the try.
    local_launch_failures: list[str] = []
    # The finalizer may compensate only after this rollout has entered Phase A.
    phase_a_started = False

    # ── Phase 0: pre-flight git fetch on every agent-runner ──────────────────
    # Every selected runner must confirm fetch. Missing acknowledgements abort
    # before pause; no unavailable participant is silently excluded.
    with _stage_telemetry("phase0_fetch"):
        phase0_failed = _run_preflight_fetch(agent_runners, restart_only=restart_only)
    if phase0_failed:
        # Printed after the stage above has recorded itself — the summary must
        # include the phase whose failure aborted the rollout.
        telemetry.print_summary()
        return 1
    # Materialize each eligible runner's ops URL for the same reason: the
    # compensating resume in the `finally` dials these directly (never a fresh
    # `machines` lookup) so it survives a data plane the failed local update took
    # down — the 2026-07-20 incident, where the resume's own Postgres read raised
    # and left every host stop-the-world + paused.
    runner_urls: dict[str, str | None] = dict(agent_runners)
    hosts_to_resume: list[tuple[str, str | None]] = []

    if dry_run:
        gate = _build_prepare_gate(
            _prepare_commit,
            repo,
            target_sha,
            pull=False,
            snapshotter=_snapshot_known_good,
            check_runner=dry_run_checks,
            estimate_runner=lambda: estimate_maintenance_window(persist_seed=False),
            breakdown_runner=maintenance_window_breakdown,
            note_runner=maintenance_window_estimate_note,
            persist_seed=False,
        )
        rc = _print_dry_run_verdict(gate)
        telemetry.print_summary()
        return rc

    pull_recover: tuple[str, set[str], Path | None] | None = None
    try:
        try:
            gate = _build_prepare_gate(
                _prepare_commit,
                repo,
                target_sha,
                pull=not restart_only,
                snapshotter=_snapshot_known_good,
                check_runner=dry_run_checks,
                estimate_runner=estimate_maintenance_window,
                breakdown_runner=maintenance_window_breakdown,
                note_runner=maintenance_window_estimate_note,
                persist_seed=True,
            )
        except Exception as exc:
            print(
                f"\n✗ prepare failed before maintenance ({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            return 1
        if (refusal := _refuse_normal_prepare(gate)) is not None:
            return refusal
        pull_recover = gate.prepared.pull_recover

        # The prepare reconciliation is deliberately read-only. Its vetted
        # candidates become mutable only at this commit boundary, immediately
        # before the stop-the-world operation consumes the same set.
        for name, _url in agent_runners:
            _clear_stale_stop_marker(name)

        # 1-1c) drain hosted agents (local + remote). None = a
        #       Phase-A 5xx; abort with nothing migrated (the finally resumes).
        hosts_to_resume = list(agent_runners)
        with _stage_telemetry("stop_the_world"):
            phase_a_started = True
            paused_names, all_quiesced = _stop_the_world(
                agent_runners,
                mode=mode,
                deploy_capability=deploy_capability,
            )
        if paused_names is None:
            return 1
        if not all_quiesced:
            return 1
        force_reap = mode == "force"

        # 2-5) gateway local stop -> pull -> sync -> start (start migrates).
        #      restart_only skips the pull/sync (bounce on current code).
        with _stage_telemetry("local_leg"):
            rc = _ns._run_gateway_local_update(
                repo,
                target_sha=target_sha,
                pull_recover=pull_recover,
                restart_frontend=restart_frontend,
                pull=not restart_only,
                force_reap_agents=force_reap,
                origin=origin,
            )
        if rc != 0:
            # rc carries the recovery outcome (1 recovered / 2 DOWN on the pull path;
            # the raw start code for a restart-only bounce). The compensating-unpause
            # finally fires for all of these — immediately on a recovered gateway,
            # queued when the gateway is DOWN.
            detail = local_update_failure_detail(rc, restart_only=restart_only)
            failing_step = f"gateway local update (rc={rc}): {detail}"
            # rc==1 on the pull path IS the recovery: `_run_gateway_local_update`
            # returns it only after the gateway came back on last-known-good. A
            # restart-only bounce has nothing to roll back to, so its rc==1 is a
            # plain failed bounce and must not be dressed up as a recovery.
            recovered = rc == 1 and not restart_only
            print(
                f"\n✗ gateway local update failed (rc={rc}); {detail}. Resuming the "
                "paused agent-runners.",
                file=sys.stderr,
            )
            return rc  # finally resumes every paused host

        # What the local leg's child `ava start` could not launch. Read here rather
        # than inferred from its exit code for two reasons: that leg runs with
        # `--no-readiness-gate`, so the code is 0 either way, and the names cannot
        # ride an integer. The child writes them because it — not this pre-pull
        # interpreter — holds the new tree's service roster.
        #
        # This does NOT abort the rollout. The gateway is serving (6.5 checks that
        # for real) and the agent-runners still need their update; what a missing
        # local session changes is the *verdict*, applied at the returns below.
        local_launch_failures = launch_failures.take()
        failing_step = _local_leg_defect(local_launch_failures) or failing_step

        # The local leg's child `ava start` may have rewritten $AVA_HOME/.env —
        # a data-plane credential rotation migration (the 2026-08-25 secret
        # split) does exactly that. THIS process's Settings singleton was built
        # at startup, so without a refresh every later data-plane write — the
        # pin advance below, and the compensating unpause / lock release in the
        # finally — dials with the pre-rotation password and dies with SASL
        # authentication failures, stranding the cluster paused with a stale
        # pin (2026-08-25 incident). Refresh before the first post-leg write.
        refresh_data_plane_settings()

        # Persist the cluster's pinned commit now that the gateway is on it:
        # the standing `cluster_target_sha` agent-runners converge to in Phase B and
        # `ava status` compares each node's HEAD against. restart_only bounces the
        # current code (target_sha None) and pins nothing.
        if target_sha is not None:
            _persist_cluster_pin(target_sha, origin=origin, advance_known_good=True)
            pin_advanced = True

        if not agent_runners:
            outcome = RolloutOutcome.INCOMPLETE if local_launch_failures else RolloutOutcome.CLEAN
            return 1 if local_launch_failures else 0

        # 6.4) Who Phase B actually fans out to: every rollout target except THIS
        #      host. A co-located gateway,agent-runner box was updated by the local
        #      leg above, and its redundant self-update would kill the gateway the
        #      readiness gate is about to bless — see `_phase_b_targets`. Phases 0
        #      and A keep the full list on purpose: their ops are idempotent with
        #      the local work, Phase B's is not.
        fanout_targets = _phase_b_targets(agent_runners)

        # 6.5) Phase B's precondition, checked instead of assumed (see
        #      `_gateway_ready_or_incomplete`). A non-SERVING gateway skips the fan-out
        #      entirely and reports INCOMPLETE rather than letting every runner decline.
        #      Still asked when this host is the only target: the local leg's `ava start`
        #      runs with `--no-readiness-gate`, so skipping here would leave a single-box
        #      rollout with the readiness question asked nowhere at all.
        with _stage_telemetry("readiness"):
            gateway_serving = _gateway_ready_or_incomplete(
                fanout_targets, paused_names, unconverged
            )
        if not gateway_serving:
            outcome = RolloutOutcome.INCOMPLETE
            failing_step = "the gateway was not serving, so Phase B never fanned out"
            return 1

        # 7-8) Phase B + poll + verdict; hosts still mid-transition keep the lease
        #      as a settle hold. outcome / hosts_to_resume are re-assigned here so
        #      the `finally` reports the true aftermath, not the ABORTED default.
        # Per-host updater stage times, gathered by the Phase-B poll from the
        # `last_updater_outcome` each status probe carried; a converged host is
        # re-probed once (the fresh-idle read in `ops.updater_outcome` serves
        # its completed breakdown, `start` included). Land in the telemetry
        # summary so one rollout log shows every host's checkout/uv/stop/start.
        host_outcomes: dict[str, dict[str, object]] = {}
        with _stage_telemetry("phase_b"):
            rc, outcome, hosts_to_resume = _phase_b_outcome(
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
            # already names the sessions (set right after the local leg) and the
            # aftermath block lists them.
            outcome, rc = RolloutOutcome.INCOMPLETE, 1
        return rc
    finally:
        _finalize_orchestration(
            hosts_to_resume=hosts_to_resume,
            phase_a_started=phase_a_started,
            fan_out=_ns._fan_out,
            phase_a_timeout_s=_PHASE_A_TIMEOUT_S,
            outcome=outcome,
            deploy_capability=deploy_capability,
            pin_advanced=pin_advanced,
            failing_step=failing_step,
            recovered=recovered,
            local_launch_failures=local_launch_failures,
            telemetry=telemetry,
            refresh_settings=refresh_data_plane_settings,
            finalize_rollout_runner=finalize_rollout,
            finalize_commit_telemetry=_finalize_commit_telemetry,
            spawn_offsite_upload=_spawn_async_offsite_upload,
            repo=repo,
            pull_recover=pull_recover,
        )
