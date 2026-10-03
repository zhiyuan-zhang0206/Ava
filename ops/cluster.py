"""Cluster-ops RPC implementations.

Stopping announcements, live status snapshots, persistent-shell probes and the
per-agent command view. One of the op clusters beside `ops.lifecycle`,
`ops.host_config`, `ops.inventory` and `ops.uploads`; each cluster is
self-contained.

Most of these are thin wrappers; this layer is the agent-runner-callable RPC
surface the ops server dispatches (`services/agent_ops/daemon.py:_dispatch`) and
the gateway cluster router calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from base.agents.history.checkpoint_serde import STATIC_CHECKPOINT_MSGPACK_TYPES
from base.cluster.machines import mark_stopping
from base.config.agent_pins import resolve_agent_config_pins
from base.db import Database
from base.log import logger
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


def cluster_stopping_op(db: Database, machine: str, home: str) -> dict[str, str]:
    """Record an intentional shutdown announced by the (machine, home) unit.

    `ava stop` calls this (best-effort) just before tearing the local stack
    down, so the cluster view shows the host as "stopped" rather than "offline"
    (a live probe cannot tell an intentional stop from a crash). Stamps the
    unit's `stopped_at` and recomputes the composed `machines` row; `ava start`
    clears it. `home` is the stopping unit's $AVA_HOME, sent on the wire so a
    co-located peer's caps are not retracted along with this unit's.
    """
    mark_stopping(db, machine, home)
    return {"machine": machine}


def cluster_status_op(db: Database, pool: Any | None = None) -> ClusterStatus:
    """Local snapshot — assembled by `status_snapshot()`."""
    return status_snapshot(db, pool=pool)


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
        from ava_builtins.plugins.ava_code import project_skill_roots
    except ImportError:
        logger.debug("agent_skill_view: ava-code plugin unavailable; skipping project skills")
        return []
    return project_skill_roots(cwd)


def _narrow_commands(commands: list[Any], wanted: list[str] | None) -> list[Any]:
    """Keep explicit commands plus skill commands selected as prompt capabilities.

    This intentionally mirrors ``agent.graph.capabilities.resolve_prompt_skills``:
    ``*`` selects all loaded skills; otherwise a configured value matches the
    dotted identifier first and then the bare frontmatter name under the common
    dash/underscore fold.  Only skill-as-command entries are narrowed; explicit
    command files remain available to every agent as they are not capabilities.
    """
    if wanted is None or "*" in wanted:
        return commands

    from ava import skills
    from base.packages.skills.names import match_key

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
    provider is scoped to this call (and only it is taken back), so one request cannot leak its
    project skills into a later agent's result.  The
    result also carries this runner's enabled MCP names as phase-2 groundwork.
    """
    from ava.composer_commands import discover_commands
    from ava.mcp_config import load_mcp_config
    from ava.sdk_surface import skill_sources
    from base.packages.plugins.mcp_enabled import read_enabled

    cwd, wanted = _agent_skill_view_inputs(pool, agent_id)
    with skill_sources.scoped(lambda: _project_skill_roots(cwd)):
        commands = _narrow_commands(discover_commands(), wanted)
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
