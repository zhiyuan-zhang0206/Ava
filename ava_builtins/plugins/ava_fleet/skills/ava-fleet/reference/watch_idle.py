"""Reference watcher body for supervising a fleet worker.

Launch this in the background with `ava.watcher.launch(code=..., timeout=..., name="...")` after
substituting TARGET_AGENT_ID. It waits until the agent you are watching finishes
a turn and goes idle, then sends you a message (`ava.agents.send_message`) once so you wake up
and judge that agent's progress against the goal.

Runtime notes:
- This program runs under the same Python environment, configuration, and
  machine credentials as the agent that launched it.
- `ava.agents.send_message(ava.self.AGENT_ID, content)` delivers `content` back to you (the launching agent)
  as a new incoming message.
- Connection settings are read from the launching agent's own configuration, so
  the watcher always listens on the same event stream the watched agent reports
  to.
- `socket_timeout=None` is mandatory: redis-py 8 defaults it to 5s, which kills
  pubsub.listen()'s long blocking read when the event stream goes quiet. If the
  stream still dies, the watcher falls back to polling the agents table.
"""

import json

import redis

import ava
from shared.config import settings

# Substitute before launching: the id of the agent you want to watch.
TARGET_AGENT_ID = 0

# An agent reports a lifecycle update every time its status changes. "idling"
# means it ended its turn and is waiting for the next message -- the moment to
# check whether the goal is met.
IDLE_STATUS = "idling"


def _is_target_idle(event: dict, target_id: int) -> bool:
    """True when `event` reports that agent `target_id` just went idle.

    `event` is one decoded lifecycle update from the shared event stream. Only
    status-change updates carry a snapshot; everything else is ignored.
    """
    if event.get("role") != "agent_updated":
        return False
    return event["agent_id"] == target_id and event["snapshot"]["status"] == IDLE_STATUS


def _notify(target_id: int) -> None:
    ava.agents.send_message(
        ava.self.AGENT_ID,
        f"target agent {target_id} idled -- inspect its recent output "
        "and judge it against the goal",
    )


def _watch_via_poll(target_id: int, interval_s: float = 5.0) -> None:
    """Fallback: poll the agents table until the target idles, then remind once."""
    import time

    import psycopg

    while True:
        try:
            with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
                cur.execute("SELECT status FROM agents_meta WHERE id = %s", (target_id,))
                row = cur.fetchone()
        except Exception:
            time.sleep(interval_s)
            continue
        if row is not None and row[0] == IDLE_STATUS:
            _notify(target_id)
            return
        time.sleep(interval_s)


def watch(target_id: int) -> None:
    """Block until `target_id` next goes idle, remind once, then return.

    One-shot by design: after it reminds you, this watcher exits. If the target
    is not done yet, launch a fresh idle-watch watcher to wait for its next
    idle. Launch the watcher BEFORE the target starts working -- an idle
    transition that happens before the subscription is established is missed
    (the poll fallback covers the already-idle case).
    """
    client = redis.Redis.from_url(
        settings.data_plane.redis_url,
        decode_responses=True,
        socket_timeout=None,  # redis-py 8 defaults 5s -- kills long pubsub reads
    )
    pubsub = client.pubsub()
    pubsub.subscribe(settings.data_plane.events_channel)
    try:
        for message in pubsub.listen():
            if message["type"] != "message":
                continue  # skip the subscribe-confirmation control frame
            payload = message["data"]
            if not isinstance(payload, str):
                continue  # every published event is a JSON string
            event = json.loads(payload)
            if _is_target_idle(event, target_id):
                _notify(target_id)
                return
    except (redis.TimeoutError, TimeoutError, ConnectionError, OSError):
        # Stream died mid-watch; poll so the caller is not left waiting blind.
        _watch_via_poll(target_id)


if __name__ == "__main__":
    watch(TARGET_AGENT_ID)
