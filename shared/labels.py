"""Label event publish helper — shared by gateway + services/labeler.

`publish_label_updated` pushes a LabelUpdated event to the Redis
`ava:events` channel so the frontend's SSE sees label changes in real
time. Two callers:
- `gateway/app.py` — when PATCH /api/agents/{id} manually changes label
- `services/labeler/labeler.py` — after LLM auto-generated label succeeds
"""

from shared.config import settings
from shared.live_events import LabelUpdated
from shared.redis_client import publish_best_effort


async def publish_label_updated(agent_id: int, label: str | None) -> None:
    """Publish LabelUpdated to the Redis events channel — best-effort, never raises.

    label=None means already reset back to "not set" (PATCH body
    label="" takes this branch).
    """
    ev = LabelUpdated(agent_id=agent_id, label=label)
    await publish_best_effort(
        settings.data_plane.events_channel, ev.model_dump_json(), context="label_updated"
    )
