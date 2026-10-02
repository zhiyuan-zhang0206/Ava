"""Label helpers shared by agent birth, gateway, and services/labeler.

`publish_label_updated` pushes a LabelUpdated event to the Redis
`ava:events` channel so the frontend's SSE sees label changes in real
time. Two callers:
- `gateway/app.py` — when PATCH /api/agents/{id} manually changes label
- `services/labeler/labeler.py` — after LLM auto-generated label succeeds
"""

from base.events.live.bus import EventBus
from base.events.live.projection import LabelUpdated


def spawn_prompt_with_label(prompt: str, label: str | None) -> str:
    """Append the initial label notice to a spawn prompt when a label exists."""
    return f"{prompt}\n\nYour label has been set to {label}." if label else prompt


async def publish_label_updated(bus: EventBus, agent_id: int, label: str | None) -> None:
    """Publish LabelUpdated to the Redis events channel — best-effort, never raises.

    label=None means already reset back to "not set" (PATCH body
    label="" takes this branch).
    """
    ev = LabelUpdated(agent_id=agent_id, label=label)
    await bus.publish_best_effort(ev.model_dump_json(), context="label_updated")
