"""Cluster-ops RPC implementations.

Stranded-lease recovery, stopping announcements, live status snapshots and the
release image-exec handoff. One of the four
op clusters split out of the former single `ops/operations.py` (the others
are ops_lifecycle / ops_config / ops_inventory); each cluster is self-contained.

Most of these are thin wrappers — the real work lives in `gateway/cluster.py`;
this layer is the agent-runner-callable RPC surface the ops server dispatches
(`services/agent_ops/daemon.py:_dispatch`) and the gateway cluster router calls.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

from ops.cluster_pause import unpause_local_cluster
from ops.cluster_status import (
    ClusterStatus,
    agent_shell_sessions,
    capture_shell,
    kill_shell,
    status_snapshot,
)
from ops.rpc_schemas import (
    AgentSkillViewResult,
    OpsCommandItem,
    ShellCaptureResult,
    ShellKillResult,
    ShellProbeResult,
)
from shared import home_lifecycle_locks, pause_owner, updater_handoff
from shared.agents.history.checkpoint_serde import STATIC_CHECKPOINT_MSGPACK_TYPES
from shared.api_contracts.release_handoff import (
    HandoffRefusedError,
    ReleaseImageEntry,
    ReleaseImageExecPayload,
    ReleaseImageExecResult,
    ReleaseImageRef,
    entry_argv,
    entry_environment,
    read_envelope,
)
from shared.cluster_lock import (
    claim_recovery_lock,
    read_update_lease,
    release_update_lock,
)
from shared.config.turn_view import resolve_agent_config_pins
from shared.host_deploy_state import updater_lease_live
from shared.log import logger
from shared.machine import machine_name
from shared.machines import mark_stopping
from shared.paths import ava_home
from shared.runtime_abi import current_abi


class ClusterUpdateInProgress(RuntimeError):  # noqa: N818 — state description
    """An exact deploy or maintenance owner still excludes this transition."""


def _lock_holder_is_live(holder: str, *, held_for_s: float | None = None) -> bool:
    """Whether `holder` (the update-lock owner string `<machine>:pid<N>`, minted by
    the deploy lease) names a process that is
    still running on THIS host.

    The negation of `shared.cluster_lock.holder_process_gone` supplies local-owner
    proof, including the pid-recycling slack. A holder on a different machine, an
    unparseable holder, and an unreadable process identity are all treated as live
    (refuse rather than risk clobbering a real run). `held_for_s` (the lease's
    server-computed age) arms the pid-recycling check: the holder string carries
    no start time, but a live pid whose process STARTED after the acquire (+ the
    shared slack) is the pid's next occupant, not the holder. This matters at
    recover's timescale: a 30-minute TTL is exactly the window in which a busy
    host recycles the dead orchestration's pid.
    """
    from shared.cluster_lock import holder_process_gone

    return not holder_process_gone(holder, held_for_s=held_for_s)


def cluster_recover_op() -> dict[str, object]:
    """Operator stranded-cluster recovery — force-clear a pause + update lock that
    a hard-killed rollout left behind.

    Refuses (raises ClusterUpdateInProgress) when a deploy is actually alive, via
    two authoritative checks — a deploy lease whose holder PROCESS is still
    running (pid-probed when the holder is this host; a holder elsewhere cannot
    be probed and is conservatively treated as live), OR this host's live updater
    lease (a separate updater owner the deploy lease cannot see).
    Only when neither holds is the paused/locked state stale and safe to
    force-clear. Probing the holder pid allows this retained legacy helper to
    refuse while the process is still alive.

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
    with (
        home_lifecycle_locks.resource_lock(purpose="ops.cluster_recover"),
        home_lifecycle_locks.lifecycle_lock(),
    ):
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
        if not updater_handoff.allows_generic_recovery(handoff):
            raise ClusterUpdateInProgress(
                "retained updater compensation requires an explicit checked recovery — "
                "generic unpause refused"
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
        "[cluster] manual recover: force-released lock (was {holder}) + unpaused",
        holder=cleared,
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
    from shared.packages.skills.skill_names import match_key

    loaded = skills.names()
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
    from ava.composer_commands import discover_commands
    from ava.mcp_config import load_mcp_config
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

    The `lines` default is a direct-call fallback: the gateway resolves the
    configured default (display.shell_capture_default_lines) before
    dispatching, and every programmatic caller passes an explicit window.

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


# ── release_image_exec: the frozen v1 image-exec handoff on this unit ──

RELEASE_ENTRY_TIMEOUT_S = 120.0
_OUTPUT_TAIL = 2000


def _tail(data: bytes) -> str:
    return data.decode("utf-8", "replace")[-_OUTPUT_TAIL:]


def run_release_entry(
    *,
    home: Path,
    machine: str,
    entry: ReleaseImageEntry,
    image: ReleaseImageRef,
    request: bytes,
    timeout_s: float = RELEASE_ENTRY_TIMEOUT_S,
) -> dict[str, Any]:
    """Run `entry` of `image` on `request` for this unit; the entry's JSON object.

    The request's envelope must name this home and machine and `image` as its
    executor; the image is verified in this home's store against this host now.
    The entry reads the exact bytes on stdin and is killed past `timeout_s`.
    """
    envelope = read_envelope(request)
    if Path(envelope.home) != home:
        raise HandoffRefusedError(f"the request belongs to {envelope.home}, not to {home}")
    if envelope.machine != machine:
        raise HandoffRefusedError(
            f"the request names machine {envelope.machine!r}, not {machine!r}"
        )
    if not envelope.names(image):
        raise HandoffRefusedError("the request names another executor than the image to run")
    verified = image.verify(home, host_abi=current_abi())
    try:
        completed = subprocess.run(  # noqa: S603 — verified image, fixed v1 entry, no shell
            entry_argv(verified, entry, "-"),
            input=request,
            capture_output=True,
            cwd=verified.cwd,
            env=entry_environment(os.environ, str(home)),
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HandoffRefusedError(
            f"the {entry} entry did not finish within {timeout_s:g}s"
        ) from exc
    if completed.returncode != 0:
        raise HandoffRefusedError(
            f"the {entry} entry exited {completed.returncode}: {_tail(completed.stderr)}"
        )
    try:
        document = json.loads(completed.stdout)
    except ValueError as exc:
        raise HandoffRefusedError(
            f"the {entry} entry printed no JSON document: {_tail(completed.stdout)}"
        ) from exc
    if not isinstance(document, dict):
        raise HandoffRefusedError(f"the {entry} entry printed a non-object JSON document")
    return cast("dict[str, Any]", document)


def release_image_exec_op(payload: ReleaseImageExecPayload) -> ReleaseImageExecResult:
    """`release_image_exec`: verify an image in this unit's store and run one entry.

    Changes nothing itself; the entry owns its own journaling. A refusal, a
    nonzero exit, non-JSON output or a timeout raises `HandoffRefusedError`
    (a ValueError: the daemon reports a failed op).
    """
    try:
        request = base64.b64decode(payload.request, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HandoffRefusedError("the request is not valid base64") from exc
    result = run_release_entry(
        home=ava_home().resolve(),
        machine=machine_name(),
        entry=payload.entry,
        image=payload.image,
        request=request,
    )
    return ReleaseImageExecResult(entry=payload.entry, result=result)
