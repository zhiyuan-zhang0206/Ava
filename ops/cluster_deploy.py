"""Trigger a whole-cluster update / rollout / restart, and refuse a second one.

The top of the `ops/cluster*` family: the read-only "is there anything to roll
out" preflight, the three detached-session triggers, the mutual exclusion that
keeps two of them from fighting over one host's services, and the reaper that
cuts a hung updater session loose so it stops blocking every later attempt —
asked both by the next attempt and, because a hung session refuses that next
attempt cluster-wide before it can arrive, once a watchdog round
(`ops.controllers.stalled_updater`).

**This file sits in the 600-800 transitional zone on purpose, not by oversight.**
That zone is a non-blocking nudge, and the only way to leave it would be to cut
the rollout control flow — preflight, trigger, refusal, reap — across two modules
that are each half of one story. A module that exists because its sibling was
seventy lines over a soft threshold is fragmentation by count rather than by
topic, and nobody can explain it later. Split this file when a *topic* leaves,
not when the number does.

The gateway's `ava cluster update` no longer only touches the local host;
it fans out in two phases by dialing each agent-runner's ops server:

  Phase A: cluster_stop
    -> POST to the agent-runner's ops server, which calls
       `operations.cluster_stop_op` in-process
    -> pauses the host (posture row) + kills ava-restarter
    -> while the host is paused, the watchdog skips reconcile so the
       restarter stays down through migration
  Phase B: cluster_update  (the gateway has already force-checked-out the
    pinned target_sha + migrated locally), payload carries `target_sha`
    -> the agent-runner's ops server calls `operations.cluster_update_op(target_sha=...)`
    -> spawns a detached `ava-updater` session running the in-process
       self-update (R1-6 execution-shape convergence):
       `$SHELL -lc '{ <venv>; if cd <repo>; then python -m cli.commands._update_agent_runner [--target-sha <ref>] [--mode <m>] [--force-reap]; rc=$?; fi; echo "[session-exit] rc=$rc"; } 2>&1 | tee -a <log>'`
       (`$SHELL -lc` re-sources the user's zshrc/bashrc for env, but `python` is
       resolved from the checkout's `.venv/bin`, not that shell's PATH; `<ref>`
       is the pinned target_sha, or origin/main on a watchdog self-heal with no sha)
    -> the in-process path FORCE-checks out the exact pinned commit from any
       branch / dirty state, so every node in the rollout matches (no per-node
       `git pull` that could re-resolve a moving tip — the 2026-06-01 collision),
       then verifies the tree (`verify_tree_at` — the 2026-08-02 mixed-tree
       guard); any checkout/sync/verify failure ABORTS before the stop instead of
       starting services on a possibly-mixed tree (deliberate, post-2026-08-02)
    -> `ava start` ends with register_self() + posture row -> idle

  CLI: `ava cluster update` on the gateway POSTs /api/cluster/rollout
    (gateway only), which spawns a detached `ava-rollout` session
    running the full `ava cluster update --local` orchestrator. The frontend
    "Update" button POSTs the same endpoint. (The one-time agent SDK entry
    `ava.self.update()`, which let an agent self-trigger a whole-cluster
    upgrade, was removed 2026-08.)
        The per-host primitive Phase B fans out is the `cluster_update` op dialed straight to each agent-runner's ops server; the gateway's `/api/cluster/update` relays it for one host, and a watchdog self-heal dials its local ops server directly.

"""

from __future__ import annotations

import contextlib
import logging
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import shared.cluster
import shared.migrations
import shared.paths
import shared.ui_update_state
import shared.updater_handoff
from ops import cluster_pause, cluster_session, deploy_spawn
from ops._update_shell import SOURCE_SWITCH_OFF, _restart_recovery_cmd
from ops.cluster_session import (
    _CLUSTER_RESTART_SERVICE,
    _REPO_ROOT,
    _ROLLOUT_DRYRUN_SERVICE,
    _ROLLOUT_SERVICE,
    _UPDATER_SERVICE,
    _native_arg,
)
from ops.deploy_spawn import (
    ClusterUpdateInProgress as ClusterUpdateInProgress,
)
from ops.deploy_spawn import (
    assert_no_orchestration_in_flight as _assert_no_orchestration_in_flight,
)
from ops.deploy_spawn import update_entry_args as _update_entry_args
from ops.deploy_spawn import wait_for_ui_owner as _wait_for_ui_owner
from ops.pitr_restart import PitrRestartContinuation, resume_commands
from ops.update_check import (
    UpdateCheck as UpdateCheck,  # re-export (split out for the file-size ceiling)
)
from ops.update_check import (
    _git_ro as _git_ro,
)
from ops.update_check import (
    update_check as update_check,
)
from ops.updater_outcome import mark_native_run, native_exit_line
from ops.updater_reap import (
    REAP_CLEARED_QUALIFIER as REAP_CLEARED_QUALIFIER,
)
from ops.updater_reap import (
    _reap_stalled_updater as _reap_stalled_updater,
)
from ops.updater_reap import (
    _updater_hung as _updater_hung,
)
from shared.config import settings
from shared.gitenv import git_env
from shared.platform import LockTimeoutError
from shared.proc import run_bounded, timeout_stderr_tail
from shared.session_env import venv_activation_prefix


class NothingToUpdate(RuntimeError):  # noqa: N818 — state description, same style as ClusterUpdateInProgress
    """The cluster is already on the latest code — `update_check()` reports a clean zero.

    Raised at the rollout chokepoint (`spawn_rollout`) before any agent is
    paused or restarted. A rollout with nothing to pull would still bounce
    every agent and discard their warm in-flight state for zero code change,
    so the trigger fails fast instead of silently rolling an empty cluster.

    Surfaced as HTTP 422 by the FastAPI handler (distinct from the 409 of an
    in-flight update). This is *not* a transient failure: there is nothing to
    update, so retrying without new commits will hit the same state. A restart
    (which pulls nothing) is unaffected and stays available.
    """


_log = logging.getLogger(__name__)

# The validate-before-kill best-effort `git fetch` (below) used to run with no
# timeout at all. `spawn_update` is invoked synchronously from the ops server's
# async dispatch (services/agent_ops/daemon.py), so an unbounded fetch (slow /
# stalled network) blocks that event loop indefinitely — including the
# compensating /api/cluster/resume the gateway sends on a later failure. Bound
# it; a timeout here is caught and treated the same as any other fetch failure
# (validate_migrations_at_ref fails closed on an unreadable ref).
# The bound is applied by `run_bounded`, not `subprocess.run(timeout=)`: this
# fetch is the site that leaked 66 git/ssh/sh processes on the Windows
# agent-runner, where the timeout killed only Git-for-Windows' launcher stub.
_VALIDATE_FETCH_TIMEOUT_S = 30.0

# Per-invocation update/rollout/restart logs live in $AVA_HOME/logs; keep only
# the newest N per prefix so a long-lived ops host does not grow the dir
# unbounded.
_UPDATE_LOG_KEEP = 30


def _new_update_log(prefix: str) -> Path:
    """Return a fresh `$AVA_HOME/logs/{prefix}-<epoch>.log`, trimming old siblings.

    Keeps the newest `_UPDATE_LOG_KEEP` logs sharing `prefix`; older ones are
    deleted. Lexical sort == chronological because the suffix is an integer epoch
    second (fixed width through year 2286).
    """
    log_dir = shared.paths.ava_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(log_dir.glob(f"{prefix}-*.log"))
    for stale in existing[:-_UPDATE_LOG_KEEP]:
        stale.unlink(missing_ok=True)
    return log_dir / f"{prefix}-{int(time.time())}.log"


def spawn_update(  # noqa: PLR0915 — one pause-to-detached-child transaction
    *,
    restart_only: bool = False,
    target_sha: str | None = None,
    mode: str = "smooth",
    force_reap: bool = False,
) -> dict[str, str]:
    """Trigger a local update via a detached `ava-updater` session.

    The session runs the in-process self-update (R1-6 execution-shape
    convergence): a detached shell command — `{ <venv activation>; if cd <repo>;
    then python -m cli.commands._update_agent_runner [--target-sha <ref>]
    [--mode <m>] [--force-reap]; rc=$?; else ...; fi; echo "[session-exit]
    rc=$rc"; } 2>&1 | tee -a <log>` — hosted on the service session backend
    (`get_backend()`; POSIX: native process supervisor, Windows: winproc), whose
    `bash -lc` wrapper re-activates the venv inside the command. `<ref>` is the
    pinned `target_sha` (so every node in the rollout lands on the exact same
    commit) or `origin/main` when absent (a watchdog self-heal POST, which just
    catches the host up). The in-process path is the same
    `_run_agent_runner_self_update` an operator's `ava cluster update` runs on a
    runner — it force-checks-out the target (landing from any branch / dirty
    state), verifies the tree post-checkout (`verify_tree_at` — the 2026-08-02
    mixed-tree guard), syncs, vets the migration layout (reverting on a broken
    one), probes the gateway, quiesces this host's agents per `mode`, stops
    gracefully and starts a fresh `ava start`. Any pre-stop failure returns
    non-zero with the host still serving its current code — the old chain's
    **ABORT** contract (never start services on a possibly-mixed tree), now
    enforced in Python; the rollout's compensating resume handles the aftermath.
    A preflight refusal returns `RESTART_DECLINED_EXIT_CODE` — nothing was
    stopped and the host is still serving. `python` resolves from this checkout's
    `.venv/bin` (`venv_activation_prefix`, exported ahead of the `if` so the
    branch sees it), never from a login PATH — see `spawn_rollout` for the
    rc=127 this prevents. Idempotent: refuses if the session already exists.

    `restart_only=True` (the agent-runner leg of a cluster *restart*) passes
    `--restart-only` — the entry skips the checkout + uv sync and bounces the
    services on the current code, no pull. The same `ava-updater` session name
    guards both so a restart and an update can never race on one host.

    Validate before kill: on a non-restart update, the target's migrations/ layout
    is vetted from git (read-only, no checkout) BEFORE the pause. A duplicate /
    non-contiguous version otherwise only fails the trailing `ava start` migrate —
    after the stop has taken the host down (the 2026-06-17 dup-0049 outage). A
    broken layout raises here, leaving the cluster serving its current code.

    Pause first: `pause_local_cluster()` kills the restarter + touches the
    paused posture before spawning, so the restarter cannot respawn
    an agent on old code during the checkout / uv sync window.

    Returns {"session": "ava-updater", "log": <path>}.

    Raises:
        ClusterUpdateInProgress: orchestration session `ava-updater` already exists;
            an update is already in flight. Wait for it to finish — a hung updater is
            force-reaped automatically — or terminate the pid named in
            `$AVA_HOME/run/sessions/ava-updater.json` if it is hung.
        MigrationLayoutError: the target commit's migrations/ has a broken layout
            (duplicate / non-contiguous version) or cannot be read; refused before
            anything is paused.
        OrchestrationSpawnFailed: the session backend declined to start the
            updater session.
    """
    deploy_spawn.assert_prod_home_has_its_own_checkout()
    # Standalone self-heals (watchdog pin/code controllers, the management
    # endpoint, an operator's direct `ava cluster update` on a runner) quiesce
    # this host's agents before the bounce — the per-host analogue of the
    # rollout's stop-the-world. A rollout's Phase B passes mode='none' (the
    # gateway-side quiesce already drained the fleet) plus force_reap when the
    # quiesce timed out on stragglers.
    quiesce = mode != "none"
    updater_sess = shared.cluster.session_name(_UPDATER_SERVICE)
    handoff_generation = shared.updater_handoff.new_generation()
    if cluster_session._has_orchestration_session(updater_sess):
        if _updater_hung(updater_sess) and _reap_stalled_updater(updater_sess):
            # "cleared", not "reaped": what lets this update proceed is the session
            # being gone, which holds whether or not the kill is what removed it.
            _log.warning(
                "[cluster] cleared a hung updater session %s (%s); proceeding with new update",
                updater_sess,
                REAP_CLEARED_QUALIFIER,
            )
        else:
            raise ClusterUpdateInProgress(
                f"orchestration session {updater_sess!r} already exists; an update is "
                f"in flight. Wait for it to finish — a hung updater is force-reaped "
                f"automatically — or terminate the pid named in "
                f"$AVA_HOME/run/sessions/{updater_sess}.json if it is hung."
            )

    # Validate before kill: vet target migrations from git read-only before pause/spawn.
    # Otherwise boot discovers a duplicate/non-contiguous layout after `ava restart`
    # stops every service (the 2026-06-17 dup-0049 outage); restart-only reuses current code.
    if not restart_only:
        ref = target_sha if target_sha is not None else f"origin/{settings.general.track_branch}"
        # best-effort fetch so an origin/main vet sees the real latest tip (the
        # updater shell fetches again before it checks out); on a fetch failure
        # (including a timeout) validate_migrations_at_ref fails closed on the
        # unreadable ref. Bounded so a stalled network cannot block this
        # synchronous call indefinitely (see _VALIDATE_FETCH_TIMEOUT_S above).
        try:
            fetch = run_bounded(
                ["git", "fetch", "--progress", "origin"],
                cwd=_REPO_ROOT,
                capture_output=True,
                env=git_env(),
                timeout=_VALIDATE_FETCH_TIMEOUT_S,
            )
            if fetch.returncode != 0:
                _log.warning(
                    "validate-before-kill git fetch failed with rc=%s; stderr=%s; "
                    "validation fails closed if its ref is unreadable",
                    fetch.returncode,
                    fetch.stderr,
                )
        except subprocess.TimeoutExpired as exc:
            _log.warning(
                "validate-before-kill git fetch timed out after %.0fs; last stderr: %r; "
                "proceeding to validate_migrations_at_ref (fails closed if %s is unreadable)",
                _VALIDATE_FETCH_TIMEOUT_S,
                timeout_stderr_tail(exc),
                ref,
            )
        shared.migrations.validate_migrations_at_ref(ref, repo_root=_REPO_ROOT)

    log_path = _new_update_log("updater")
    repo = _REPO_ROOT
    if restart_only:
        # The detached session runs the in-process self-update (R1-6 execution-shape
        # convergence) rather than a hand-built shell ladder: `python -m
        # cli.commands._update_agent_runner --restart-only` is the same code an
        # operator's `ava cluster update` runs on a runner — preflight probes,
        # quiesce, graceful stop, fresh `ava start`. Its decline-vs-failure rc
        # (RESTART_DECLINED vs other) is what the old shell ladder branched on,
        # and it touches the updater lease on entry / clears it in a finally, so
        # the chain-head lease steps the shell carried are gone. `if cd ...; then`
        # rather than `cd ... && <cmd>`: a failed `cd` must not read as a failed
        # bounce with services stopped.
        inner_cmd = (
            f"{{ {venv_activation_prefix()}export AVA_CLI_LOG_NAME=updater; "
            f"(python -m cli.commands._updater_stage run || true); "
            f"if cd {shlex.quote(str(repo))}; "
            f"then python -m cli.commands._update_agent_runner --restart-only"
            f"{_update_entry_args(mode=mode, force_reap=force_reap, handoff_generation=handoff_generation)}; "
            f"rc=$?; "
            f"else rc=$?; echo '[updater] cannot enter the repo; nothing to bounce'; fi; "
            f"(python -m cli.commands._updater_stage final || true); "
            f'echo "[session-exit] rc=$rc"; }} '
            f"2>&1 | tee -a {shlex.quote(str(log_path))}"
        )
        # forward_env_dict has already put this venv's bin dir on the child PATH,
        # so `ava` resolves without an activation prefix.
        native_cmd = (
            f"set AVA_CLI_LOG_NAME=updater && "
            f"python -m cli.commands._updater_lease touch --handoff-generation {_native_arg(handoff_generation)}"
            f" && (python -m cli.commands._updater_stage run || ver>nul)"
            f" && (python -m cli.commands._updater_stage restart || ver>nul)"
            f" && {_restart_recovery_cmd(quiesce=quiesce, mode=mode, force_reap=force_reap)}"
            f" & (python -m cli.commands._updater_stage done || ver>nul)"
            f" & python -m cli.commands._updater_lease clear --handoff-generation {_native_arg(handoff_generation)}"
        )
    else:
        # The detached session runs the in-process self-update (R1-6 execution-shape
        # convergence) — the same `_run_agent_runner_self_update` an operator's
        # `ava cluster update` runs on a runner. It owns the whole ladder the old
        # shell chain hand-built: force-checkout with the post-checkout tree
        # verification (`verify_tree_at` — the 2026-08-02 mixed-tree guard), uv
        # sync, migration-layout vet with revert, preflight probes, quiesce, the
        # graceful stop and a fresh `ava start`. Any pre-stop failure returns
        # non-zero with the host still serving its current code — the ABORT
        # contract (never start services on a possibly-mixed tree), now enforced
        # in Python instead of by the `&&` chain. The lease is touched on entry
        # and cleared in a finally (the chain-head placement PR5 gave the shell).
        # The leading echo names the target for the operator-read log; the venv
        # activation goes ahead of the `if` so it survives into the branch.
        ref = shlex.quote(target_sha) if target_sha else f"origin/{settings.general.track_branch}"
        inner_cmd = (
            f"{{ {venv_activation_prefix()}export AVA_CLI_LOG_NAME=updater; "
            f"(python -m cli.commands._updater_stage run || true); "
            f"if cd {shlex.quote(str(repo))}; "
            f"then echo '[updater] self-update to {ref} via in-process path (force-checkout discards any unpushed local commits / dirty tree; recover via git reflog)'; "
            f"python -m cli.commands._update_agent_runner"
            f"{_update_entry_args(target_sha=target_sha, mode=mode, force_reap=force_reap, handoff_generation=handoff_generation)}; "
            f"rc=$?; "
            f"else rc=$?; echo '[updater] cannot enter the repo; nothing to update'; fi; "
            f"(python -m cli.commands._updater_stage final || true); "
            f'echo "[session-exit] rc=$rc"; }} '
            f"2>&1 | tee -a {shlex.quote(str(log_path))}"
        )
        # cmd.exe evaluates `&&` / `||` left to right at equal precedence, so
        # `a && b && c && (restart-with-recovery) || abort` is exactly the POSIX
        # if/then/else above: the abort runs whenever any step of the checkout
        # chain fails. The two `git diff --quiet` tests are cmd.exe's spelling of
        # the tree verification (worktree vs index, index vs HEAD — untracked
        # strays ignored, same contract as the POSIX chain's verify_tree_at): a raced
        # checkout that landed a mixed tree fails them, and the chain aborts
        # WITHOUT the old `|| ava start` — starting on a possibly-mixed tree is
        # the outage, not the recovery.
        #
        # The `|| (abort)` now fires ONLY for those steps, which is what its own
        # message has always claimed. Every arm of the ladder ends in an `echo`
        # (its verdict), so the ladder's group always succeeds and the abort can no
        # longer be reached through it — where before, a restart that failed AND
        # whose recovering `ava start` also failed fell into a branch that blamed
        # the checkout and then `exit /b`'d past the lease clear. The rc that arm
        # was worth is not lost: it is in the verdict line, which is where the
        # reader looks.
        branch = settings.general.track_branch
        native_ref = _native_arg(target_sha) if target_sha else _native_arg(f"origin/{branch}")
        native_cmd = (
            # Claim the updater lease FIRST (R1, PR5) — same reason as POSIX: the
            # fetch/checkout/sync before the old mid-chain claim left a slow host
            # looking ownerless (stranded-pause controller) mid-update. `ver>nul`
            # is the fail-soft spelling of `|| true`.
            f"set AVA_CLI_LOG_NAME=updater && "
            f"python -m cli.commands._updater_lease touch --handoff-generation {_native_arg(handoff_generation)}"
            f" && (python -m cli.commands._updater_stage run || ver>nul)"
            f" && (python -m cli.commands._source_switch_marker on || ver>nul)"
            f" && echo [updater] force-checkout to {native_ref} -- discards any unpushed "
            f"local commits or a dirty tree, recover via git reflog"
            # Per-step stage markers (Task #1820): each prints a monotonic
            # timestamp ahead of its step, so `ops.updater_outcome` pairs them
            # into the fetch/checkout/uv durations the rollout report shows —
            # the breakdown the brief's Windows 75.9s case was missing. Fail-soft
            # like the source-switch markers: a marker that cannot print costs a
            # stage boundary, never the update. A marker missing because the step
            # BEFORE it failed is the diagnosis — the last marker names where the
            # chain died. The trailing `done` (after the restart, before the
            # lease clear) closes the decision window: it runs only on the
            # success path — the abort arm exits cmd.exe before it.
            f" && (python -m cli.commands._updater_stage fetch || ver>nul)"
            f" && git fetch origin"
            f" && (python -m cli.commands._updater_stage checkout || ver>nul)"
            f" && git checkout --force -B {_native_arg(branch)} {native_ref}"
            f" && git diff --quiet && git diff --cached --quiet"
            f" && (python -m cli.commands._updater_stage uv || ver>nul)"
            f" && python -m cli.commands._update_uv_sync"
            f" && (python -m cli.commands._installed_sha || ver>nul)"  # see that module
            f" && (python -m cli.commands._updater_stage restart || ver>nul)"
            f" && ({_restart_recovery_cmd(quiesce=quiesce, mode=mode, force_reap=force_reap)}) || ("
            f"echo [updater] checkout/sync or tree verification FAILED -- refusing to "
            f"start services on a possibly-mixed tree; the host stays on its current code{SOURCE_SWITCH_OFF}"
            f" & {native_exit_line(1)}"
            # The abort branch clears the lease ITSELF, because the trailing clear
            # below is unreachable from here: `exit /b` outside a batch script exits
            # cmd.exe, so the whole rest of the command line goes with it. That made
            # the cheapest possible failure — a `git fetch` that could not reach
            # origin, over in seconds — the most expensive one to observe: the lease
            # is armed for its full `UPDATER_LEASE_TTL_S` in a single write at the
            # chain's head, so an abort that skipped the clear left the host claiming
            # a live updater for 15 minutes. Phase B reads that claim as "still
            # working" and spends its entire bound on it, and the settle hold then
            # waits on the same host again.
            f" & python -m cli.commands._updater_lease clear --handoff-generation {_native_arg(handoff_generation)}{SOURCE_SWITCH_OFF}"
            f" & exit /b 1)"
            f" & (python -m cli.commands._updater_stage done || ver>nul)"
            f" & python -m cli.commands._updater_lease clear --handoff-generation {_native_arg(handoff_generation)}{SOURCE_SWITCH_OFF}"
        )
    # One log holds every native run, so its tail needs a seam to be read by (#1117).
    native_cmd = mark_native_run(native_cmd)
    # Bracket the spawn so a stall inside it is attributable from the log alone.
    # On 2026-08-12 this call stopped returning on the Windows runner and the only
    # thing the ops log showed was the pause's last line followed by two hours of
    # nothing — with no way to tell whether the spawn had been reached, was in
    # flight, or had returned and something after it hung. The pair below costs two
    # lines per update and answers that question outright; the syscall underneath is
    # still unidentified, so the next occurrence has to be readable off the box.
    _log.info("[cluster] spawning updater session %s (log=%s)", updater_sess, log_path)
    with shared.ui_update_state.lifecycle_lock():
        # Validation/fetch above is intentionally outside this short mutex.
        # Recheck now, then make pause + session visibility indivisible from a
        # recovery actor's no-owner proof.
        live_session = cluster_session.live_orchestration_session()
        if live_session is not None:
            raise ClusterUpdateInProgress(
                f"orchestration session {live_session!r} already exists; an update is in flight"
            )
        try:
            shared.updater_handoff.begin(
                expected_session=updater_sess,
                generation=handoff_generation,
            )
        except shared.updater_handoff.UpdaterHandoffActive as exc:
            raise ClusterUpdateInProgress(
                "an updater spawn handoff still has a live/fresh owner or is "
                "unreadable; wait for it or recover the host"
            ) from exc
        try:
            cluster_pause.pause_local_cluster()
        except BaseException:
            shared.updater_handoff.clear(handoff_generation)
            with contextlib.suppress(Exception):
                cluster_pause.unpause_local_cluster()
            raise
        try:
            cluster_session._spawn_detached_session(
                updater_sess, shell_cmd=inner_cmd, native_cmd=native_cmd
            )
        except cluster_session.OrchestrationSpawnFailed as exc:
            if exc.started is False:
                # A definitive backend decline means there is no child that can
                # recover this pause. An ambiguous post-fork/Popen failure keeps
                # posture paused: the child may be running, and recovery will
                # prove liveness or clear it after the safety bound.
                shared.updater_handoff.clear(handoff_generation)
                cluster_pause.unpause_local_cluster()
            raise
    _log.info("[cluster] spawned updater session %s log=%s", updater_sess, log_path)
    return {"session": updater_sess, "log": str(log_path)}


def spawn_rollout(
    origin: str, *, force: bool = False, mode: str = "smooth", dry_run: bool = False
) -> dict[str, str | bool]:
    """Trigger a rollout via a detached session selected by ``dry_run``.

    A real rollout runs as ``ava-rollout``; an informational, non-mutating
    dry-run runs as ``ava-rollout-dryrun``. The dry-run session is outside the
    orchestration-kind scan, so a live dry-run does not block a real rollout.

    Runs a detached shell command — `{ cd <repo> && <venv-activation> ava
    cluster update --local; } 2>&1 | tee -a <log>` — hosted on the service
    session backend (`get_backend()`; POSIX: native process supervisor,
    Windows: winproc), whose `bash -lc` wrapper re-activates the venv inside
    the command. The `ava cluster update` orchestrator drives the full
    three-phase rollout itself — pause every agent-runner, stop / pull / sync /
    migrate the gateway, fan out the agent-runner self-updates, then poll each
    host back to healthy. `ava` is resolved from this checkout's `.venv/bin`
    (`venv_activation_prefix`), never from a login PATH: the session's env is
    the forwarded env dict, and a login PATH does not necessarily carry
    `~/.local/bin` — when it did not, the whole rollout died instantly with
    `command not found: ava` / `[session-exit] rc=127`. Returns immediately;
    the detached session runs the orchestration to completion.

    `origin` names the trigger (`agent:<id>` / `frontend` / `cli:<machine>`):
    it heads the rollout log, tags this spawn's log line, and rides
    `--origin` into the orchestration so the cluster pin records who moved it.
    The log path rides down the same way (`--rollout-log`), because this side is
    the only one that knows it.

    Unlike `spawn_update`, this does NOT pause the local host first — the
    `ava cluster update` orchestrator manages pausing, migration, and restart of every
    host (including this one). Idempotent: refuses if a rollout or a local
    update is already in flight.

    Returns {"session": <selected session>, "log": <path>,
    "backend_changed": <bool>, "needs_replay": <bool>}. The update-check
    values come from the same preflight that gates the no-op case:
    `backend_changed` tells the caller
    whether this rollout will restart agent processes at all — the frontend
    "Update" button uses it to say agents will be restarted (the one-time SDK
    initiator `ava.self.update()`, which waited for the rollout's restart
    signal only when it would — a frontend-/docs-only rollout never quiesces,
    so waiting would stall — was removed 2026-08; the CLI trigger never waits,
    it returns once the detached session is spawned).
    The value is a snapshot: the detached orchestration re-fetches and
    re-classifies, so a backend commit landing in the seconds between this
    preflight and that re-classification can make a `backend_changed=False`
    answer stale — the frontend then understates the restart; the window is
    seconds wide and the quiesce convergence loop still drains every agent,
    so the degraded path is only a cosmetic mislabel.

    Raises:
        ClusterUpdateInProgress: a `ava-rollout` or
            `ava-updater` orchestration session already exists; an update is
            already in flight. Wait for it to finish — a hung session is
            force-reaped automatically.
        NothingToUpdate: the cluster is already on the latest code
            (`update_check()` reports behind==0 without a required replay). Fail fast before pausing or
            restarting any agent — a rollout with nothing to pull would bounce
            the whole fleet for zero code change. Use `spawn_restart` if the
            intent is to bounce on the current code.
        OrchestrationSpawnFailed: the session backend declined to start the
            rollout session.
    """
    _assert_no_orchestration_in_flight(force=force)

    # Fail fast on a clean no-op rollout. A mismatched installed/running pair is
    # a half-deployed state, not a no-op: the full rollout replays its checkout,
    # sync, and fresh start to reconcile those bookmarks.
    check = update_check()
    if check.behind == 0 and not check.needs_replay:
        raise NothingToUpdate("cluster is already up to date — nothing to roll out")

    rollout_service = _ROLLOUT_DRYRUN_SERVICE if dry_run else _ROLLOUT_SERVICE
    rollout_sess = shared.cluster.session_name(rollout_service)
    log_path = _new_update_log("rollout")

    repo = _REPO_ROOT
    # `ava cluster update --local`, not bare `ava cluster update`: the gateway default is
    # now the thin path (POST /api/cluster/rollout → this spawn), so a bare
    # `ava cluster update` here would re-POST and recurse. `--local` forces the in-process
    # three-phase orchestration this detached session is meant to run.
    # The trailing rc echo rides the same tee into the log: the detached
    # session's exit code would otherwise be lost entirely (the backend keeps
    # only the process, not its verdict), so a crashed orchestration would end
    # its log mid-sentence with no verdict.
    # `--rollout-log` names the file this pipeline tees into. Only this side knows
    # it — the log is created here, seconds before the detached process exists — and
    # the orchestration stamps it onto the last-update record so a failed rollout's
    # banner can name the log rather than leave an operator picking one by mtime.
    inner_cmd = (
        f"{{ echo {shlex.quote(f'[rollout] triggered by: {origin}')}; "
        f"cd {shlex.quote(str(repo))} && {venv_activation_prefix()}"
        f"ava cluster update --local --origin {shlex.quote(origin)} "
        f"--mode {shlex.quote(mode)}{' --dry-run' if dry_run else ''} "
        f"--rollout-log {shlex.quote(str(log_path))}; "
        f'echo "[session-exit] rc=$?"; }} '
        f"2>&1 | tee -a {shlex.quote(str(log_path))}"
    )
    # No `--rollout-log`: this branch has no `tee`, so the path would name an empty
    # log (and a gateway is POSIX-only — it exists for symmetry with the spawns above).
    native_cmd = (
        f"echo [rollout] triggered by {_native_arg(origin)}"
        f" & ava cluster update --local --origin {_native_arg(origin)} --mode {_native_arg(mode)}"
        f"{' --dry-run' if dry_run else ''}"
    )
    # Recovery holds the same short mutex from its no-owner proof through its
    # destructive clear/unpause. Recheck liveness inside it and keep it only
    # until the detached session is visible; the child must acquire this mutex
    # itself before publishing the marker.
    try:
        with shared.ui_update_state.lifecycle_lock():
            _assert_no_orchestration_in_flight(force=force)
            snapshot = shared.ui_update_state.read()
            if snapshot.status != "inactive":
                raise ClusterUpdateInProgress(
                    "a persistent maintenance generation is already active or invalid; "
                    "recover it before starting another rollout"
                )
            cluster_session._spawn_detached_session(
                rollout_sess, shell_cmd=inner_cmd, native_cmd=native_cmd
            )
    except LockTimeoutError as exc:
        raise ClusterUpdateInProgress(
            "another cluster lifecycle action is publishing or recovering an update; retry"
        ) from exc
    if not dry_run:
        _wait_for_ui_owner(session=rollout_sess, kind="rollout", origin=origin)
    _log.info(
        "[cluster] spawned rollout session %s log=%s origin=%s",
        rollout_sess,
        log_path,
        origin,
    )
    return {
        "session": rollout_sess,
        "log": str(log_path),
        "backend_changed": check.backend_changed,
        "needs_replay": check.needs_replay,
    }


def spawn_restart(
    origin: str,
    *,
    force: bool = False,
    mode: str = "smooth",
    continuation: PitrRestartContinuation | None = None,
    bind_continuation: Callable[[], None] | None = None,
) -> dict[str, str]:
    """Trigger a whole-cluster *restart* (no pull) via a detached session.

    The detached `ava cluster update --local --restart-only` child uses the same
    session backend and three-phase orchestration as rollout, without changing
    the checkout. It is mutually exclusive with rollout and update sessions.

    Raises:
        ClusterUpdateInProgress: a restart / rollout / update is already in flight.
        OrchestrationSpawnFailed: the session backend declined to start the
            restart session.
    """
    _assert_no_orchestration_in_flight(force=force)

    restart_sess = shared.cluster.session_name(_CLUSTER_RESTART_SERVICE)
    log_path = _new_update_log("cluster-restart")

    repo = _REPO_ROOT
    resume, native_resume = resume_commands(continuation, origin, _native_arg)
    inner_cmd = (
        f"{{ echo {shlex.quote(f'[cluster-restart] triggered by: {origin}')}; "
        f"cd {shlex.quote(str(repo))} && {venv_activation_prefix()}"
        f"ava cluster update --local --restart-only --origin {shlex.quote(origin)} "
        f"--mode {shlex.quote(mode)}{resume}; "
        f'echo "[session-exit] rc=$?"; }} '
        f"2>&1 | tee -a {shlex.quote(str(log_path))}"
    )
    native_cmd = (
        f"echo [cluster-restart] triggered by {_native_arg(origin)}"
        f" & ava cluster update --local --restart-only --origin {_native_arg(origin)}"
        f" --mode {_native_arg(mode)}" + native_resume
    )
    try:
        with shared.ui_update_state.lifecycle_lock():
            _assert_no_orchestration_in_flight(force=force)
            snapshot = shared.ui_update_state.read()
            if snapshot.status != "inactive":
                raise ClusterUpdateInProgress(
                    "a persistent maintenance generation is already active or invalid; "
                    "recover it before starting another restart"
                )
            if bind_continuation is not None:
                if continuation is None:
                    raise ValueError("restart handoff binder requires a typed continuation")
                bind_continuation()
            cluster_session._spawn_detached_session(
                restart_sess, shell_cmd=inner_cmd, native_cmd=native_cmd
            )
    except LockTimeoutError as exc:
        raise ClusterUpdateInProgress(
            "another cluster lifecycle action is publishing or recovering an update; retry"
        ) from exc
    _wait_for_ui_owner(session=restart_sess, kind="restart", origin=origin)
    _log.info(
        "[cluster] spawned cluster-restart session %s log=%s origin=%s",
        restart_sess,
        log_path,
        origin,
    )
    return {"session": restart_sess, "log": str(log_path)}
