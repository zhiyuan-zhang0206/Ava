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

import os
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from ops import cluster_session
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
from ops.cluster_status import agent_shell_sessions, capture_shell, kill_shell
from ops.rpc_schemas import (
    AgentSkillViewResult,
    OpsCommandItem,
    ShellCaptureResult,
    ShellKillResult,
    ShellProbeResult,
)
from shared import pause_owner, ui_update_state, updater_handoff
from shared.checkpoint_serde import STATIC_CHECKPOINT_MSGPACK_TYPES
from shared.cluster_lock import (
    claim_recovery_lock,
    read_update_lease,
    release_update_lock,
)
from shared.config.turn_view import resolve_agent_config_pins
from shared.gitenv import git_env
from shared.host_deploy_state import updater_lease_live
from shared.log import logger
from shared.machine import machine_name
from shared.machines import mark_stopping
from shared.proc import process_alive, run_bounded, timeout_stderr_tail

# `cluster_fetch_op`'s two git calls. The fetch ceiling is generous (the whole
# point of the pre-flight is to find out whether this host can reach the remote);
# the local rev-parse touches only the object store. Both bounds are enforced by
# `run_bounded`, so a stalled fetch cannot leave a live git/ssh tail behind on a
# host the rollout then declares unreachable.
_FETCH_TIMEOUT_S = 30.0
_RESOLVE_TIMEOUT_S = 5.0


def _require_executing_deploy(
    deploy_holder: str,
    deploy_acquired_at: datetime,
) -> None:
    """Reject a delayed transition unless its exact lease generation still runs."""
    lease = read_update_lease()
    if (
        lease is None
        or lease.note is not None
        or lease.acquired_at is None
        or lease.holder != deploy_holder
        or lease.acquired_at != deploy_acquired_at
    ):
        raise ClusterUpdateInProgress(
            "cluster transition refused: its exact executing deploy lease is no longer current"
        )


def cluster_stop_op(
    deploy_holder: str,
    deploy_acquired_at: datetime,
) -> dict[str, object]:
    """Drain hosted continuations under the exact executing deploy generation."""
    with ui_update_state.lifecycle_lock():
        # Phase A is valid only while an executing rollout/restart owns the
        # cluster lease. Recheck under the same local mutex recovery uses: if a
        # cross-host recover CAS-claimed the lease first, this delayed stop must
        # not re-pause the just-recovered host.
        _require_executing_deploy(deploy_holder, deploy_acquired_at)
        pause_owner.mark_paused(deploy_holder, deploy_acquired_at)
        try:
            # Close the cross-host lease-change window between the first DB
            # proof and publishing the local capability.
            _require_executing_deploy(deploy_holder, deploy_acquired_at)
        except BaseException:
            pause_owner.clear(deploy_holder, deploy_acquired_at)
            raise
        try:
            pause_local_cluster()
        except BaseException:
            try:
                unpause_local_cluster()
            except Exception:
                # Partial pause + failed compensation is conservative: retain
                # the paused journal so an exact retry/recover can repair it.
                logger.warning(
                    "[cluster] pause failed and compensating unpause also failed; "
                    "retaining exact pause owner"
                )
            else:
                pause_owner.mark_resumed(deploy_holder, deploy_acquired_at)
            raise
    return {}


def _refuse_live_local_updater() -> None:
    """Keep a deploy resume from unpausing a newer host-local updater."""
    handoff = updater_handoff.read()
    if handoff.status == "invalid":
        raise ClusterUpdateInProgress(
            "cluster resume refused: local updater ownership is unreadable"
        )
    if handoff.status == "pending" and not handoff.expired:
        raise ClusterUpdateInProgress("cluster resume refused: a newer local updater is pending")
    if handoff.status == "running" and updater_handoff.owner_is_live(handoff):
        raise ClusterUpdateInProgress("cluster resume refused: a newer local updater is running")


def cluster_resume_op(
    deploy_holder: str,
    deploy_acquired_at: datetime,
) -> dict[str, object]:
    """Generation-scoped unpause — never resume a later rollout's pause."""
    with ui_update_state.lifecycle_lock():
        owner = pause_owner.read()
        if not owner.matches(deploy_holder, deploy_acquired_at):
            raise ClusterUpdateInProgress(
                "cluster resume refused: this host is paused by a different deploy generation"
            )
        if owner.status == "resumed":
            return {}
        if owner.status != "paused":
            raise ClusterUpdateInProgress("cluster resume refused: pause owner is unreadable")
        _refuse_live_local_updater()
        unpause_local_cluster()
        if not pause_owner.mark_resumed(deploy_holder, deploy_acquired_at):
            raise RuntimeError("lost the local pause-owner capability after unpausing")
    return {}


def cluster_resume_legacy_op() -> dict[str, object]:
    """One-rollout bridge for an old orchestrator resuming a newly updated host.

    An old Phase-A receiver writes no exact pause-owner journal. After that host
    updates onto this code, the still-old in-memory orchestrator sends an empty
    resume payload. Only an absent or already-completed legacy journal may take
    this path; any exact or malformed owner fails closed.
    """
    with ui_update_state.lifecycle_lock():
        owner = pause_owner.read()
        if owner.status == "legacy-resumed":
            return {}
        if owner.status != "inactive":
            raise ClusterUpdateInProgress(
                "legacy cluster resume refused: an exact or unreadable pause owner exists"
            )
        _refuse_live_local_updater()
        unpause_local_cluster()
        pause_owner.mark_legacy_resumed()
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
    with ui_update_state.lifecycle_lock():
        handoff = updater_handoff.read()
        if handoff.status == "invalid":
            raise ClusterUpdateInProgress(
                "an updater spawn handoff is still active or unreadable — recovery "
                "refused until its child publishes the DB lease or its safety bound expires"
            )
        if handoff.status == "pending" and not handoff.expired:
            raise ClusterUpdateInProgress(
                "an updater child is still inside its protected startup window — "
                "recovery refused until that pending handoff expires"
            )
        if handoff.status == "running" and updater_handoff.owner_is_live(handoff):
            raise ClusterUpdateInProgress(
                "an updater process still owns this host pause — recovery refused"
            )
        live_session = cluster_session.live_orchestration_session()
        if live_session is not None:
            raise ClusterUpdateInProgress(
                f"orchestration session {live_session!r} is still alive — recovery refused; "
                "wait for it to acquire/finish its deploy lease or terminate that session first"
            )
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
        snapshot = ui_update_state.read()
        pause_snapshot = pause_owner.read()
        recovery_holder = f"recovery:{machine_name()}:pid{os.getpid()}"
        claim = claim_recovery_lock(recovery_holder, lease)
        if not claim.acquired:
            raise ClusterUpdateInProgress(
                "the cluster deploy lease changed while recovery was proving it stale; "
                "a new owner may have started, so recovery refused without unpausing or clearing"
            )
        try:
            unpause_local_cluster()
            if snapshot.status == "updating" and snapshot.generation is not None:
                ui_update_state.clear(snapshot.generation)
            elif snapshot.status == "invalid":
                ui_update_state.force_clear()
            if handoff.generation is not None:
                updater_handoff.clear(handoff.generation)
            if pause_snapshot.holder is not None and pause_snapshot.acquired_at is not None:
                pause_owner.clear(
                    pause_snapshot.holder,
                    pause_snapshot.acquired_at,
                )
            elif pause_snapshot.status == "invalid":
                pause_owner.force_clear()
        finally:
            release_update_lock(recovery_holder)
        cleared = claim.previous_holder
    logger.info(
        "[cluster] manual recover: force-released lock (was {holder}) + unpaused + "
        "cleared the UI update marker",
        holder=cleared,
    )
    return {"unlocked_holder": cleared}


def cluster_cancel_op() -> dict[str, object]:
    """Formally cancel this host's live rollout / restart orchestration.

    The recovery for a cancelled rollout is the orchestration's own `finally`
    (`cli.commands.update._run_gateway_orchestration`): it resumes every paused
    host, releases the deploy lease — or converts it to a settle hold over the
    hosts still mid-transition — and clears the durable maintenance marker. That
    unwind is exactly the recovery the stalled-rollout controller triggers
    unattended after its no-progress bound (`ops.controllers.stalled_rollout`,
    stage 1); this op is the operator-triggered twin — a formal cancel instead of
    a hand kill, which is what a rollout a hung Windows box is dragging used to
    leave an operator with (P1, 2026-08-30).

    The trigger is `SIGINT` to the orchestration's own pid, read out of the deploy
    lease's holder string (`<machine>:pid<N>`) — never a session kill, because a
    killed process cannot run its `finally` and the cluster would sit paused until
    the lease lapses. Python turns `SIGINT` into `KeyboardInterrupt`, which no
    `except Exception:` in the orchestration swallows, so the `finally` runs.

    Refuses (`ClusterUpdateInProgress`, each refusal naming its own next step)
    unless: a live orchestration session exists, the deploy lease is an
    *executing* one (note NULL — a settle hold has nothing running), the holder
    names THIS machine, and the pid is alive. The pid-liveness probe is the same
    `holder_pid_if_local` the stalled-rollout controller uses, so a cancel and an
    unattended reclaim can never disagree about who is signalable.

    Returns {"cancelled": <holder>}. The orchestration may take tens of seconds
    to finish unwinding — watch its rollout log, and `ava cluster status` after.
    """
    import signal

    import shared.cluster
    from ops.cluster_session import (
        _CLUSTER_RESTART_SERVICE,
        _ROLLOUT_SERVICE,
        _UPDATER_SERVICE,
        live_orchestration_session,
    )
    from shared.cluster_lock import holder_pid_if_local
    from shared.last_update import UpdateOutcome, read_last_update

    live = live_orchestration_session()
    updater_session = shared.cluster.session_name(_UPDATER_SERVICE)
    cluster_sessions = {
        shared.cluster.session_name(_ROLLOUT_SERVICE),
        shared.cluster.session_name(_CLUSTER_RESTART_SERVICE),
    }
    if live == updater_session:
        raise ClusterUpdateInProgress(
            "this host is mid self-update, not running a cluster orchestration — "
            "nothing a cluster cancel owns. Its watchdog reaps a hung updater at the "
            "no-progress bound; `ava cluster recover` clears a stranded state whose "
            "owner is gone."
        )
    if live is None or live not in cluster_sessions:
        raise ClusterUpdateInProgress(
            "no rollout/restart orchestration is running on this host — the "
            "orchestration runs on the gateway host, so run `ava cluster cancel` "
            "there; `ava cluster recover` here clears a stranded state whose owner "
            "is gone."
        )
    lease = read_update_lease()
    if lease is not None and lease.note is not None:
        raise ClusterUpdateInProgress(
            f"the deploy lease is a settle hold, not an executing orchestration — "
            f"{lease.describe()}. Nothing is running to cancel; `ava cluster recover` "
            "breaks the hold once you have confirmed the hosts have converged."
        )
    if lease is not None and lease.kind not in ("rollout", "restart"):
        raise ClusterUpdateInProgress(
            f"the deploy lease {lease.describe()} carries no rollout/restart kind — "
            "a rollback or legacy orchestration; cancel is scoped to rollout/restart. "
            "Wait for it, or `ava cluster recover` once its holder is provably gone."
        )
    holder: str | None = None
    if lease is not None:
        holder = lease.holder
    else:
        # Pre-lease window (the child has not acquired yet) or a lease-less
        # orchestration. The last-update row still names the process.
        try:
            record = read_last_update()
        except Exception:
            record = None
        if record is not None and record.outcome is UpdateOutcome.RUNNING:
            holder = record.holder
    if holder is None:
        raise ClusterUpdateInProgress(
            "the orchestration has published neither a deploy lease nor a running "
            "update record — it is still starting or already gone; retry in a few "
            "seconds, or `ava cluster recover` once no owner remains."
        )
    pid = holder_pid_if_local(holder)
    if pid is None:
        from shared.machine import machine_name as _machine_name

        machine = holder.split(":pid", 1)[0]
        where = (
            f"run `ava cluster cancel` on {machine}"
            if machine != _machine_name()
            else "the holder pid is gone — `ava cluster recover` clears the residue"
        )
        raise ClusterUpdateInProgress(
            f"the orchestration holder {holder!r} is not a live process on this host — {where}"
        )
    try:
        os.kill(pid, signal.SIGINT)
    except OSError as exc:
        raise ClusterUpdateInProgress(
            f"could not interrupt the orchestration pid {pid}: {exc!r}"
        ) from exc
    logger.warning(
        "[cluster] cancel: SIGINT sent to rollout holder %s (pid %d); its own finally "
        "is unwinding — compensating resume, settle/release of the deploy lease, "
        "maintenance marker cleared",
        holder,
        pid,
    )
    return {"cancelled": holder}


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
    gateway-side quiesce already drained the fleet). The legacy `force_reap`
    argument carries an explicit interruption request, never timeout escalation.

    `spawn_update` validates before its own pause, then publishes a host-local
    handoff across pause -> DB lease. It is the only layer with enough evidence
    to compensate a definitive no-child failure; this wrapper never guesses
    from exception type/timing.
    """
    # `spawn_update` owns the exact pause/spawn boundary and therefore owns
    # compensation: only a definitive backend decline CAS-clears its handoff
    # and unpauses. This wrapper cannot infer "no child" from an exception — a
    # post-fork/Popen session-record failure is ambiguous and must stay paused.
    return spawn_update(
        restart_only=restart_only,
        target_sha=target_sha,
        mode=mode,
        force_reap=force_reap,
    )


def cluster_rollout_op(
    origin: str, *, mode: str = "smooth", force: bool = False, dry_run: bool = False
) -> dict[str, str | bool]:
    """Run `spawn_rollout()` and return the new orchestration session metadata + rollout scope.

    `mode` is the agent-drain policy (smooth/force) the detached orchestration
    applies to its quiesce step; `force` overrides the deploy-window check
    (issue #216 threads the CLI's `--force` through the POST body)."""
    return spawn_rollout(origin, force=force, mode=mode, dry_run=dry_run)


def cluster_restart_op(origin: str, *, mode: str = "smooth") -> dict[str, str]:
    """Run `spawn_restart()` and return the new orchestration session metadata."""
    return spawn_restart(origin, mode=mode)


def cluster_update_check_op() -> UpdateCheck:
    """Read-only preflight — is there anything to roll out, and what would restart."""
    return update_check()


def cluster_status_op(pool: Any | None = None) -> ClusterStatus:
    """Local snapshot — assembled by `status_snapshot()`."""
    return status_snapshot(pool=pool)


def shell_probe_op(agent_id: int) -> ShellProbeResult:
    """This host's live persistent-shell sessions for one agent.

    The runner-side half of the inspector panel's `shells` list: the gateway
    dispatches this op when the agent runs on this machine rather than on the
    gateway's own box (`agent_shell_sessions` is host-scoped, so a local probe
    on the gateway would always read empty for a remote agent).
    """
    return ShellProbeResult(shells=agent_shell_sessions(agent_id))


def shell_kill_op(agent_id: int, session_id: int) -> ShellKillResult:
    """Kill one persistent shell on this runner for TTL reclamation.

    ``interrupted`` reports whether the kill cut short a running job — the
    gateway notifies the owner only then (an empty shell's reaping is silent)."""
    match kill_shell(agent_id, session_id):
        case ("killed", interrupted, name):
            return ShellKillResult(mode="killed", interrupted=interrupted, name=name)
        case ("absent", _interrupted, _name):
            return ShellKillResult(mode="absent")
        case mode:
            raise AssertionError(f"unknown shell kill mode {mode!r}")


def _agent_skill_view_inputs(pool: Any, agent_id: int) -> tuple[Path | None, list[str] | None]:
    """The persisted cwd and effective skill-index narrowing for one agent.

    The daemon's shared pool keeps this read on the agent's machine.  ``cwd`` is
    the ava-code plugin's private channel key, following ``PluginStateHandle``'s
    ``<plugin>__<field>`` convention in ``agent/state.py``.  An old agent with
    no checkpoint has no project-local roots; an old row with no frozen/overlay
    value falls through to the normal unfiltered (``["*"]``) command view.
    """
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (agent_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None, None
        overlay = cast(dict[str, Any], row[0]) if isinstance(row[0], dict) else {}
        birth = cast(dict[str, Any], row[1]) if isinstance(row[1], dict) else {}
        pins = resolve_agent_config_pins(overlay, birth)
        wanted = pins.get("skills_to_inject_into_system_prompt")

        saver = PostgresSaver(
            conn=conn,
            serde=JsonPlusSerializer(allowed_msgpack_modules=STATIC_CHECKPOINT_MSGPACK_TYPES),
        )
        checkpoint = saver.get({"configurable": {"thread_id": str(agent_id)}})

    cwd = checkpoint["channel_values"].get("ava_code__cwd") if checkpoint else None
    narrowed = cast(list[str], wanted) if isinstance(wanted, list) else None
    return (Path(cwd) if isinstance(cwd, str) else None), narrowed


def _project_skill_roots(cwd: Path | None) -> list[Path]:
    """Best-effort project roots; an absent ava-code plugin is not an op failure."""
    if cwd is None:
        return []
    try:
        from ava_builtins.plugins.ava_code._walk import project_skill_roots
    except ImportError:
        logger.debug("agent_skill_view: ava-code plugin unavailable; skipping project skills")
        return []
    return project_skill_roots(cwd)


def _narrow_commands(commands: list[Any], wanted: list[str] | None) -> list[Any]:
    """Keep explicit commands plus skill commands selected as prompt capabilities.

    This intentionally mirrors ``agent.graph._capabilities.resolve_prompt_skills``:
    ``*`` selects all loaded skills; otherwise a configured value matches the
    dotted identifier first and then the bare frontmatter name under the common
    dash/underscore fold.  Only skill-as-command entries are narrowed; explicit
    command files remain available to every agent as they are not capabilities.
    """
    if wanted is None or "*" in wanted:
        return commands

    from ava import skills
    from shared.skill_names import match_key

    loaded = skills._names()
    by_ident = {match_key(skills.identifier(skill)): skill for skill in loaded}
    by_name = {match_key(skill["name"]): skill for skill in loaded}
    selected_targets = {
        skills.target(skill)
        for name in wanted
        if (skill := by_ident.get(match_key(name)) or by_name.get(match_key(name))) is not None
    }
    return [
        command
        for command in commands
        if command["skill_target"] is None or command["skill_target"] in selected_targets
    ]


def agent_skill_view_op(agent_id: int, pool: Any) -> AgentSkillViewResult:
    """Build the command-autocomplete view that ``agent_id`` sees on this host.

    Converged skills are discovered on the target runner, with the agent's
    checkpointed cwd contributing project-local roots only for this call.  The
    provider registry is process-global, so cleanup is unconditional to prevent
    one request leaking its project skills into a later agent's result.  The
    result also carries this runner's enabled MCP names as phase-2 groundwork.
    """
    from ava import skills
    from ava._commands import discover_commands
    from ava._mcp_config import load_mcp_config
    from shared.mcp_enabled import read_enabled

    cwd, wanted = _agent_skill_view_inputs(pool, agent_id)
    skills.register_skill_source(lambda: _project_skill_roots(cwd))
    try:
        commands = _narrow_commands(discover_commands(), wanted)
    finally:
        skills.clear_skill_sources()
    merged_mcp = load_mcp_config(include_disabled=True)
    mcp_overlay = read_enabled()
    return AgentSkillViewResult(
        commands=[
            OpsCommandItem(
                name=command["name"],
                description=command["description"],
                instruction_hint=command["instruction_hint"],
            )
            for command in commands
        ],
        mcp_names=sorted(name for name in merged_mcp if mcp_overlay.get(name, True)),
    )


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
    full_name, captured, created_at, uptime_seconds = capture_shell(agent_id, session_id, lines)
    return ShellCaptureResult(
        session_name=full_name,
        lines=captured,
        created_at=created_at,
        uptime_seconds=uptime_seconds,
    )


def cluster_fetch_op() -> dict[str, object]:
    """Run `git fetch origin` on this agent-runner — a lightweight pre-flight
    that confirms this host can reach the remote and has the objects needed for
    the upcoming rollout's pinned target.

    Non-disruptive: does not pause agent admission, change posture or restart
    any service. The caller (the gateway's rollout orchestration)
    fans this out to every agent-runner *before* Phase A so a fetch failure
    aborts the rollout with nothing paused.

    **Per-attempt observability (2026-08-27 performance forensics):** the
    gateway retries a transport timeout at its own level, so one Phase 0 can
    drive several sequential invocations of this op on the host (observed on
    win/wsl: two 30s timeouts, then success on the third). Each invocation
    logs its own start/end with pid + elapsed, so the attempts are countable
    in the ops log; `--progress` forces git's stage markers onto stderr even
    under a pipe, and a timeout carries the partial stderr tail — where git
    was when `run_bounded` killed it (ssh connect / negotiation / pack
    transfer) — instead of a bare "timed out".

    Returns:
        ``{"ok": True, "fetched": "<sha or empty>", "elapsed_s": <float>}``
        on success; ``{"ok": False, "error": "<message>"}`` on failure.
    """
    import subprocess
    import time

    from shared.config import settings
    from shared.paths import repo_root

    t0 = time.monotonic()
    logger.info(
        "[cluster_fetch] start pid={pid} timeout={timeout:.0f}s",
        pid=os.getpid(),
        timeout=_FETCH_TIMEOUT_S,
    )
    try:
        result = run_bounded(
            ["git", "fetch", "--progress", "origin"],
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
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - t0
        # `run_bounded` drains the tree's pipes after the kill, so `exc.stderr`
        # holds everything git wrote before the bound tripped — the timeout
        # point. Empty stderr is itself evidence: a fetch killed before ssh or
        # git printed anything died in the local/connect phase, not mid-transfer.
        tail = timeout_stderr_tail(exc)
        logger.warning(
            "[cluster_fetch] git fetch timed out after {elapsed:.1f}s "
            "(bound {bound:.0f}s); last stderr: {tail!r}",
            elapsed=elapsed,
            bound=_FETCH_TIMEOUT_S,
            tail=tail,
        )
        return {
            "ok": False,
            "error": (
                f"git fetch origin timed out after {elapsed:.0f}s"
                + (f"; last stderr: {tail[:200]}" if tail else "")
            ),
            "elapsed_s": round(elapsed, 2),
        }
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.warning("[cluster_fetch] unexpected error: {exc!r}", exc=exc)
        return {"ok": False, "error": str(exc)}
