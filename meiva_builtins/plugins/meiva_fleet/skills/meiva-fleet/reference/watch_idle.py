"""Reference watcher body for supervising a fleet worker.

Launch this in the background with `meiva.watcher.launch(code=..., timeout=..., name="...")` after
substituting TARGET_AGENT_ID. It waits until the agent you are watching finishes
a turn and goes idle, then sends you a message (`meiva.agents.send_message`) once so you wake up
and judge that agent's progress against the goal.

The wake is sent once; the SDK's own bounded retries are the only retry. If
the send still fails the watcher logs the error and exits 2, so the launcher
gets an exit notice and re-launches; a wake that lands in a gateway / agent
restart window is not redelivered.

Runtime notes:
- This program runs under the same Python environment, configuration, and
  machine credentials as the agent that launched it.
- `meiva.agents.send_message(meiva.self.AGENT_ID, content)` delivers `content` back to you (the launching agent)
  as a new incoming message.
- Connection settings are read from the launching agent's own configuration, so
  the watcher always listens on the same event stream the watched agent reports
  to.
- `socket_timeout=None` is mandatory: redis-py 8 defaults it to 5s, which kills
  pubsub.listen()'s long blocking read when the event stream goes quiet. If the
  stream still dies, the watcher falls back to polling the target's status.
"""

import json
import time

import redis

import meiva
from base.config import settings

# Substitute before launching: the id of the agent you want to watch.
TARGET_AGENT_ID = 0

# An agent reports a lifecycle update every time its status changes. "idling"
# means it ended its turn and is waiting for the next message -- the moment to
# check whether the goal is met.
IDLE_STATUS = "idling"


def _is_target_idle(event: dict, target_id: int) -> bool:
    """A matching lifecycle hint requests a fresh authoritative status read."""
    if event.get("role") != "agent_updated" or event["agent_id"] != target_id:
        return False
    return meiva.agents.get_status(target_id) == IDLE_STATUS


def _notify(target_id: int) -> None:
    """Deliver the idle reminder once; a failed send logs and exits 2."""
    message = (
        f"target agent {target_id} idled -- inspect its recent output and judge it against the goal"
    )
    try:
        meiva.agents.send_message(meiva.self.AGENT_ID, message)
    except Exception as exc:  # a lost wake must surface as an exit notice
        print(f"wake delivery failed: {exc!r}", flush=True)
        raise SystemExit(2) from None


def _watch_via_poll(target_id: int, interval_s: float = 5.0) -> None:
    """Fallback: poll the target's status until it idles, then remind once.

    Used when the event stream is unavailable, when the target may ALREADY be
    idle before the watcher starts, or when a pubsub read died mid-watch. It
    reads the same authoritative status as the event path. A transient read
    error only skips one round; the 6h `meiva.watcher.launch` timeout remains the
    outer safety bound, so a persistently failing read also wakes the launching
    agent eventually (via the watcher's own exit).
    """
    failing = False
    while True:
        try:
            idle = meiva.agents.get_status(target_id) == IDLE_STATUS
        except Exception as exc:
            if not failing:
                failing = True
                print(f"status read failed, retrying every {interval_s}s: {exc!r}", flush=True)
            time.sleep(interval_s)
            continue
        if failing:
            failing = False
            print("status read recovered", flush=True)
        if idle:
            _notify(target_id)
            return
        time.sleep(interval_s)


def watch(target_id: int) -> None:
    """Block until `target_id` next goes idle, remind once, then return.

    One-shot by design: after it reminds you, this watcher exits. If the target
    is not done yet, launch a fresh watcher to wait for its next idle. Launch
    the watcher BEFORE the target starts working -- an idle transition that
    happens before the subscription is established is missed (the poll fallback
    covers the already-idle case).
    """
    client = redis.Redis.from_url(
        settings.data_plane.redis_url,
        decode_responses=True,
        # redis-py 8 defaults to 5s, which kills a quiet pubsub.listen() read.
        # Observed 2026-08-13: the watcher died mid-watch before target idle.
        socket_timeout=None,
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
    except (
        redis.TimeoutError,
        TimeoutError,
        ConnectionError,
        OSError,
        meiva.agents.GatewayUnavailable,
    ):
        # The stream died mid-watch (redis restart, socket timeout, ...). The
        # target may idle -- or already have -- while we are blind: poll the
        # table so the launching agent does not stall silently.
        _watch_via_poll(target_id)


if __name__ == "__main__":
    watch(TARGET_AGENT_ID)
