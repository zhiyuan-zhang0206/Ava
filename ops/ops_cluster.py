"""Cluster-ops RPC implementations.

Local pause / resume / recover / stopping-announce + the update / rollout /
restart spawns + the read-only update-check / status snapshot. One of the four
op clusters split out of the former single `ops/operations.py` (the others
are ops_lifecycle / ops_config / ops_inventory); each cluster is self-contained.

Most of these are thin wrappers — the real work lives in `gateway/cluster.py`;
this layer is the agent-runner-callable RPC surface the ops server dispatches
(`services/agent_ops/daemon.py:_dispatch`) and the gateway cluster router calls.
"""

from __future__ import annotations

from ops.cluster import (
    ClusterStatus,
    ClusterUpdateInProgress,
    UpdateCheck,
    pause_local_cluster,
    spawn_restart,
    spawn_rollout,
    spawn_update,
    status_snapshot,
    unpause_local_cluster,
    update_check,
)
from ops.cluster_status import agent_shell_sessions, capture_shell
from ops.rpc_schemas import ShellCaptureResult, ShellProbeResult
from shared.cluster_lock import force_release_update_lock, read_update_lease
from shared.gitenv import git_env
from shared.host_deploy_state import updater_lease_live
from shared.log import logger
from shared.machine import machine_name
from shared.machines import mark_stopping
from shared.proc import process_alive, run_bounded

# `cluster_fetch_op`'s two git calls. The fetch ceiling is generous (the whole
# point of the pre-flight is to find out whether this host can reach the remote);
# the local rev-parse touches only the object store. Both bounds are enforced by
# `run_bounded`, so a stalled fetch cannot leave a live git/ssh tail behind on a
# host the rollout then declares unreachable.
_FETCH_TIMEOUT_S = 30.0
_RESOLVE_TIMEOUT_S = 5.0


def cluster_stop_op() -> dict[str, object]:
    """Local pause — posture row -> paused + kill restarter."""
    pause_local_cluster()
    return {}


def cluster_resume_op() -> dict[str, object]:
    """Local unpause — posture row -> idle + respawn restarter (idempotent)."""
    unpause_local_cluster()
    return {}


# `_lock_holder_is_live`'s pid-recycling slack. A genuine holder PROCESS existed
# before it acquired the lease, so a probed process whose start time is
# meaningfully AFTER the acquire moment is a recycled pid, not the holder. The
# slack absorbs pg-vs-local clock fuzz (the probe host is the holder's own box,
# and every gateway-capable host runs its DB locally, so the skew is NTP-grade)
# and errs toward "live": a false "recycled" verdict would let recovery clear the
# lease under a running rollout — the 2026-06-01 collision class.
_HOLDER_START_SLACK_S = 30.0


def _lock_holder_is_live(holder: str, *, held_for_s: float | None = None) -> bool:
    """Whether `holder` (the update-lock owner string `<machine>:pid<N>`, minted by
    cli/commands/update.py:_run_gateway_orchestration) names a process that is
    still running on THIS host.

    A holder on a different machine cannot be probed locally — treat it as live so
    recovery never clobbers another gateway's lock. An unparseable holder is
    likewise treated as live (refuse rather than risk clobbering a real run).

    `held_for_s` (the lease's server-computed age, `DeployLease.held_for_s`) arms
    the pid-recycling check: the holder string carries no start time, but a real
    holder process predates its own acquire, so a live pid whose process STARTED
    after the acquire (+ slack) is the pid's next occupant, not the holder — dead
    for recovery purposes. Without `held_for_s` the probe is bare liveness, as
    before. This matters at recover's timescale: a 30-minute TTL is exactly the
    window in which a busy host recycles the dead orchestration's pid.
    """
    machine, sep, pid_str = holder.partition(":pid")
    if sep == "" or machine != machine_name():
        return True
    try:
        pid = int(pid_str)
    except ValueError:
        return True
    if not process_alive(pid):
        return False
    if held_for_s is None:
        return True
    import time

    import psutil

    try:
        started = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return False  # exited between the two probes
    except psutil.Error:
        return True  # unreadable identity — refuse rather than clobber
    return started <= (time.time() - held_for_s) + _HOLDER_START_SLACK_S


def cluster_recover_op() -> dict[str, object]:
    """Operator stranded-cluster recovery — force-clear a pause + update lock that
    a hard-killed rollout left behind.

    Refuses (raises ClusterUpdateInProgress) when a deploy is actually alive, via
    two authoritative checks — a deploy lease whose holder PROCESS is still
    running (pid-probed when the holder is this host; a holder elsewhere cannot
    be probed and is conservatively treated as live), OR this host's live updater
    lease (the lease-less watchdog-spawned updater the deploy lease cannot see).
    Only when neither holds is the paused/locked state stale and safe to
    force-clear. This is the immediate manual counterpart to the watchdog's
    auto-recovery, which instead waits out the lock TTL; probing the holder pid
    lets the manual path clear at once without that wait, while still never
    racing a live run.

    The pid-probe gates the lease refusal rather than following it: the lease is
    renewed by its holder and outlives a crashed one by up to its full TTL, so an
    un-probed "the lease says a rollout is executing" refusal blocks recovery for
    exactly the window this op exists to skip (2026-08-12: a rollout hard-killed
    by its own stop leg left a live-looking lease, and recovery refused on it for
    the rest of the TTL with the holder pid provably dead).

    Order: clear the lock first, then unpause — so a failure clearing the lock
    leaves the cluster paused (the safe, still-wedged state) rather than unpaused
    with a stale lock that would block the next rollout's acquire.

    Returns {"unlocked_holder": <prior lock holder or None>}.
    """
    lease = read_update_lease()
    if lease is not None and _lock_holder_is_live(lease.holder, held_for_s=lease.held_for_s):
        what = lease.kind or "deploy"
        raise ClusterUpdateInProgress(
            f"the cluster deploy lease ({what}) is held by a live process "
            f"({lease.holder}) — recovery refused; wait for it to finish or kill it "
            "first. A holder on another machine cannot be probed from here: run "
            "recover there, or wait out the lease TTL"
        )
    if updater_lease_live():
        raise ClusterUpdateInProgress(
            "an update is in flight on this host — its updater lease is live; "
            "recovery refused; wait for it to finish or kill its session first"
        )
    cleared = force_release_update_lock()
    unpause_local_cluster()
    logger.info(
        "[cluster] manual recover: force-released lock (was {holder}) + unpaused", holder=cleared
    )
    return {"unlocked_holder": cleared}


def cluster_stopping_op(machine: str, home: str) -> dict[str, str]:
    """Record an intentional shutdown announced by the (machine, home) unit.

    `ava stop` calls this (best-effort) just before tearing the local stack
    down, so the cluster view shows the host as "stopped" rather than "offline"
    (a live probe cannot tell an intentional stop from a crash). Stamps the
    unit's `stopped_at` and recomputes the composed `machines` row; `ava start`
    clears it. `home` is the stopping unit's $AVA_HOME, sent on the wire so a
    co-located peer's caps are not retracted along with this unit's.
    """
    mark_stopping(machine, home)
    return {"machine": machine}


def cluster_update_op(
    *,
    restart_only: bool = False,
    target_sha: str | None = None,
    mode: str = "smooth",
    force_reap: bool = False,
) -> dict[str, str]:
    """Run `spawn_update()` and return the new orchestration session metadata.

    `target_sha` is the rollout's pinned commit (Phase B forwards it so this host
    force-checks-out the same commit as every other node); absent, spawn_update
    catches up to origin/main. `restart_only=True` (the agent-runner leg of a cluster
    restart) bounces services on the current code with no checkout / uv sync.
    `mode` sets the agent-drain policy (smooth/force; Phase B passes 'none' — the
    gateway-side quiesce already drained the fleet) and `force_reap` is the
    quiesce-timeout backstop that kills a host's still-live agents before the
    bounce (Phase B's stragglers).

    Phase A (cluster_stop_op, dialed separately) has already paused this host
    before this call ever lands. If spawn_update raises before it manages to spawn
    the `ava-updater` orchestration session (e.g. MigrationLayoutError from the
    validate-before-kill vet, or any other pre-spawn failure), nothing on this host
    will ever run `ava start`/`ava restart` to clear the pause — the host would sit
    paused until the gateway's compensating /api/cluster/resume lands (which can
    itself be delayed or dropped) or the 10-minute stranded-pause watchdog fires.
    Self-heal immediately instead: unpause locally (idempotent — a later resume
    call is harmless) and re-raise so the caller still sees the failure.

    A `ClusterUpdateInProgress` means an update/rollout/restart is genuinely
    already running on this host — that in-flight run owns the pause, so it must
    not be touched here.
    """
    try:
        return spawn_update(
            restart_only=restart_only,
            target_sha=target_sha,
            mode=mode,
            force_reap=force_reap,
        )
    except ClusterUpdateInProgress:
        raise
    except Exception:
        logger.warning(
            "[cluster] cluster_update_op failed before spawning ava-updater; "
            "self-unpausing this host immediately rather than waiting on the "
            "gateway's compensating resume or the stranded-pause watchdog",
            exc_info=True,
        )
        unpause_local_cluster()
        raise


def cluster_rollout_op(origin: str, *, mode: str = "smooth") -> dict[str, str | bool]:
    """Run `spawn_rollout()` and return the new orchestration session metadata + rollout scope.

    `mode` is the agent-drain policy (smooth/force) the detached orchestration
    applies to its quiesce step."""
    return spawn_rollout(origin, mode=mode)


def cluster_restart_op(origin: str, *, mode: str = "smooth") -> dict[str, str]:
    """Run `spawn_restart()` and return the new orchestration session metadata."""
    return spawn_restart(origin, mode=mode)


def cluster_update_check_op() -> UpdateCheck:
    """Read-only preflight — is there anything to roll out, and what would restart."""
    return update_check()


def cluster_status_op() -> ClusterStatus:
    """Local snapshot — assembled by `status_snapshot()`."""
    return status_snapshot()


def shell_probe_op(agent_id: int) -> ShellProbeResult:
    """This host's live persistent-shell sessions for one agent.

    The runner-side half of the inspector panel's `shells` list: the gateway
    dispatches this op when the agent runs on this machine rather than on the
    gateway's own box (`agent_shell_sessions` is host-scoped, so a local probe
    on the gateway would always read empty for a remote agent).
    """
    return ShellProbeResult(shells=agent_shell_sessions(agent_id))


def shell_capture_op(agent_id: int, session_id: int, lines: int = 200) -> ShellCaptureResult:
    """Capture one of an agent's persistent shells' terminal tail, locally.

    The runner-side half of the shell-monitor endpoint (`capture_shell` —
    resolves the session against this host's pty sessions, reconstructs the full
    session name, runs capture-pane). The gateway dispatches this op when the
    agent runs on this machine; `capture_shell` raises ShellNotFoundError /
    RuntimeError when the session is absent or died mid-capture, which the ops
    daemon surfaces as a 'failed' op result.

    Raises:
        ShellNotFoundError: no live shell with `session_id` on this host.
        RuntimeError: the session capture failed.
    """
    full_name, captured = capture_shell(agent_id, session_id, lines)
    return ShellCaptureResult(session_name=full_name, lines=captured)


def cluster_fetch_op() -> dict[str, object]:
    """Run `git fetch origin` on this agent-runner — a lightweight pre-flight
    that confirms this host can reach the remote and has the objects needed for
    the upcoming rollout's pinned target.

    Non-disruptive: does NOT pause the restarter or write the paused posture,
    or restart any service. The caller (the gateway's rollout orchestration)
    fans this out to every agent-runner *before* Phase A so a fetch failure
    aborts the rollout with nothing paused.

    Returns:
        ``{"ok": True, "fetched": "<sha or empty>", "elapsed_s": <float>}``
        on success; ``{"ok": False, "error": "<message>"}`` on failure.
    """
    import subprocess
    import time

    from shared.config import settings
    from shared.paths import repo_root

    t0 = time.monotonic()
    try:
        result = run_bounded(
            ["git", "fetch", "origin"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            env=git_env(),
            timeout=_FETCH_TIMEOUT_S,
        )
        elapsed = time.monotonic() - t0
        if result.returncode == 0:
            # Resolve the track ref HEAD (settings.general.track_branch, not
            # hardcoded origin/main — staging/preview clusters track another
            # branch) to confirm the fetch landed objects.
            track_ref = f"origin/{settings.general.track_branch}"
            resolve = run_bounded(
                ["git", "rev-parse", track_ref],
                cwd=repo_root(),
                capture_output=True,
                text=True,
                env=git_env(),
                timeout=_RESOLVE_TIMEOUT_S,
            )
            fetched = resolve.stdout.strip() if resolve.returncode == 0 else ""
            logger.info(
                "[cluster_fetch] ok {ref}={sha} elapsed={elapsed:.1f}s",
                ref=track_ref,
                sha=fetched[:7] if fetched else "?",
                elapsed=elapsed,
            )
            return {"ok": True, "fetched": fetched, "elapsed_s": round(elapsed, 2)}
        logger.warning(
            "[cluster_fetch] git fetch failed rc={rc} stderr={err!r} elapsed={elapsed:.1f}s",
            rc=result.returncode,
            err=result.stderr[:200],
            elapsed=elapsed,
        )
        return {
            "ok": False,
            "error": f"git fetch origin failed (rc={result.returncode}): {result.stderr[:300]}",
            "elapsed_s": round(elapsed, 2),
        }
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        logger.warning("[cluster_fetch] git fetch timed out after {elapsed:.1f}s", elapsed=elapsed)
        return {"ok": False, "error": f"git fetch origin timed out after {elapsed:.0f}s"}
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.warning("[cluster_fetch] unexpected error: {exc!r}", exc=exc)
        return {"ok": False, "error": str(exc)}
