"""Trigger a whole-cluster update / rollout / restart, and refuse a second one.

The top of the `ops/cluster*` family: the read-only "is there anything to roll
out" preflight, the three detached-session triggers, the mutual exclusion that
keeps two of them from fighting over one host's services, and the reaper that
cuts a hung updater session loose so it stops blocking every later attempt —
asked both by the next attempt and, because a hung session refuses that next
attempt cluster-wide before it can arrive, once a watchdog round
(`ops.controllers.stalled_updater`).

**This file sits in the 500-800 transitional zone on purpose, not by oversight.**
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

import logging
import shlex
import subprocess
import time
from pathlib import Path

import shared.cluster
import shared.migrations
import shared.paths
from ops import cluster_pause, cluster_session
from ops._update_shell import SOURCE_SWITCH_OFF, SOURCE_SWITCH_ON, _restart_recovery_cmd
from ops.cluster_session import (
    _CLUSTER_RESTART_SERVICE,
    _ORCHESTRATION_KINDS,
    _REPO_ROOT,
    _ROLLOUT_SERVICE,
    _UPDATER_SERVICE,
    _native_arg,
)
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
from shared.config import settings
from shared.deploy_timing import NO_PROGRESS_TIMEOUT_S
from shared.gitenv import git_env
from shared.proc import run_bounded
from shared.session_env import venv_activation_prefix


class ClusterUpdateInProgress(RuntimeError):  # noqa: N818 — state description, same style as ResurrectAlreadyAlive
    """The `ava-updater` session already exists — an update is in flight.

    Triggered when /api/cluster/update is called while the `ava-updater`
    session is alive. Racing two update sessions is the 2026-05-25
    incident: both call `ava stop` concurrently, kill each other's sessions
    sessions, leave the cluster fully dead with no one to run `ava start`.

    Surfaced as HTTP 409 by the FastAPI handler; caller should wait for
    the in-flight update to finish (poll /api/cluster/status until
    paused=false) and only then retry.
    """


class NothingToUpdate(RuntimeError):  # noqa: N818 — state description, same style as ClusterUpdateInProgress
    """The cluster is already on the latest code — `update_check()` reports behind==0.

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


# The most a True from `_reap_stalled_updater` may claim. Both call sites — `spawn_update`
# below and `ops.controllers.stalled_updater` — interpolate it instead of wording the
# ambiguity themselves, which is how one of them drifted into claiming a kill nobody made.
REAP_CLEARED_QUALIFIER = "killed, or already exited by the recheck"


def _updater_hung(session: str) -> bool:  # noqa: ARG001 — the session name is the judgment's subject; the evidence is the lease
    """Whether this host's updater session is hung — the lease-expiry judgment.

    R1 (Task #1021): liveness = the updater's lease in `host_deploy_state`
    (`shared.host_deploy_state.touch_updater_lease`, written first thing by the
    update chains and the in-process self-update). A live lease means "still
    working"; a lease armed by THIS pause window that has run out means the
    updater stopped renewing and is hung (`HostDeployState.updater_expired` —
    which is also what stops a *previous* run's uncleared expiry from condemning
    the session this update just spawned). A session with NO lease cannot be
    judged — a lease write that failed at entry, or an updater spawned before the
    lease existed — and reads as NOT hung (the old log-mtime fallback was retired
    with the old-signal sweep, PR5; killing on missing evidence is the worse
    mistake).
    """
    from shared.host_deploy_state import read

    try:
        state = read()
    except Exception:
        return False  # unreadable liveness -> not hung; the caller refuses instead
    return state is not None and state.updater_expired


def _reap_stalled_updater(session: str) -> bool:
    """True if `session` (the ava-updater session) is gone because this call cleared it.

    A hung updater session — its lease expired — blocks every future update,
    rollout, restart, and even /api/cluster/recover on this host until a human
    kills the session by hand. The caller supplies the liveness half
    (`_updater_hung`); this function kills and handles the kill race.
    """
    # The updater session lives on the service session backend (S7 — the same
    # backend as every other session; before S7 it stayed on its own), so the kill
    # goes to get_backend(), never get_shell_backend().
    from shared.session_backend import get_backend

    _log.warning("[cluster] %s judged hung — force-killing as hung", session)
    try:
        killed, mode = get_backend().kill_session(session, graceful=False, expected=True)
    except Exception:
        _log.exception("[cluster] failed to kill stalled updater session %s", session)
        return False
    if not killed:
        # `ok=False` now means the backend re-asked `has-session` and the session was
        # still there (issue #1015 made that the contract; it used to be the raw
        # kill-session exit status, which is *also* non-zero for a target that
        # cannot be found — indistinguishable from a session that finished on its own
        # between the caller's liveness check and this kill). Re-ask anyway. It costs
        # one `has-session` and it is the only question that matters here, so this
        # stays correct whichever way a future backend answers `ok`; treating a benign
        # race as a failure would make `spawn_update` raise `ClusterUpdateInProgress`
        # naming a session that is not there, blocking an update for no reason.
        try:
            still_there = cluster_session._has_orchestration_session(session)
        except Exception:
            # Cannot tell: keep the error. A false "cleared" is the expensive
            # direction — it spawns a second updater over a live one, which is the
            # 2026-05-25 incident that `ClusterUpdateInProgress` exists to prevent.
            still_there = True
        if not still_there:
            _log.info(
                "[cluster] %s exited on its own before the kill landed — nothing to reap",
                session,
            )
            return True
        # It survived its own kill. Reporting it as reaped would tell `spawn_update`
        # to spawn over a session that is still there, and would make the watchdog
        # round log a fresh "force-killing as hung" every 60s while claiming success.
        _log.error(
            "[cluster] backend declined to kill stalled updater session %s (mode=%s); "
            "kill it by hand: terminate the pid named in "
            "$AVA_HOME/run/sessions/%s.json",
            session,
            mode,
            session,
        )
        return False
    return True


def reap_stalled_updater_if_hung() -> bool:
    """Kill this host's `ava-updater` session if it is alive and has stopped writing.

    The scheduled half of the reaper — `ops.controllers.stalled_updater` calls this
    once a watchdog round, so a hung updater is cut loose on a clock of its own
    instead of waiting for someone to try to deploy.

    **Waiting for the next attempt does not work, because the corpse refuses it.**
    A live `ava-updater` makes `current_orchestration()` answer `"update"` forever,
    and that answer is read by everything that could have cleared it:
    `_assert_no_orchestration_in_flight` fails the gateway's next `ava cluster update`
    cluster-wide on signal 3 of `ops.deploy_window` (so Phase B never fans out, and
    the `spawn_update` that carries the other reap call is never reached), and this
    host's own **code**, **pin** and **stranded-pause** controllers all defer to it as
    the normal mid-deploy transient. Pin and pause once gated on the
    `update_lock_holder()` lease alone — which a watchdog-spawned `spawn_update` never
    takes, so an off-pin host reached `spawn_update`'s inline reap below and recovered
    on its own — but that same blindness let the pin controller force-checkout
    underneath a live updater and flap prod between two commits (issue #1074), so all
    three now read the session. A hung session therefore refuses the cluster's deploy
    path and every one of this host's self-heals, and the only exits are `--force`
    (which also suppresses the real deploy-window protection this is impersonating) or
    a human killing the pid named in `$AVA_HOME/run/sessions/ava-updater.json`. Bounding
    the Phase-B poll does not touch that:
    the poll protects a rollout that *started*, and this refuses the start.

    Returns True when the hung session is gone on our account — killed here, or found
    already exited on the recheck after a kill that reported failure (see
    `_reap_stalled_updater`, which treats that benign race as cleared). False means
    nothing was hung, or the session survived its own kill. Never raises — it runs
    inside a controller round.
    """
    try:
        session = shared.cluster.session_name(_UPDATER_SERVICE)
        if not cluster_session._has_orchestration_session(session):
            return False
        if not _updater_hung(session):
            return False
        return _reap_stalled_updater(session)
    except Exception:
        _log.exception("[cluster] stalled-updater reap failed; retrying next round")
        return False


# The family's one no-progress definition (`shared.deploy_timing`), shared with the
# settle-hold TTL and the gateway's Phase-B poll: three clocks that used to disagree
# about when a host has stopped making progress, and the smallest of them decided
# when the deploy lease stopped protecting the deploy.
_UPDATER_STALL_TIMEOUT_S: float = NO_PROGRESS_TIMEOUT_S


def _assert_no_orchestration_in_flight(*, force: bool = False) -> None:
    """Refuse a new whole-cluster op while a deploy is in flight **anywhere in the
    cluster**, not just on this host.

    The whole-cluster orchestration sessions — at most one may run at a time on
    the gateway (rollout, restart) or this host (updater). They all drive
    stop/start against the shared paused state, so they must be mutually
    exclusive: a rollout starting under a restart (or vice versa) would have two
    `ava cluster update` orchestrations fighting over the same services.

    The local session check alone was not mutual exclusion for the *cluster*. On
    2026-07-28 a supervised rollout had finished its gateway leg — local session
    gone, update lock released — while the Windows host was still running its own
    Phase-B updater. A second `ava cluster update` started here, saw a clean local host,
    advanced the pin to an unreviewed commit and quiesced into the first rollout's
    window, force-terminating two agents. So the question asked here is the
    cluster-wide one (`ops.deploy_window`), which sees that tail.

    `force=True` is the deliberate operator override for a deploy window that is
    real but stale — evidence this cannot reason about (a host powered off
    mid-update, say). It skips the cluster-wide question but NOT the local session
    check: two orchestrations on one host fighting over the same services is never
    something an operator means to do, and the local check has a precise remedy
    (kill the named session).

    Raises:
        ClusterUpdateInProgress: an orchestration session exists here, or a deploy
            is in flight elsewhere in the cluster.
    """
    for service, _ in _ORCHESTRATION_KINDS:
        session = shared.cluster.session_name(service)
        if cluster_session._has_orchestration_session(session):
            raise ClusterUpdateInProgress(
                f"orchestration session {session!r} already exists; a rollout / restart "
                f"/ update is in flight. Wait for it to finish — a hung session is "
                f"force-reaped automatically — or terminate the pid named in "
                f"$AVA_HOME/run/sessions/{session}.json if it is hung."
            )
    if force:
        _log.warning("[cluster] --force: skipping the cluster-wide deploy-window check")
        return
    from ops.deploy_window import deploy_in_flight

    window = deploy_in_flight()
    if window.active:
        raise ClusterUpdateInProgress(
            f"a deploy is already in flight: {window.detail}. Two concurrent deploys "
            f"defeat the rollout's own safety (each pins its own commit and quiesces "
            f"into the other's window — the 2026-07-28 collision cost two agents). "
            f"Wait for `ava cluster status` to show every host on the pin, or re-run "
            f"with --force if you are certain that deploy is dead."
        )


def _update_entry_args(
    *,
    target_sha: str | None = None,
    mode: str,
    force_reap: bool,
) -> str:
    """The `python -m cli.commands._update_agent_runner` flag tail for the detached
    POSIX updater session (R1-6 execution-shape convergence).

    `--mode` is always spelled out so the operator-read updater log shows the
    drain policy; `--target-sha` only when the rollout pinned one (absent = the
    entry resolves the track ref itself — a watchdog self-heal catching the host
    up); `--force-reap` only when set (Phase B's quiesce-timeout backstop).
    """
    args = f" --mode {shlex.quote(mode)}"
    if target_sha:
        args += f" --target-sha {shlex.quote(target_sha)}"
    if force_reap:
        args += " --force-reap"
    return args


def spawn_update(
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
    # Standalone self-heals (watchdog pin/code controllers, the management
    # endpoint, an operator's direct `ava cluster update` on a runner) quiesce
    # this host's agents before the bounce — the per-host analogue of the
    # rollout's stop-the-world. A rollout's Phase B passes mode='none' (the
    # gateway-side quiesce already drained the fleet) plus force_reap when the
    # quiesce timed out on stragglers.
    quiesce = mode != "none"
    updater_sess = shared.cluster.session_name(_UPDATER_SERVICE)
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

    # validate-before-kill: refuse a target whose migrations/ layout is broken
    # (duplicate / non-contiguous version) BEFORE pausing or spawning the updater.
    # Such breakage is otherwise only discovered at boot — after `ava restart`
    # stops every service — taking the whole cluster down (the 2026-06-17 dup-0049
    # outage). Fetch the target read-only and vet its migrations/ from git; a broken
    # layout raises here (surfacing to the caller as a refused update) with the
    # cluster still serving its current code, nothing paused. A restart_only bounce
    # reuses the current code (no checkout / migration), so it has nothing to vet.
    if not restart_only:
        ref = target_sha if target_sha is not None else f"origin/{settings.general.track_branch}"
        # best-effort fetch so an origin/main vet sees the real latest tip (the
        # updater shell fetches again before it checks out); on a fetch failure
        # (including a timeout) validate_migrations_at_ref fails closed on the
        # unreadable ref. Bounded so a stalled network cannot block this
        # synchronous call indefinitely (see _VALIDATE_FETCH_TIMEOUT_S above).
        try:
            run_bounded(
                ["git", "fetch", "origin"],
                cwd=_REPO_ROOT,
                capture_output=True,
                env=git_env(),
                timeout=_VALIDATE_FETCH_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            _log.warning(
                "validate-before-kill git fetch timed out after %.0fs; proceeding "
                "to validate_migrations_at_ref against whatever is already local "
                "(fails closed if %s is unreadable)",
                _VALIDATE_FETCH_TIMEOUT_S,
                ref,
            )
        shared.migrations.validate_migrations_at_ref(ref, repo_root=_REPO_ROOT)

    cluster_pause.pause_local_cluster()

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
            f"{{ {venv_activation_prefix()}"
            f"if cd {shlex.quote(str(repo))}; "
            f"then python -m cli.commands._update_agent_runner --restart-only"
            f"{_update_entry_args(mode=mode, force_reap=force_reap)}; "
            f"rc=$?; "
            f"else rc=$?; echo '[updater] cannot enter the repo; nothing to bounce'; fi; "
            f'echo "[session-exit] rc=$rc"; }} '
            f"2>&1 | tee -a {shlex.quote(str(log_path))}"
        )
        # forward_env_dict has already put this venv's bin dir on the child PATH,
        # so `ava` resolves without an activation prefix.
        native_cmd = (
            "(python -m cli.commands._updater_lease touch || ver>nul)"
            f" && {_restart_recovery_cmd(quiesce=quiesce, mode=mode, force_reap=force_reap)}"
            " & python -m cli.commands._updater_lease clear"
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
            f"{{ {venv_activation_prefix()}"
            f"if cd {shlex.quote(str(repo))}; "
            f"then echo '[updater] self-update to {ref} via in-process path (force-checkout discards any unpushed local commits / dirty tree; recover via git reflog)'; "
            f"python -m cli.commands._update_agent_runner"
            f"{_update_entry_args(target_sha=target_sha, mode=mode, force_reap=force_reap)}; "
            f"rc=$?; "
            f"else rc=$?; echo '[updater] cannot enter the repo; nothing to update'; fi; "
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
            f"(python -m cli.commands._updater_lease touch || ver>nul){SOURCE_SWITCH_ON}"
            f" & echo [updater] force-checkout to {native_ref} -- discards any unpushed "
            f"local commits or a dirty tree, recover via git reflog"
            f" & git fetch origin"
            f" && git checkout --force -B {_native_arg(branch)} {native_ref}"
            f" && git diff --quiet && git diff --cached --quiet"
            f" && uv sync"
            f" && (python -m cli.commands._installed_sha || ver>nul)"  # see that module
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
            f" & python -m cli.commands._updater_lease clear{SOURCE_SWITCH_OFF}"
            f" & exit /b 1)"
            f" & python -m cli.commands._updater_lease clear{SOURCE_SWITCH_OFF}"
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
    try:
        cluster_session._spawn_detached_session(
            updater_sess, shell_cmd=inner_cmd, native_cmd=native_cmd
        )
    except Exception:
        # Roll back: pause was set, but the updater session never started — leaving
        # the cluster paused with no recovery in flight would strand agents. The
        # posture row marked by that same pause is rolled back with it: no
        # updater ever ran, so nothing is updating and the gate must not say so.
        # (The old flag files were retired with the old-signal sweep, PR5.)
        from shared.host_deploy_state import set_posture

        set_posture("idle")
        raise
    _log.info("[cluster] spawned updater session %s log=%s", updater_sess, log_path)
    return {"session": updater_sess, "log": str(log_path)}


def spawn_rollout(
    origin: str, *, force: bool = False, mode: str = "smooth"
) -> dict[str, str | bool]:
    """Trigger the whole-cluster rollout via a detached `ava-rollout` session.

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

    Returns {"session": "ava-rollout", "log": <path>,
    "backend_changed": <bool>}. `backend_changed` comes from the same
    `update_check()` preflight that gates the no-op case: it tells the caller
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
            (`update_check()` reports behind==0). Fail fast before pausing or
            restarting any agent — a rollout with nothing to pull would bounce
            the whole fleet for zero code change. Use `spawn_restart` if the
            intent is to bounce on the current code.
        OrchestrationSpawnFailed: the session backend declined to start the
            rollout session.
    """
    _assert_no_orchestration_in_flight(force=force)

    # Fail fast on a no-op rollout: behind==0 means origin/main has nothing the
    # running commit lacks, so a rollout would restart every agent and discard
    # their warm in-flight state for zero code change. update_check() is the
    # truth source (it fetches origin/main, then rev-lists against the running
    # commit). The extra fetch here is acceptable — it is the same preflight the
    # UI polls. A restart (spawn_restart) pulls nothing and is intentionally not
    # gated on behind.
    check = update_check()
    if check.behind == 0:
        raise NothingToUpdate("cluster is already up to date — nothing to roll out")

    rollout_sess = shared.cluster.session_name(_ROLLOUT_SERVICE)
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
        f"--mode {shlex.quote(mode)} "
        f"--rollout-log {shlex.quote(str(log_path))}; "
        f'echo "[session-exit] rc=$?"; }} '
        f"2>&1 | tee -a {shlex.quote(str(log_path))}"
    )
    # No `--rollout-log`: this branch has no `tee`, so the path would name an empty
    # log (and a gateway is POSIX-only — it exists for symmetry with the spawns above).
    native_cmd = (
        f"echo [rollout] triggered by {_native_arg(origin)}"
        f" & ava cluster update --local --origin {_native_arg(origin)} --mode {_native_arg(mode)}"
    )
    cluster_session._spawn_detached_session(
        rollout_sess, shell_cmd=inner_cmd, native_cmd=native_cmd
    )
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
    }


def spawn_restart(origin: str, *, force: bool = False, mode: str = "smooth") -> dict[str, str]:
    """Trigger a whole-cluster *restart* (no pull) via a detached session.

    Runs a detached shell command — `{ cd <repo> && <venv-activation> ava
    cluster update --local --restart-only; } 2>&1 | tee -a <log>` — hosted on the
    service session backend (`get_backend()`; `ava` from this checkout's
    `.venv/bin`, never a login PATH — see `spawn_rollout`).
    `--local` keeps the detached child in-process; without it, the default
    `--restart-only` route would POST again and collide with this child's live
    session. The three-phase orchestration pauses every runner, quiesces agents,
    stops / starts this host, and fans out runner bounces, but skips git pull /
    uv sync / migration; services bounce without touching the checked-out revision.

    `origin` names the trigger, same convention as `spawn_rollout` (heads the
    log; no pin involvement — a restart pins nothing).

    Mutually exclusive with rollout / update: refuses if a `ava-cluster-restart`,
    `ava-rollout`, or `ava-updater` session is already alive.

    Returns {"session": "ava-cluster-restart", "log": <path>}.

    Raises:
        ClusterUpdateInProgress: a restart / rollout / update is already in flight.
        OrchestrationSpawnFailed: the session backend declined to start the
            restart session.
    """
    _assert_no_orchestration_in_flight(force=force)

    restart_sess = shared.cluster.session_name(_CLUSTER_RESTART_SERVICE)
    log_path = _new_update_log("cluster-restart")

    repo = _REPO_ROOT
    inner_cmd = (
        f"{{ echo {shlex.quote(f'[cluster-restart] triggered by: {origin}')}; "
        f"cd {shlex.quote(str(repo))} && {venv_activation_prefix()}"
        f"ava cluster update --local --restart-only --mode {shlex.quote(mode)}; "
        f'echo "[session-exit] rc=$?"; }} '
        f"2>&1 | tee -a {shlex.quote(str(log_path))}"
    )
    native_cmd = (
        f"echo [cluster-restart] triggered by {_native_arg(origin)}"
        f" & ava cluster update --local --restart-only --mode {_native_arg(mode)}"
    )
    cluster_session._spawn_detached_session(
        restart_sess, shell_cmd=inner_cmd, native_cmd=native_cmd
    )
    _log.info(
        "[cluster] spawned cluster-restart session %s log=%s origin=%s",
        restart_sess,
        log_path,
        origin,
    )
    return {"session": restart_sess, "log": str(log_path)}
