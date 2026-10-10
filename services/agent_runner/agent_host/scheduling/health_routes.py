"""Loopback health routes expose the actual hosted owner and cancellation boundary."""

import os

from base import paths
from base.daemon.health import RouteHandler
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.host import AgentHost


def cancel_turn_route(scheduler: TurnScheduler, host: AgentHost) -> RouteHandler:
    """A `POST /cancel-turn` handler — the hosted force-terminate / wedged
    recovery primitive.

    Body: `{"agent_id": <int>, "command_id": <int>}`. Cancels the captured task with the
    bounded unwind (a C-call-blocked turn is reported, not awaited forever) and
    answers `{"cancelled": true|false}` — false means no task was running,
    which the ops caller treats as "nothing to accelerate", never as an error.

    Loopback-only and unauthenticated, like `/healthz`: anything that can dial
    the host's localhost health port already owns the box. The durable
    terminate/restart inbound is always the correctness mechanism — this
    endpoint only accelerates a turn stuck inside a long await.
    """

    async def handler(body: bytes) -> tuple[int, bytes, str]:
        import json

        try:
            payload = json.loads(body or b"{}")
            agent_id, command_id = payload["agent_id"], payload["command_id"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return (
                400,
                json.dumps({"error": "positive agent_id and command_id required"}).encode(),
                "application/json",
            )
        if (
            type(agent_id) is not int
            or type(command_id) is not int
            or agent_id <= 0
            or command_id <= 0
        ):
            return 400, b'{"error":"positive integer identifiers required"}', "application/json"
        cancelled = await scheduler.cancel_exact_force(agent_id, command_id, host.accepts_force)
        return 200, json.dumps({"cancelled": cancelled}).encode(), "application/json"

    return handler


def stats_route(host: AgentHost, scheduler: TurnScheduler) -> RouteHandler:
    """Expose cache/activity counters and this running boot's maintenance identity."""
    import json

    async def handler(_body: bytes) -> tuple[int, bytes, str]:
        # Per-agent turn-progress age: the health signal that separates
        # "the host process is alive" from "this turn is alive". A busy agent
        # (progress every couple of minutes) reads small; an agent whose
        # invocation has been silent for the wedged budget reads large — the
        # turn-level fake-alive state a heartbeat probe alone cannot see.
        active_progress: dict[int, float] = {}
        for agent_id in sorted(scheduler.active_agents):
            age = host.turn_progress.age_s(agent_id)
            if age is not None:
                active_progress[agent_id] = round(age, 1)
        payload = {
            **host.stats.as_payload(),
            **host.admission.payload(),
            "maintenance_protocol": 1,
            "runtime_owner": str(host.runtime_owner),
            "home": str(paths.ava_home()),
            "pid": os.getpid(),
            "active_agents": sorted(scheduler.active_agents),
            "active_progress": active_progress,
        }
        return 200, json.dumps(payload).encode(), "application/json"

    return handler
