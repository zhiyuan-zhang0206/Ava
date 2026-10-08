"""Agent-lifecycle effect scenarios: what an agent, a peer or the model actually sees.

All use the recording model (`fakes/_recording.py`): real agent turns, real SDK calls from
a real exec child, and the test asserts on the messages each agent's model was handed.

Two-agent scenarios (`relay`, `spawn`) tell the roles apart the way the fork scenario
does: a *peer* is the agent whose inbound came from another agent (`source LIKE
'agent:%'`), committed before its process starts; the initiating agent has none.
"""

from __future__ import annotations

import psycopg

from base.config import settings
from tests.e2e.fakes._recording import RecordingModel, exec_call, say, scratch_root

PING = "PING-FROM-A: please confirm you got this."
PEER_REPLY = "PEER-GOT-IT"
CHILD_PROMPT = "CHILD-TASK-MARK: report that you started."
CHILD_REPLY = "CHILD-STARTED"
TERMINATE_NOTE = "WRAPUP-NOTE: finish the report when you wake."
COMPACT_SUMMARY = "COMPACT-SUMMARY-MARK: I was asked to compact."
FINAL = "scenario finished."
LONG_SLEEP_SECONDS = 10


def peer_id_file() -> str:
    return str(scratch_root("lifecycle") / "peer_id")


def exec_finished_marker() -> str:
    return str(scratch_root("lifecycle") / "exec-finished")


def _is_peer(agent_id: int | None) -> bool:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages WHERE agent_id = %s AND source LIKE 'agent:%%' LIMIT 1",
            (agent_id,),
        )
        return cur.fetchone() is not None


def build_relay(model: str, *, agent_id: int | None) -> RecordingModel:
    """A sends B a message through the SDK; B answers."""
    if _is_peer(agent_id):
        return RecordingModel(agent_id=agent_id, script=(say(PEER_REPLY),))
    code = (
        "import ava\n"
        f"peer = int(open({peer_id_file()!r}).read())\n"
        f"ava.agents.send_message(peer, {PING!r})\n"
        "print('sent-to', peer)"
    )
    return RecordingModel(agent_id=agent_id, script=(exec_call(1, code), say(FINAL)))


def build_spawn(model: str, *, agent_id: int | None) -> RecordingModel:
    """A spawns a child through the SDK with a first prompt; the child answers."""
    if _is_peer(agent_id):
        return RecordingModel(agent_id=agent_id, script=(say(CHILD_REPLY),))
    code = (
        f"import ava\nchild = ava.agents.spawn(prompt={CHILD_PROMPT!r})\nprint('child-id', child)"
    )
    return RecordingModel(agent_id=agent_id, script=(exec_call(1, code), say(FINAL)))


def build_terminate(model: str, *, agent_id: int | None) -> RecordingModel:
    """One reply before the external terminate, one after the revival."""
    return RecordingModel(agent_id=agent_id, script=(say("first reply"), say(FINAL)))


def build_cancel(model: str, *, agent_id: int | None) -> RecordingModel:
    """A long exec the test cancels, then a normal reply to the next message."""
    code = (
        "import time\n"
        "print('exec-started')\n"
        f"time.sleep({LONG_SLEEP_SECONDS})\n"
        f"open({exec_finished_marker()!r}, 'w').write('done')\n"
        "print('exec-finished')"
    )
    return RecordingModel(
        agent_id=agent_id, script=(exec_call(1, code), say("after cancel"), say(FINAL))
    )


def build_self_compact(model: str, *, agent_id: int | None) -> RecordingModel:
    code = f"import ava\nava.self.compact({COMPACT_SUMMARY!r})"
    return RecordingModel(agent_id=agent_id, script=(exec_call(1, code), say(FINAL)))
