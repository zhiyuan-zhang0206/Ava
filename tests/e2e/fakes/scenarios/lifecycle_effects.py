from __future__ import annotations

import psycopg

from base.config import settings
from tests.e2e.fakes.scenario_recording import RecordingModel, exec_call, say, scratch_root

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
    if _is_peer(agent_id):
        return RecordingModel(agent_id=agent_id, script=(say(PEER_REPLY),))
    code = (
        "import meiva\n"
        f"peer = int(open({peer_id_file()!r}).read())\n"
        f"meiva.agents.send_message(peer, {PING!r})\n"
        "print('sent-to', peer)"
    )
    return RecordingModel(agent_id=agent_id, script=(exec_call(1, code), say(FINAL)))


def build_spawn(model: str, *, agent_id: int | None) -> RecordingModel:
    if _is_peer(agent_id):
        return RecordingModel(agent_id=agent_id, script=(say(CHILD_REPLY),))
    code = (
        "import meiva\nfrom uuid import uuid4\n"
        f"child = meiva.agents.spawn(prompt={CHILD_PROMPT!r}, idempotency_key=str(uuid4()))\n"
        "print('child-id', child)"
    )
    return RecordingModel(agent_id=agent_id, script=(exec_call(1, code), say(FINAL)))


def build_terminate(model: str, *, agent_id: int | None) -> RecordingModel:
    return RecordingModel(agent_id=agent_id, script=(say("first reply"), say(FINAL)))


def build_cancel(model: str, *, agent_id: int | None) -> RecordingModel:
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
    code = f"import meiva\nmeiva.self.compact({COMPACT_SUMMARY!r})"
    return RecordingModel(agent_id=agent_id, script=(exec_call(1, code), say(FINAL)))
