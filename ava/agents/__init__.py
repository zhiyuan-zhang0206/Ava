"""Interact with your peer agents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import ava
import ava._boot
from ava import _gateway_client as _client

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
    """`depth`: hops away (1 = direct); `score`: tie strength. Terminated
    agents can still be neighbors."""

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
    delta = datetime.now().astimezone() - dt.astimezone()
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
    """Rank the agents most strongly tied to `agent_id`, strongest first.

    Ties form on spawn, fork, resurrect, or send_message and fade with time;
    `depth` is how many hops out to look.
    """
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


def list_agents(
    filter_by_status: tuple[AgentStatus, ...] | None = (
        AgentStatus.RUNNING,
        AgentStatus.IDLING,
    ),
) -> list[AgentRow]:
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
    """Start a new agent and return its id. Does not block.

    `prompt` is the first message (make it self-contained — the new agent has
    no context about why you spawned it); omit to leave it idling. `fork_from`
    copies another agent's conversation state. `machine` defaults to your own.
    `preset` names a saved config template to start the agent from; when both
    `preset` and `config_overlay` are given, `config_overlay` wins per field.

    Identity-class config you do not name — model, reasoning effort, skill set,
    prompt shaping — is taken from the cluster default at spawn time and frozen
    onto the new agent for its whole life, so a later change to that default
    never re-brains it. Operational knobs (compaction thresholds, timeouts) stay
    live and follow the cluster.
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


def terminate(agent_id: int, *, force: bool = False) -> TerminateResult:
    """The agent finishes its current turn, then exits; `force=True` kills
    its process immediately instead."""
    return TerminateResult(_client.terminate(agent_id, force=force))


def restart(agent_id: int) -> RestartResult:
    """The agent finishes its current turn, then comes back up as a fresh
    process under the same id."""
    return RestartResult(_client.restart(agent_id))


def resurrect(agent_id: int, prompt: str) -> ResurrectResult:
    """Wake a terminated agent with its previous conversation state intact."""
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
    """Send a message to another agent. Does not wait or confirm delivery.

    A terminated target is auto-resurrected to handle the message.
    """  # lint-docstring: ok "auto-resurrected" is public behaviour, not impl detail
    source = ava._boot.require_actor()
    _client.send_message(agent_id, content=content, source=source)


def get_last_message(agent_id: int) -> str | None:
    """Return the text of an agent's last message, or None when it has
    produced none yet.

    The returned text is peer-authored, so it passes through the same
    prompt-injection scan as inbound chat messages (audit round-2
    up-security-trust P1-4: the pull path bypassed the scan the push
    path has)."""
    from ava.security import scan_content

    caller = ava._boot.require_actor()
    message = _client.get_last_message(agent_id, caller)
    if message is not None:
        scan_content(message, source=f"peer.last_message:{agent_id}")
    return message


def get_status(agent_id: int) -> AgentStatus:
    """Return the current status of an agent."""
    agents = _client.list_agents()
    for a in agents:
        if a["agent_id"] == agent_id:
            return AgentStatus(a["status"])
    raise AgentNotFound(f"Agent {agent_id} not found")
