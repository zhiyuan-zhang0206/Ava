"""Gateway recovery to last-known-good after a failed local `ava cluster update`.

Split out of `cli/commands/update.py` to keep that module within the file-size
budget. The local update graceful-stops every gateway daemon (the gateway
included) before it pulls, so any pull / uv sync / `ava start` failure leaves the
gateway offline — and the orchestration's compensating cluster/resume rows
cannot reach the agent-runners until it is back up. `_recover_gateway_local`
rolls the node back to the pre-update commit + schema and re-starts, reviving the
gateway so those resumes deliver. Invoked only by `_run_gateway_local_update`.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from cli.commands._update_fanout import ClusterOpPayload
from cli.commands._update_git import (
    GitPullFailed,
    LocalAdminSchemaConnectionError,
    git_reset_hard,
    rollback_schema_to,
)
from cli.commands._update_uv_sync import run_uv_sync
from shared.exit_codes import SERVICES_NOT_READY_EXIT_CODE

# The `_fan_out` the orchestration injects into `finalize_rollout`: POST a
# path-addressed op to each (name, ops_url) host, return (name, status, detail).
_FanOut = Callable[
    [list[tuple[str, str | None]], str, float, ClusterOpPayload],
    list[tuple[str, str, str]],
]


class RolloutOutcome(StrEnum):
    """How a gateway orchestration ended — three facts, where there used to be a
    `clean_exit` bool carrying two.

    The bool made `INCOMPLETE` unrepresentable, so it was reported as `CLEAN`: a
    rollout whose Phase-B poll gave up on every remote agent-runner printed no
    aftermath and exited 0. Measured on prod (`~/.ava/logs/rollout-1785326960.log`,
    2026-07-29): four runners acked their self-update, one came back, the session
    log ends `[session-exit] rc=0`. "The poll ran out of patience" and "the update
    finished cleanly" are different facts, and one boolean cannot hold both.

    Flipping the bool instead would have swapped one lie for another — the
    `clean_exit=False` report is headed ROLLOUT ABORTED, and an incomplete rollout
    is not an aborted one: the gateway migrated, the pin advanced, and the hosts
    that acked are still converging under a settle hold.
    """

    CLEAN = "clean"
    # The gateway leg succeeded and the pin advanced, but some acked agent-runner
    # never reported back. Not an abort; the lease is held while they settle.
    INCOMPLETE = "incomplete"
    # The orchestration bailed — before Phase B, or on an exception.
    ABORTED = "aborted"


def _print_pre_update_data_snapshot_restore(data_snapshot: Path | None) -> None:
    """Name the verified restore point without exposing the cluster DB URL."""
    if data_snapshot is not None:
        print(
            f"  · pre-update data snapshot: {data_snapshot} — restore: decrypt, then "
            "pg_restore --clean --if-exists -d <db_url> <decrypted-dump> "
            "(drill: conventions/down-failure-drill.md)",
            file=sys.stderr,
        )


def _recover_gateway_local(
    repo: Path,
    from_sha: str,
    schema_snapshot: set[str],
    *,
    preserve_sessions: frozenset[str],
    data_snapshot: Path | None = None,
) -> int:
    """Restore the gateway to last-known-good (`from_sha`) after a failed
    local update.

    Order matters: roll the schema back to `schema_snapshot` FIRST (the down files
    only exist in the just-pulled tree), THEN `git reset --hard from_sha` + uv sync
    + a fresh `ava start` on the known-good revision. `schema_snapshot` is the
    applied set captured before the failed start; when the start never advanced the
    schema the rollback is a no-op. `preserve_sessions` forwards `--disable-service` for a
    backend-only update (the frontend was left running, so don't rebuild it). A verified
    `data_snapshot` lets manual recovery restore data when schema rollback cannot fully
    reverse a destructive migration.

    Returns 0 when the gateway is back up on last-known-good; 1 when it
    cannot be (the rollback would cross the squashed baseline, which has no down —
    code + schema are left on the new revision, consistent, for fix-forward — or
    the recovery git reset / uv sync / start itself failed). The unrecoverable cases are logged
    loudly: there is no push-notification primitive, so the marker in the detached
    rollout session log + a visibly-broken `ava cluster status` is the alert. The
    caller returns non-zero regardless — the update did not succeed.
    """
    from shared.migrations import MigrationError, RollbackBelowFloor

    print(f"\n→ recover: roll gateway back to last-known-good {from_sha[:7]}", file=sys.stderr)
    try:
        # Recovery must not authenticate as the runtime owner role: a failed
        # credential-journal replay can leave this surviving parent holding the
        # old password after Postgres already accepted the new one.
        rolled = rollback_schema_to(schema_snapshot, local_admin=True)
    except RollbackBelowFloor:
        # Nothing was rolled back (the floor guard fires before any down runs): the
        # schema is untouched on the new revision, so code + schema stay consistent.
        # Resetting code to from_sha under that newer schema would invert this into
        # CodeBehindSchema, so leave it for fix-forward.
        print(
            "  ✗✗ MANUAL INTERVENTION: rolling back to the pre-update applied set "
            "would cross the squashed baseline (no down to reverse it). Left code + "
            "schema on the new revision (consistent, fix-forward); the gateway is DOWN.",
            file=sys.stderr,
        )
        _print_pre_update_data_snapshot_restore(data_snapshot)
        return 1
    except LocalAdminSchemaConnectionError:
        # This dedicated exception is raised only before rollback_to receives a
        # connection, so unchanged schema is a justified conclusion.
        print(
            "  ✗✗ MANUAL INTERVENTION: local-admin schema recovery could not start; "
            "schema is unchanged and code was NOT reset. The gateway is DOWN.",
            file=sys.stderr,
        )
        _print_pre_update_data_snapshot_restore(data_snapshot)
        return 1
    except MigrationError as exc:
        # A down failure aborts rollback_to's one batch transaction, leaving the
        # schema at the applied set the failed start left: equal to or a subset of
        # what the new code expects. Do NOT reset code: old code under that schema
        # would become CodeBehindSchema. The state is determinate and fix-forwardable,
        # but the stopped gateway still requires a human decision.
        print(
            f"  ✗✗ MANUAL INTERVENTION: a down migration failed mid-rollback ({exc}); "
            "rollback aborted atomically, so the schema is unchanged (not partially "
            "rolled back) and code was NOT reset. Code + schema remain consistent "
            "for fix-forward: re-run `ava cluster update` or `ava start` on the new "
            "revision to apply pending migrations. Do NOT reset code; the gateway is "
            "DOWN and requires a human decision.",
            file=sys.stderr,
        )
        _print_pre_update_data_snapshot_restore(data_snapshot)
        return 1
    except Exception as exc:  # fail-fast-ok: recovery must preserve code/schema ordering
        # After rollback_to begins, an error can arrive after transaction commit
        # (for example while releasing the advisory lock) or with an uncertain
        # commit outcome. Never claim the schema remained unchanged, and never
        # reset code across that ambiguity. Name only the exception type because
        # connection failures may embed a URL in their text.
        print(
            f"  ✗✗ MANUAL INTERVENTION: schema rollback raised {type(exc).__name__}; "
            "schema state is UNKNOWN and code was NOT reset. Verify the applied "
            "migration set and schema before choosing reset or fix-forward. The "
            "gateway is DOWN.",
            file=sys.stderr,
        )
        _print_pre_update_data_snapshot_restore(data_snapshot)
        return 1
    if rolled:
        print(f"  · rolled back {len(rolled)} migration(s): {rolled}", file=sys.stderr)

    try:
        git_reset_hard(from_sha)
    except GitPullFailed as exc:
        # A reset that does not land (index-lock timeout, wedged tree, or a raced
        # reset leaving a mixed tree) leaves the gateway stopped on a tree that is
        # not last-known-good — uv sync / start must not run on it, and a bare
        # traceback at the top of the rollout session log is exactly the failure
        # shape the recovery exists to mark loudly.
        print(
            f"  ✗✗ MANUAL INTERVENTION: recovery `git reset --hard {from_sha[:7]}` "
            f"failed ({exc}); the gateway is DOWN and the tree is NOT on "
            "last-known-good",
            file=sys.stderr,
        )
        _print_pre_update_data_snapshot_restore(data_snapshot)
        return 1
    print(f"  · git reset --hard {from_sha[:7]}", file=sys.stderr)
    sync = run_uv_sync(repo)
    if sync.returncode != 0:
        print(
            "  ✗✗ MANUAL INTERVENTION: recovery `uv sync` failed; gateway DOWN",
            file=sys.stderr,
        )
        _print_pre_update_data_snapshot_restore(data_snapshot)
        return 1

    print("\n→ recover: ava start on last-known-good", file=sys.stderr)
    # --persist-services: this restart's skips are transient, not a durable
    # operator disable — preserve the watchdog's --disable-service marker.
    start_cmd = [str(repo / ".venv" / "bin" / "ava"), "start", "--persist-services"]
    # The maintenance admission hold remains owned by the orchestration until
    # its final readiness and compensating-resume boundary.
    for session in sorted(preserve_sessions):
        start_cmd += ["--disable-service", session]
    start_rc = subprocess.run(start_cmd, cwd=repo, check=False).returncode
    if start_rc == SERVICES_NOT_READY_EXIT_CODE:
        # The recovery start completed every step and this host is back on
        # last-known-good; a service it launched has not passed its probe yet, and the
        # child named which one immediately above. Degraded, not down — so it must not
        # fall into the escalation below, where `_recover_rc` turns any non-zero into
        # "gateway DOWN, a human is needed". Sending someone at a slow `milvus` or a
        # headless `browser` on a host that is already serving is a false page, and the
        # watchdog keepalive is armed to revive whatever is missing.
        #
        # The code is roster-wide, so this path cannot tell a straggling `browser` from
        # an unready gateway; the latter would read as "recovered (degraded)" here.
        # That blind spot is not new — before the code existed this line was reached
        # with rc 0 and claimed a clean recovery either way — but it is now announced
        # rather than silent. Narrowing it needs a per-service verdict across the
        # process boundary, which an exit code cannot carry.
        print(
            f"  ⚠ gateway recovered to {from_sha[:7]}, but service(s) above are not "
            f"ready yet; the watchdog keeps reviving them — confirm with `ava status`",
            file=sys.stderr,
        )
        return 0
    if start_rc != 0:
        print(
            "  ✗✗ MANUAL INTERVENTION: recovery `ava start` on last-known-good failed; "
            "gateway DOWN",
            file=sys.stderr,
        )
        _print_pre_update_data_snapshot_restore(data_snapshot)
        return 1
    print(f"  ✓ gateway recovered to {from_sha[:7]}", file=sys.stderr)
    return 0


def _recover_rc(
    repo: Path, recover_ctx: tuple[str, set[str], Path | None], preserve_sessions: frozenset[str]
) -> int:
    """Recover, mapping the outcome onto the non-zero rc `_run_gateway_local_update`
    returns: 1 = recovered to last-known-good (gateway back, deferred resumes deliver),
    2 = gateway DOWN (no auto-fix; a human is needed). The update failed either
    way, so this is always non-zero."""
    from_sha, schema_snapshot, data_snapshot = recover_ctx
    rec = _recover_gateway_local(
        repo,
        from_sha,
        schema_snapshot,
        preserve_sessions=preserve_sessions,
        data_snapshot=data_snapshot,
    )
    return 1 if rec == 0 else 2


def local_update_failure_detail(rc: int, *, restart_only: bool) -> str:
    """One-line operator summary for a failed gateway local update, keyed on the
    rc `_run_gateway_local_update` returned (1 recovered / 2 DOWN on the pull path;
    a restart-only bounce has no recovery, so rc is the raw `ava start` code)."""
    from shared.exit_codes import STOP_INCOMPLETE_EXIT_CODE

    if rc == STOP_INCOMPLETE_EXIT_CODE:
        return "pause/stop incomplete; source and schema unchanged; use ava start to restore stopped services"
    if restart_only:
        return "restart bounce on current code failed (nothing to roll back); check `ava cluster status`"
    if rc == 1:
        return "recovered to last-known-good; the gateway is back, so the compensating resume rows deliver now"
    return (
        "the gateway is DOWN and could not auto-recover — MANUAL INTERVENTION required "
        "(grep the recover log for the ✗✗ marker); the resume rows queue until it is back"
    )


def finalize_rollout(
    hosts_to_resume: list[tuple[str, str | None]],
    fan_out: _FanOut,
    resume_timeout_s: float,
    *,
    deploy_capability: ClusterOpPayload,
    outcome: RolloutOutcome,
    pin_advanced: bool,
    failing_step: str | None = None,
    recovered: bool = False,
    local_launch_failures: list[str] | None = None,
) -> None:
    """`finally`-clause tail of the gateway orchestration: record how the rollout
    ended, best-effort resume every still-paused host, then — unless the rollout was
    `CLEAN` — print the cluster's residual state and the recovery commands that fit
    what actually happened.

    `hosts_to_resume` carries each host's pre-resolved ops URL so `fan_out` dials
    it Postgres-free (a failed local update may have taken the data plane down).

    The record (`shared.last_update`) is written here rather than beside each return
    because this is the one place every path reaches, including the abort paths —
    which are the ones worth reporting. It closes the row `begin_update` opened, so
    the state the status surfaces show goes from "an update is running" to a stated
    outcome, and a *successful* rollout replaces the previous failure rather than
    leaving anyone to clear it.

    `local_launch_failures` names the sessions this host's own `ava start`
    could not create. It is reported here rather than left to the child's log
    because it is the one failure the host-oriented aftermath would otherwise miss
    entirely: no host is stranded, nothing needs resuming, and every runner may have
    converged — yet a service on the gateway does not exist.

    `recovered` is the caller's first-hand answer to the one question `outcome`
    cannot express: an `ABORTED` rollout whose gateway leg rolled itself back to
    last-known-good left a *working* cluster, which is a different thing to tell an
    operator than an abort that left one broken. It refines only the recorded
    outcome; the aftermath report below still keys on `outcome`, since what has to be
    resumed and re-checked is the same either way.

    **Never raises.** It runs in a `finally` that may already be unwinding an
    exception; a raise here would mask that root cause and skip the report (the
    2026-07-20 incident: the compensating resume's own Postgres read raised, so the
    cluster was left stop-the-world + paused under a second traceback). The record
    write, the resume dial and the report are each guarded so none escapes — and the
    record is first, because a rollout whose data plane is down is exactly when the
    row cannot be written and the reader has to fall back on the orphan reading.
    """
    _record_outcome(
        outcome, pin_advanced=pin_advanced, failing_step=failing_step, recovered=recovered
    )
    reached: list[str] = []
    unreached: list[str] = [name for name, _url in hosts_to_resume]
    if hosts_to_resume:
        print(
            f"\n→ compensating unpause: resume {len(hosts_to_resume)} still-paused host(s)",
            file=sys.stderr,
        )
        try:
            results = fan_out(
                hosts_to_resume,
                "/api/cluster/resume",
                resume_timeout_s,
                deploy_capability,
            )
            reached = [name for name, status, _ in results if status == "ok"]
            unreached = [name for name, status, _ in results if status != "ok"]
        except Exception as exc:
            print(f"  ✗ compensating resume dial itself failed: {exc!r}", file=sys.stderr)

    if outcome is RolloutOutcome.CLEAN:
        return
    _print_rollout_aftermath(
        reached=reached,
        unreached=unreached,
        pin_advanced=pin_advanced,
        outcome=outcome,
        local_launch_failures=local_launch_failures or [],
    )


def _record_outcome(
    outcome: RolloutOutcome, *, pin_advanced: bool, failing_step: str | None, recovered: bool
) -> None:
    """Persist the terminal outcome, or say on the rollout log that it could not be.

    `RolloutOutcome` maps onto `UpdateOutcome` name-for-name, except that a
    self-recovered abort is recorded as `RECOVERED` — the orchestration watched its
    own gateway leg roll back, so this is a report rather than an inference, and it
    is written directly instead of waiting for an external observer to notice.

    Best-effort by necessity: the write goes to the cluster's Postgres, and the
    rollouts most worth recording are the ones that may have just taken it down. A
    failure here is not silent — the row stays open, so `read_last_update` resolves
    it as `ORPHANED` once the lease lapses, which is the *correct* reading of "the
    orchestration could not report" and strictly better than a row claiming CLEAN.
    """
    from shared.last_update import UpdateOutcome, finish_update

    recorded = UpdateOutcome.RECOVERED if recovered else UpdateOutcome(str(outcome))
    try:
        finish_update(recorded, failing_step=failing_step, pin_advanced=pin_advanced)
    except Exception as exc:  # fail-fast-ok: the finally must not raise; see the caller
        print(
            f"  ✗ could not record the rollout outcome ({exc!r}); the status surfaces "
            f"will report it as an orphaned update once the deploy lease lapses",
            file=sys.stderr,
        )


def _print_rollout_aftermath(
    *,
    reached: list[str],
    unreached: list[str],
    pin_advanced: bool,
    outcome: RolloutOutcome,
    local_launch_failures: list[str],
) -> None:
    """Print the residual-state + recovery block after a rollout that did not finish
    clean, so an operator does not have to reverse-engineer the cluster's state from a
    traceback: which hosts are resumed vs still paused, whether the pin advanced, and
    the exact recovery commands. stderr (the rollout log's error stream).

    The banner is keyed on `outcome` because the two cases need opposite instructions:
    an ABORTED rollout is retried once its cause is fixed, while an INCOMPLETE one has
    already migrated and advanced the pin — re-running `ava cluster update` into it is the
    2026-07-29 collision, and the correct move is to wait out (or look at) the hosts
    the settle hold names.

    INCOMPLETE now has two shapes, so the banner distinguishes them: hosts that never
    came back, and a local service session that never launched. Naming the wrong one
    sends the operator to the wrong machine.
    """
    rule = "=" * 64
    if outcome is not RolloutOutcome.INCOMPLETE:
        banner = "ROLLOUT ABORTED — cluster residual state + recovery"
    elif unreached or not local_launch_failures:
        banner = "ROLLOUT INCOMPLETE — the gateway landed; some agent-runners did not"
    else:
        banner = "ROLLOUT INCOMPLETE — the code landed; a service on this host did not come up"
    out = ["", rule, banner, rule]
    # Local sessions first: unlike the host rows below, this one names a service
    # process that does not exist on the host printing the block, and it is the
    # failure an operator can fix immediately.
    if local_launch_failures:
        out.append(
            f"  THIS HOST is short a service — sessions that failed to launch "
            f"(retried once): {local_launch_failures}"
        )
    if reached:
        out.append(f"  resumed now: {reached}")
    if unreached:
        out.append(f"  STILL PAUSED (resume did not reach them): {unreached}")
    if not reached and not unreached:
        out.append("  paused hosts: none (nothing to resume)")
    if not pin_advanced:
        out.append("  cluster pin: NOT advanced — the cluster is still on the pre-rollout commit")
    elif unreached:
        # Issue #1114: this block lists these hosts as STILL PAUSED one line above, and
        # then promised them a watchdog self-heal. A paused host's watchdog reconciles
        # NOTHING — `PauseController` blocks the tick ahead of the pin and code
        # controllers — so the promise cannot land until the flag comes off, and the
        # operator reading it as "wait" waits on the wrong thing.
        #
        # The dependency is named instead of a duration, deliberately. How long the flag
        # stands is not one number: stranded-pause recovery declines while anything owns
        # the pause, and a rollout ending INCOMPLETE has just taken a deploy hold over
        # these very hosts. Printing a bound here would be a second wrong promise.
        out.append(
            "  cluster pin: advanced — the gateway is on the new commit. A resumed host "
            "converges via its watchdog self-heal; a STILL PAUSED one does not — its "
            "watchdog skips every round while the pause holds, so it converges only once "
            "the pause clears (stranded-pause recovery, which waits until nothing owns "
            "the pause — `ava cluster status` shows the deploy hold — or the manual "
            "command below)"
        )
    else:
        out.append(
            "  cluster pin: advanced — the gateway is on the new commit; remaining hosts "
            "converge via Phase B / their watchdog self-heal"
        )
    out.append("  manual recovery:")
    if local_launch_failures:
        out.append(
            "    · on THIS host: `ava start` (idempotent; relaunches only what is missing). "
            "The watchdog keepalive is also retrying it every 60s."
        )
    if unreached:
        out.append(
            "    · per STILL-PAUSED host: unpause its posture row (ava start, or wait for the stranded-pause controller) && "
            "cd $AVA_HOME/source && ava start"
        )
    # The three lines below are about hosts mid-transition. A rollout whose only
    # defect is a local session that would not launch has none — every runner
    # converged — so pointing the operator at updater logs and `ava cluster recover`
    # would send them hunting a host that is fine.
    if outcome is RolloutOutcome.INCOMPLETE and (reached or unreached):
        out.append(
            "    · do NOT re-run `ava cluster update` yet: this rollout already migrated and "
            "advanced the pin, and a second deploy into a half-transitioned cluster is "
            "the 2026-07-29 incident. The deploy lease is held for the named hosts and "
            "releases the moment they reach the pin."
        )
        out.append(
            "    · per host above: read its updater log under $AVA_HOME/logs "
            "(`updater-<epoch>.log` on POSIX, `ava-updater.out.log` on Windows). A "
            "STALLED host needs its cause fixed; a still-converging one needs time."
        )
        out.append(
            "    · `ava cluster recover` on a host whose deploy is provably dead "
            "(it refuses while the holder is alive)"
        )
    elif outcome is not RolloutOutcome.INCOMPLETE:
        out.append(
            "    · once the cause is fixed, re-run `ava cluster update` from the gateway to retry"
        )
    out.append("    · `ava cluster status` to confirm every node is unpaused and on-pin")
    out.append(rule)
    print("\n".join(out), file=sys.stderr)
