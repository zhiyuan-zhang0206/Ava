"""Agent process lifecycle events: spawn, resurrect, terminate, launch, wake, node and halt."""

from __future__ import annotations

from typing import TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class Halt(TypedDict):
    """`halt` payload — compact/idle detection reads the body."""

    body: str


class ServiceStarted(TypedDict):
    """`service_started` payload — base/log/__init__.py."""

    name: str
    pid: int


class AgentSpawned(TypedDict):
    """`agent_spawned` payload — ops/agents/spawn.py."""

    spawner: str  # "user" | "agent:<id>" | "scheduler" | ...
    forked_from: int | None


class AgentContinuationLost(TypedDict):
    """`agent_continuation_lost` payload — cli/commands/lifecycle/_failed_receipts.py.

    One row per agent whose continuation failed during a stop's drain and whose
    durable restart pointer is gone, so `ava start` cannot re-deliver it.
    ``failure_category`` is the drain's failure class, ``holder`` the maintenance
    hold, ``reason`` why the pointer is lost.
    """

    failure_category: str
    holder: str | None
    reason: str


class NodeExitEntry(TypedDict):
    """One node's exit inside an aggregated per-turn `node_exit` event."""

    node: str
    outcome: str  # ok | cancelled
    duration_seconds: float


class NodeExit(TypedDict):
    """`node_exit` payload — one aggregated event per graph turn (agent/graph/node_log.py)."""

    count: int
    nodes: list[NodeExitEntry]


class NativeCancelUncertain(TypedDict):
    """`native_cancel_uncertain` identifies the original work held for proof recovery."""

    agent_id: int
    work_id: str


EVENTS: dict[str, EventSpec] = {
    "native_cancel_uncertain": telemetry_event(
        "native_cancel_uncertain",
        "native cancel recovery lacks exact execution or certified stop proof; "
        "the supported consumer holds new work",
        payload=NativeCancelUncertain,
        tier="anomaly",
    ),
    "agent_continuation_lost": telemetry_event(
        "agent_continuation_lost",
        "an agent's continuation failed at a maintenance hold and its restart pointer is gone, "
        "so `ava start` cannot re-deliver it",
        payload=AgentContinuationLost,
        tier="anomaly",
    ),
    # node / process lifecycle
    "node_enter": telemetry_event(
        "node_enter",
        "LangGraph node entered — sink-filtered out of the event stream (PR #1758); log files only",
        destination="file",
        tier="noise",
    ),
    "node_exit": telemetry_event(
        "node_exit", "LangGraph node exited", payload=NodeExit, tier="noise"
    ),
    "service_started": telemetry_event(
        "service_started",
        "gateway/daemon started",
        payload=ServiceStarted,
        tier="noise",
        persist=True,
    ),
    "halt": telemetry_event(
        "halt",
        "turn stopped (idle/compact/system)",
        payload=Halt,
        tier="noise",
        persist=True,
    ),
    "agent_spawned": telemetry_event(
        "agent_spawned",
        "agent process started",
        payload=AgentSpawned,
        persist=True,
    ),
    "agent_resurrected": telemetry_event("agent_resurrected", "agent resurrected", persist=True),
    "billing_resurrect_run": telemetry_event(
        "billing_resurrect_run",
        "billing batch recovery run finished",
        site=(
            'ops/lifecycle/billing_recovery.py:_record_run_event telemetry.emit("telemetry", ...)'
        ),
    ),
    "auto_resurrect_refused": telemetry_event(
        "auto_resurrect_refused",
        "an automatic resurrect of a terminated agent was refused (e.g. "
        "runtime_cutover_required); the triggering inbound stays queued until an "
        "operator resolves the named reason",
        tier="anomaly",
    ),
    "auto_resurrect_failed": telemetry_event(
        "auto_resurrect_failed",
        "an automatic resurrect failed for a possibly transient reason; the "
        "triggering inbound stays queued for a later or manual resurrect",
    ),
    # agent lifecycle / state
    "wake_degraded": telemetry_event(
        "wake_degraded",
        "RedisInboundListener wake path degraded (instant pub/sub wake off)",
        tier="anomaly",
    ),
    "wake_restored": telemetry_event(
        "wake_restored",
        "RedisInboundListener wake path recovered (clean consume restored instant wake)",
        tier="noise",
    ),
}
