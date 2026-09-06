from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import ava
import ava._boot
from ava import _gateway_client as _client
from ava._sdk_validation import coerce_str, coerce_typed

# Redundant-alias re-exports: importable from this module but deliberately not
# in __all_for_ava__ — error types never render into the SDK docs every agent carries;
# a traceback names them clearly on the rare occasion one fires. The aliases keep
# pyright from flagging them unused.
from shared.agents import AgentNotFound as AgentNotFound
from shared.agents import AgentStatus, RestartResult, ResurrectResult, TerminateResult
from shared.agents import CrossMachineGatewayUnavailable as CrossMachineGatewayUnavailable
from shared.agents import ForkCheckpointNotFound as ForkCheckpointNotFound
from shared.agents import ForkError as ForkError
from shared.agents import ForkSourceEmpty as ForkSourceEmpty
from shared.agents import GatewayUnavailable as GatewayUnavailable
from shared.agents import InvalidModelConfig as InvalidModelConfig
from shared.agents import MachineNotRegistered as MachineNotRegistered
from shared.agents import ResurrectError as ResurrectError
from shared.agents import SpawnTargetNotAgentRunner as SpawnTargetNotAgentRunner
from shared.config import cluster_tz
from shared.message_kwargs import NoteTag

from . import presets as presets

__all_for_ava__ = [
    "AgentRow",
    "AgentStatus",
    "CommandInfo",
    "Machine",
    "Neighbor",
    "RestartResult",
    "ResurrectResult",
    "TerminateResult",
    "commands",
    "get_ancestors",
    "get_last_message",
    "get_neighbors",
    "get_status",
    "list_agents",
    "list_machines",
    "presets",
    "restart",
    "resurrect",
    "send_message",
    "spawn",
    "terminate",
]


@dataclass
class CommandInfo:
    name: str
    description: str
    instruction_hint: str

    def __str__(self) -> str:
        hint = f" {self.instruction_hint}" if self.instruction_hint else ""
        desc = f"  — {self.description}" if self.description else ""
        return f"/{self.name}{hint}{desc}"


@dataclass
class Machine:
    name: str
    description: str | None
    live: bool

    def __str__(self) -> str:
        state = "live" if self.live else "offline"
        desc = f"  {self.description}" if self.description else ""
        return f"{self.name}  [{state}]{desc}"


@dataclass
class AgentRow:
    agent_id: int
    label: str | None
    status: AgentStatus
    spawner: str
    machine: str
    spawned_at: datetime
    started_at: datetime | None
    # The agent's real-activity clock (every completed LLM turn) — the value
    # triage surfaces should use for "is it alive". The API also carries
    # `last_inbound_at` ("when did anyone last talk to it") since the two
    # diverge during long single turns (issue #183).
    last_active_at: datetime
    last_inbound_at: datetime
    pid: int | None
    heartbeat_paused_until: datetime | None

    def __str__(self) -> str:
        label_part = f"{self.label} " if self.label else ""
        parts = [f"#{self.agent_id} {label_part} {self.status.value}"]
        parts.append(f"machine={self.machine}")
        parts.append(f"spawned={_relative_time(self.spawned_at)}")
        if self.last_active_at != self.spawned_at:
            parts.append(f"last_active={_relative_time(self.last_active_at)}")
        return "  ".join(parts)


@dataclass
class Neighbor:
    """`depth`: hops from the queried agent (1 = direct) — out along ties for
    neighbors, up the spawn chain for ancestors; `score`: tie strength.
    Terminated agents are included."""

    agent_id: int
    label: str | None
    status: AgentStatus
    depth: int
    score: float

    def __str__(self) -> str:
        label_part = f"{self.label} " if self.label else ""
        return (
            f"#{self.agent_id} {label_part} {self.status.value}  "
            f"depth={self.depth}  score={self.score:.4g}"
        )


def _relative_time(dt: datetime) -> str:
    """Convert a datetime into a human-readable relative time string."""
    delta = datetime.now().astimezone(cluster_tz()) - dt.astimezone(cluster_tz())
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def get_neighbors(agent_id: int, depth: int = 1, limit: int = 20) -> list[Neighbor]:
    """Rank the agents most strongly tied to `agent_id`.

    Ties form on spawn, fork, resurrect, or send_message and fade with time;
    `depth` is how many hops out to look.
    """
    agent_id = coerce_typed(agent_id, "agent_id", int)
    depth = coerce_typed(depth, "depth", int)
    limit = coerce_typed(limit, "limit", int)
    return [
        Neighbor(
            agent_id=n["agent_id"],
            label=n.get("label"),
            status=AgentStatus(n["status"]),
            depth=n["depth"],
            score=n["score"],
        )
        for n in _client.get_neighbors(agent_id, depth=depth, limit=limit)
    ]


def get_ancestors(agent_id: int) -> list[Neighbor]:
    """The spawn/fork chain above `agent_id`, nearest ancestor first.

    Message ties never form ancestors, and an agent spawned by the user (or
    with no recorded spawn) returns [].
    """
    agent_id = coerce_typed(agent_id, "agent_id", int)
    return [
        Neighbor(
            agent_id=n["agent_id"],
            label=n.get("label"),
            status=AgentStatus(n["status"]),
            depth=n["depth"],
            score=n["score"],
        )
        for n in _client.get_ancestors(agent_id)
    ]


def list_agents(
    filter_by_status: tuple[AgentStatus, ...] | None = (
        AgentStatus.RUNNING,
        AgentStatus.IDLING,
    ),
) -> list[AgentRow]:
    filter_by_status = coerce_typed(filter_by_status, "filter_by_status", tuple, allow_none=True)
    raw_rows = _client.list_agents(
        filter_by_status=filter_by_status,
    )
    return [_row_from_dict(r) for r in raw_rows]


def list_machines() -> list[Machine]:
    return [
        Machine(name=r["name"], description=r.get("description"), live=r["live"])
        for r in _client.list_machines()
    ]


def _row_from_dict(data: dict) -> AgentRow:
    """Gateway JSON dict → AgentRow dataclass."""
    return AgentRow(
        agent_id=data["agent_id"],
        label=data.get("label"),
        status=AgentStatus(data["status"]),
        spawner=data["spawner"],
        machine=data["machine"],
        spawned_at=datetime.fromisoformat(data["spawned_at"]),
        started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
        last_active_at=datetime.fromisoformat(data["last_active_at"]),
        last_inbound_at=datetime.fromisoformat(data["last_inbound_at"]),
        pid=data.get("pid"),
        heartbeat_paused_until=(
            datetime.fromisoformat(data["heartbeat_paused_until"])
            if data.get("heartbeat_paused_until")
            else None
        ),
    )


def spawn(
    prompt: str | None = None,
    fork_from: int | None = None,
    machine: str | None = None,
    config_overlay: dict[str, object] | None = None,
    preset: str | None = None,
) -> int:
    """Start a new agent; does not block.

    `prompt` is the first message (make it self-contained — the new agent has
    no context about why you spawned it); omit to leave it idling. `fork_from`
    copies another agent's conversation state. `machine` defaults to your own.
    `preset` names a saved config template to start the agent from; when both
    `preset` and `config_overlay` are given, `config_overlay` wins per field.

    Identity-class config you do not name — model, reasoning effort, skill set,
    prompt shaping — is taken from the cluster default at spawn time and frozen
    onto the new agent for its whole life, so a later change to that default
    never re-brains it. Operational knobs (compaction thresholds, timeouts) stay
    live and follow the cluster. `config_overlay={"eval_isolation": True,
    "eval_network_allowlist": ["web"]}` starts an eval-isolated agent and
    explicitly permits only the listed `web` or `understand` capability; `mcps`
    and `ui` are always disabled for isolated agents.
    """
    return _spawn_impl(
        prompt=prompt,
        fork_from=fork_from,
        machine=machine,
        config=config_overlay,
        label=None,
        preset=preset,
    )


def _spawn_impl(
    *,
    prompt: str | None,
    fork_from: int | None,
    machine: str | None,
    config: dict[str, object] | None,
    label: str | None,
    preset: str | None = None,
) -> int:
    # Shared spawn body. `label` is exposed on the public `spawn` only when the
    # ava_fleet plugin wraps it (the plugin passes a real label through here);
    # the unwrapped core spawn always passes label=None. `preset` is resolved to
    # its config template on the gateway side; only the explicit `config` overlay
    # is validated locally (the preset's own values are validated at child boot).
    prompt = coerce_str(prompt, "prompt", allow_none=True)
    fork_from = coerce_typed(fork_from, "fork_from", int, allow_none=True)
    machine = coerce_str(machine, "machine", allow_none=True)
    config = coerce_typed(config, "config", dict, allow_none=True)
    label = coerce_str(label, "label", allow_none=True)
    preset = coerce_str(preset, "preset", allow_none=True)
    spawner = ava._boot.require_actor()
    if config:
        from shared.plugin_config_registry import validate_config_overlay

        validate_config_overlay(config)
    return _client.spawn(
        spawner=spawner,
        prompt=prompt,
        fork_from=fork_from,
        prompt_source=spawner,
        machine=machine if machine is not None else ava.self.SELF_MACHINE_NAME,
        config=config,
        label=label,
        preset=preset,
    )


def terminate(
    agent_id: int,
    *,
    message: str | None = None,
    force: bool = False,
) -> TerminateResult:
    """End an agent after its current step. `message` is saved without another
    response and is available if the agent is later revived. `force=True`
    interrupts work; an `enqueued` result confirms acceptance, not exit."""
    agent_id = coerce_typed(agent_id, "agent_id", int)
    message = coerce_str(message, "message", allow_none=True)
    force = coerce_typed(force, "force", bool)
    return TerminateResult(_client.terminate(agent_id, message=message, force=force))


def restart(agent_id: int) -> RestartResult:
    """The agent finishes its current turn, then comes back up as a fresh
    process under the same id."""
    agent_id = coerce_typed(agent_id, "agent_id", int)
    return RestartResult(_client.restart(agent_id))


def resurrect(agent_id: int, prompt: str) -> ResurrectResult:
    """Wake a terminated agent with its previous conversation state intact."""
    agent_id = coerce_typed(agent_id, "agent_id", int)
    prompt = coerce_str(prompt, "prompt")
    return ResurrectResult(_client.resurrect(agent_id, prompt=prompt))


def commands() -> list[CommandInfo]:
    """List the commands a peer agent accepts; invoke one by sending
    `/name <instruction>` as the message text."""
    from ava._commands import discover_commands

    return [
        CommandInfo(
            name=c["name"], description=c["description"], instruction_hint=c["instruction_hint"]
        )
        for c in discover_commands()
    ]


def send_message(agent_id: int, content: str) -> None:
    """Does not wait or confirm delivery.

    A terminated target is auto-resurrected to handle the message.
    """  # lint-docstring: ok "auto-resurrected" is public behaviour, not impl detail
    agent_id = coerce_typed(agent_id, "agent_id", int)
    content = coerce_str(content, "content", allow_types=(list,))
    source = ava._boot.require_actor()
    _client.send_message(agent_id, content=content, source=source)


def send_system_note(
    agent_id: int,
    content: str,
    *,
    tag: str = "task",
    task_id: int | None = None,
    resurrect: bool = True,
) -> int:
    """Deliver a framework system note to another agent.

    The note appears in the target agent's timeline as a system note (no
    sender prefix, no peer timestamp), not as a chat message from you. `tag`
    selects the note chip — a NoteTag value; `task` is the task-notification
    family (assign / update / reminder). `resurrect` revives a terminated
    target so it can receive the note: pass True only for real work
    directions (a task assignment), never for plain notifications (user
    ruling 2026-08-27).

    `task_id` explicitly attributes the target's subsequent LLM work to one
    task. Leave it unset for a notification that does not drive task work.

    Returns the durable inbound id. Does not wait for the target to act.
    """  # lint-docstring: ok "resurrect" is public behaviour, not impl detail
    agent_id = coerce_typed(agent_id, "agent_id", int)
    content = coerce_str(content, "content")
    tag = coerce_str(tag, "tag")
    task_id = coerce_typed(task_id, "task_id", int, allow_none=True)
    if task_id is not None and task_id <= 0:
        raise ValueError(f"task_id must be a positive integer, got {task_id!r}")
    try:
        NoteTag(tag)
    except ValueError as exc:
        valid_tags = ", ".join(member.value for member in NoteTag)
        raise ValueError(f"tag must be one of: {valid_tags}; got {tag!r}") from exc
    if task_id is not None and tag != NoteTag.TASK.value:
        raise ValueError("task_id requires tag='task'")
    resurrect = coerce_typed(resurrect, "resurrect", bool)
    source = ava._boot.require_actor()
    return _client.send_system_note(
        agent_id,
        content=content,
        note_tag=tag,
        source=source,
        task_id=task_id,
        resurrect=resurrect,
    )


def get_last_message(agent_id: int) -> str | None:
    """Return the last message, or None when none yet.

    The returned text is peer-authored, so it passes through the same
    prompt-injection scan as inbound chat messages."""
    from ava.security import scan_content

    agent_id = coerce_typed(agent_id, "agent_id", int)
    caller = ava._boot.require_actor()
    message = _client.get_last_message(agent_id, caller)
    if message is not None:
        scan_content(message, source=f"peer.last_message:{agent_id}")
    return message


def get_status(agent_id: int) -> AgentStatus:
    agent_id = coerce_typed(agent_id, "agent_id", int)
    agents = _client.list_agents()
    for a in agents:
        if a["agent_id"] == agent_id:
            return AgentStatus(a["status"])
    raise AgentNotFound(f"Agent {agent_id} not found")
