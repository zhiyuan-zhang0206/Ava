"""Hibernation full-link e2e — the process is REALLY killed and REALLY relaunched.

Closes the loop the unit tests can only approximate (they stub the launch):

  spawn → chat 1 → agent turn → idle (pid1, 1 AIMessage)
  → test identity-probes pid1 and sends SIGUSR1 to it (exactly what the
    controller's _signal_swap_out does — probe first, signal only on OWNED)
    → the agent converts it to a clean exit → POST /api/agents/{id}/hibernating
    → row parks 'hibernating', pid1 process is gone
  → a heartbeat inbound is inserted (exactly what the heartbeat daemon's
    _send_heartbeat_checkin does — the "heartbeat wakes a hibernating agent" link)
    → the restarter's HibernateController swap-in poll sees hibernating + pending
    inbound → swap_in_agent relaunches a REAL new process (pid2 != pid1) → it
    restores the checkpoint and runs the heartbeat turn → idle (2 AIMessages)

Asserts the mechanism end to end:
- the row really reached 'hibernating' (swap-out landed)
- pid changed and the old process is dead (real kill), the new one is alive (real launch)
- the wake is INVISIBLE: no "resurrected"/"restarted" lifecycle marker in the
  checkpoint — the agent sees only the two human chats and its two replies
- both chats survived: 2 HumanMessages + 2 AIMessages in the restored history

Spontaneous swap-out is disabled cluster-wide in the e2e env
(AVA_HIBERNATE_ENABLED=false, see conftest `_e2e_process_env`) so this test drives
swap-out itself; the swap-IN poll runs unconditionally, so the wake is the real
controller path. The controller's swap-out SELECT + signal are unit-tested in
tests/services/test_hibernation.py.
"""

from __future__ import annotations

import os
import signal
import time

import httpx
import psycopg
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.postgres import PostgresSaver

from ops.agent_identity import AgentProcessIdentity, probe_agent_process
from shared.agents import AgentStatus
from shared.config import settings
from tests.e2e._ports import GATEWAY_URL


def _checkpoint_messages(agent_id: int) -> list:
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        tup = saver.get_tuple({"configurable": {"thread_id": str(agent_id)}})
    if tup is None:
        return []
    return tup.checkpoint.get("channel_values", {}).get("messages", [])


def _status_pid(agent_id: int) -> tuple[str | None, int | None]:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours to signal


def _send_chat(agent_id: int, text: str) -> None:
    httpx.post(
        f"{GATEWAY_URL}/api/agents/{agent_id}/messages",
        json={"content": text, "source": "user"},
        timeout=10.0,
    ).raise_for_status()


def _insert_heartbeat_inbound(agent_id: int) -> None:
    """The exact INSERT the heartbeat daemon's `_send_heartbeat_checkin` does — a
    `heartbeat` inbound with no process listening. For a hibernating agent it is
    the wake that the controller's swap-in poll picks up."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, 'Heartbeat.', 'heartbeat', 'system')",
            (agent_id,),
        )
        conn.commit()


def _wait(predicate, *, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.3)
    raise RuntimeError(f"timed out after {timeout}s waiting for {what}; last={last!r}")


@pytest.mark.scenario("tests.e2e.fakes.scenarios.hibernate_cycle:build")
def test_hibernate_swap_out_and_wake_is_silent(spawned_agent: int, restarter_proc: None) -> None:
    agent_id = spawned_agent

    # ── chat 1: agent answers, idles (pid1, 1 AIMessage) ──
    _send_chat(agent_id, "first")

    def _one_turn_done():
        status, pid = _status_pid(agent_id)
        ai = sum(1 for m in _checkpoint_messages(agent_id) if isinstance(m, AIMessage))
        return (pid, ai) if status == AgentStatus.IDLING.value and ai == 1 and pid else None

    pid1, _ = _wait(_one_turn_done, timeout=90.0, what="chat-1 turn to finish + idle")

    # ── swap-out ──
    # The controller signals only a pid the OS confirms is that agent's own
    # process (issue #1123). Assert that here, against a REAL agent launched the
    # real way: this is the only place the argv matcher meets a genuine
    # prod-shaped process, so it is the only place that can catch a launcher argv
    # the probe would misread as a stranger — which in prod would be a fleet-wide
    # reap, not a test failure.
    assert probe_agent_process(pid1, agent_id) is AgentProcessIdentity.OWNED

    # …then the real signal the controller sends (os.kill SIGUSR1).
    os.kill(pid1, signal.SIGUSR1)

    def _hibernating_and_dead():
        status, _ = _status_pid(agent_id)
        return status == AgentStatus.HIBERNATING.value and not _pid_alive(pid1)

    _wait(_hibernating_and_dead, timeout=30.0, what="row 'hibernating' + pid1 process gone")

    # ── heartbeat wake: the daemon's nudge INSERT for a hibernating agent; the
    #    swap-in poll picks it up and relaunches a REAL new process, which runs. ──
    _insert_heartbeat_inbound(agent_id)

    def _woken_and_second_turn_done():
        status, pid = _status_pid(agent_id)
        ai = sum(1 for m in _checkpoint_messages(agent_id) if isinstance(m, AIMessage))
        if status == AgentStatus.IDLING.value and ai == 2 and pid and pid != pid1:
            return pid
        return None

    pid2 = _wait(
        _woken_and_second_turn_done, timeout=90.0, what="heartbeat swap-in + turn to finish"
    )

    # ── real kill + real launch ──
    assert pid2 != pid1, "swap-in must launch a fresh process (new pid)"
    assert _pid_alive(pid2), "the relaunched process must be alive"
    assert not _pid_alive(pid1), "the swapped-out process must be dead"

    # ── invisible wake: chat-1 survived, the heartbeat woke a real turn, no marker ──
    messages = _checkpoint_messages(agent_id)
    ais = [m for m in messages if isinstance(m, AIMessage)]
    assert len(ais) == 2, f"expected exactly 2 AI turns across the swap, got {len(ais)}"
    # langchain stub — message.content has Unknown slots.
    joined = " ".join(
        str(m.content)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        for m in messages
    )
    # chat-1 was restored from the checkpoint after the swap.
    assert "first" in joined, f"the pre-swap chat was lost across the swap; history={joined!r}"
    # Airtight invisibility: swap_in inserts NO lifecycle inbound — the only inbounds
    # are chat-1 and the heartbeat that woke it (unlike resurrect/restart, which
    # insert a 'resurrect'/'restart_completed' kind rendered into a "You have been …"
    # marker).
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT kind FROM inbound_messages WHERE agent_id = %s ORDER BY kind",
            (agent_id,),
        )
        kinds = [r[0] for r in cur.fetchall()]
    assert kinds == ["chat", "heartbeat"], (
        f"hibernation swap-in leaked a lifecycle inbound: kinds={kinds!r}"
    )
    for marker in ("You have been resurrected", "You have been restarted"):
        assert marker not in joined, f"hibernation leaked a {marker!r} marker into the history"
