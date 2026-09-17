from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

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
from shared.agents import ForkConfigChangeNotAllowed as ForkConfigChangeNotAllowed
from shared.agents import ForkError as ForkError
from shared.agents import ForkSourceEmpty as ForkSourceEmpty
from shared.agents import GatewayUnavailable as GatewayUnavailable
from shared.agents import InvalidModelConfig as InvalidModelConfig
from shared.agents import MachineNotRegistered as MachineNotRegistered
from shared.agents import ResurrectError as ResurrectError
from shared.agents import SpawnTargetNotAgentRunner as SpawnTargetNotAgentRunner
from shared.config import cluster_tz, settings

from . import presets as presets

__all_for_ava__ = [
    "AgentDirectoryPage",
    "AgentRow",
    "AgentStatus",
    "CommandInfo",
    "Machine",
    "Neighbor",
    "OpenTaskRow",
    "OpenTasksHint",
    "RestartResult",
    "ResurrectResult",
    "TerminateOutcome",
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
    fork_source_agent_id: int | None
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
class AgentDirectoryPage:
    """One page, newest agent IDs first. Pass `next_cursor` as `before_id`
    with the same filters to read the next page; None means no more results.
    """

    agents: list[AgentRow]
    next_cursor: int | None


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


@dataclass
class OpenTaskRow:
    """One open task a terminated agent still owns: its id, title, status and
    last update time."""

    id: int
    title: str
    status: str
    updated_at: datetime

    def __str__(self) -> str:
        return f"#{self.id} | {self.title} | {self.status} | {_relative_time(self.updated_at)}"


@dataclass
class OpenTasksHint:
    """The open tasks a terminated agent still owns as it goes down: the total
    `count`, at most the five most recently updated `tasks`, and `more` — how
    many beyond those five remain."""

    count: int
    tasks: list[OpenTaskRow]
    more: int

    def __str__(self) -> str:
        lines = [f"{self.count} open task(s):", *(str(task) for task in self.tasks)]
        if self.more:
            lines.append(f"... and {self.more} more")
        return "\n".join(lines)


class TerminateOutcome(str):
    """What a terminate call returned: the acceptance status as a string —
    compare it directly ("enqueued" / "already_terminated") — plus `status` as
    the enum, and `open_tasks`: the tasks the agent still owned as it went
    down, or None."""

    __slots__ = ("open_tasks", "status")

    status: TerminateResult
    open_tasks: OpenTasksHint | None

    def __new__(
        cls, status: TerminateResult, open_tasks: OpenTasksHint | None = None
    ) -> TerminateOutcome:
        self = super().__new__(cls, status.value)
        self.status = status
        self.open_tasks = open_tasks
        return self

    def __repr__(self) -> str:
        return f"TerminateOutcome(status={self.status.value!r}, open_tasks={self.open_tasks!r})"


def get_neighbors(
    agent_id: int, depth: int | None = None, limit: int | None = None
) -> list[Neighbor]:
    """Rank the agents most strongly tied to `agent_id`.

    Ties form on spawn, fork, resurrect, or send_message and fade with time;
    `depth` is how many hops out to look. Omit `depth`/`limit` for the
    configured defaults (``display.neighbors_default_depth`` /
    ``display.neighbors_default_limit`` - 1 / 20 out of the box).
    """
    agent_id = coerce_typed(agent_id, "agent_id", int)
    resolved_depth: int
    if depth is None:
        resolved_depth = settings.display.neighbors_default_depth
    else:
        resolved_depth = coerce_typed(depth, "depth", int)
    resolved_limit: int
    if limit is None:
        resolved_limit = settings.display.neighbors_default_limit
    else:
        resolved_limit = coerce_typed(limit, "limit", int)
    return [
        Neighbor(
            agent_id=n["agent_id"],
            label=n.get("label"),
            status=AgentStatus(n["status"]),
            depth=n["depth"],
            score=n["score"],
        )
        for n in _client.get_neighbors(agent_id, depth=resolved_depth, limit=resolved_limit)
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
    *,
    scope: Literal["live", "terminated", "all"] = "live",
    query: str = "",
    before_id: int | None = None,
    limit: int = 100,
) -> AgentDirectoryPage:
    """Read one page of agents, newest IDs first.

    `live` includes every agent that has not terminated. Search by label
    substring or exact ID with `query` (at most 200 characters);
    use `terminated` for history or `all` for both. The page holds at most
    `limit` agents (1 through 200). Continue explicitly with `next_cursor`
    as `before_id`, retaining the same scope and query.
    """
    scope = coerce_str(scope, "scope")
    query = coerce_str(query, "query")
    if len(query) > 200:
        raise ValueError("query must be at most 200 characters")
    before_id = coerce_typed(before_id, "before_id", int, allow_none=True)
    limit = coerce_typed(limit, "limit", int)
    if scope not in ("live", "terminated", "all"):
        raise ValueError("scope must be 'live', 'terminated', or 'all'")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if before_id is not None and not 1 <= before_id <= 9223372036854775807:
        raise ValueError("before_id must be a positive bigint agent ID")
    page = _client.list_agents(scope=scope, query=query, before_id=before_id, limit=limit)
    return AgentDirectoryPage(
        agents=[_row_from_dict(row) for row in page["agents"]],
        next_cursor=page["next_cursor"],
    )


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
        fork_source_agent_id=data["fork_source_agent_id"],
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


def _open_tasks_from_dict(data: dict | None) -> OpenTasksHint | None:
    """Gateway JSON dict → OpenTasksHint; null passes through."""
    if data is None:
        return None
    return OpenTasksHint(
        count=data["count"],
        tasks=[
            OpenTaskRow(
                id=row["id"],
                title=row["title"],
                status=row["status"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in data["tasks"]
        ],
        more=data["more"],
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
    `config_overlay` names a saved config template through its `preset` key
    (`config_overlay={"preset": "name"}`); the preset's stored config is the
    base and the explicit fields win per key. The legacy `preset` argument is
    equivalent (deprecated) — passing both is a ValueError.

    A fork keeps the source agent's effective config so its inherited context
    stays cache-valid: at fork, `config_overlay` may only ADD skills to
    `skills_to_inject_into_system_prompt` / `skills_to_expand_at_start`
    (supersets — loaded at the context tail); any other change raises
    ForkConfigChangeNotAllowed.

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
    # the unwrapped core spawn always passes label=None. A preset — given as the
    # legacy `preset` argument or inside `config["preset"]` — is resolved to its
    # config template on the gateway side; only the explicit `config` fields are
    # validated locally (the preset's own values are validated at child boot).
    prompt = coerce_str(prompt, "prompt", allow_none=True)
    fork_from = coerce_typed(fork_from, "fork_from", int, allow_none=True)
    machine = coerce_str(machine, "machine", allow_none=True)
    config = coerce_typed(config, "config", dict, allow_none=True)
    label = coerce_str(label, "label", allow_none=True)
    preset = coerce_str(preset, "preset", allow_none=True)
    spawner = ava._boot.require_actor()
    if preset is not None:
        merged = dict(config) if config else {}
        if "preset" in merged:
            raise ValueError(
                "preset given twice — as the spawn `preset` argument and as "
                "config_overlay['preset']; pass only one"
            )
        merged["preset"] = preset
        config = merged
    if config:
        # The `preset` key is spawn-boundary metadata, not a Settings field: it
        # must not reach the overlay validators, which reject unknown keys.
        overlay = {k: v for k, v in config.items() if k != "preset"}
        if "preset" in config:
            name = config["preset"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f"config_overlay['preset'] must be a non-empty string, got {name!r}"
                )
        if overlay:
            from shared.plugin_config_registry import validate_config_overlay

            validate_config_overlay(overlay)
    return _client.spawn(
        spawner=spawner,
        prompt=prompt,
        fork_from=fork_from,
        prompt_source=spawner,
        machine=machine if machine is not None else ava.self.SELF_MACHINE_NAME,
        config=config,
        label=label,
        preset=None,
    )


def terminate(
    agent_id: int,
    *,
    message: str | None = None,
    force: bool = False,
    final: bool = False,
) -> TerminateOutcome:
    """End an agent after its current step. `message` is saved without another
    response and is available if the agent is later revived. `force=True`
    interrupts work; an `enqueued` result confirms acceptance, not exit.
    `final=True` closes the agent — never auto-resurrected (its queued work
    dead-letters on the existing thresholds); an explicit `resurrect` reopens it.

    The result compares as the status string (`== "enqueued"` works as before)
    and carries `open_tasks`: the tasks the agent still owns as it goes down
    (at most five, most recently updated first), or None when it leaves none."""
    agent_id = coerce_typed(agent_id, "agent_id", int)
    message = coerce_str(message, "message", allow_none=True)
    force = coerce_typed(force, "force", bool)
    final = coerce_typed(final, "final", bool)
    data = _client.terminate(agent_id, message=message, force=force, final=final)
    return TerminateOutcome(
        TerminateResult(data["status"]),
        _open_tasks_from_dict(data["open_tasks"]),
    )


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
    from shared.message_kwargs import NoteTag

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
    """Return the agent's most recent AI turn text, or None when none yet.

    The text is the agent's latest output, not necessarily addressed to you:
    it may be text sent to another agent or an update for the user, and it is
    not a message inbox. To check whether the agent sent you a message, look
    at the messages you received, not this text. While the agent is working it
    can be a half-finished preamble that ends mid-sentence (often with a
    trailing colon), not a conclusion. The text is peer-authored, so it
    passes through the same prompt-injection scan as inbound chat messages."""
    from ava.security import scan_content

    agent_id = coerce_typed(agent_id, "agent_id", int)
    caller = ava._boot.require_actor()
    message = _client.get_last_message(agent_id, caller)
    if message is not None:
        scan_content(message, source=f"peer.last_message:{agent_id}")
    return message


def get_status(agent_id: int) -> AgentStatus:
    agent_id = coerce_typed(agent_id, "agent_id", int)
    return AgentStatus(_client.get_agent(agent_id)["status"])
