"""Fork identity + prompt cross-process e2e — a forked agent learns who it is
and sees its prompt before the first turn.

Full cycle:
  1. source agent runs one turn (so it has a checkpoint to fork from), idles
  2. test forks it via POST /api/agents (fork_from=source, prompt=FORK_PROMPT)
     -> spawn_agent copies the checkpoint + INSERTs a kind='fork' lifecycle
        inbound in the same transaction, then the prompt as a separate
        pre-launch insert; both committed before the forked process launches
  3. forked process boots, its first claim grabs [fork marker, prompt] together,
     dispatches the identity marker + the prompt, runs its turn, idles

Asserts on the FORKED agent (new id, new process):
- inbound_messages has exactly [fork, chat] (identity marker + the prompt)
- timeline carries the lifecycle_fork system marker and the prompt's chat item
- its reply is FORK_OK — the forked fake only emits that when its first-turn
  context held BOTH the marker and the prompt (see fakes/scenarios/fork_identity)
"""

from __future__ import annotations

import contextlib
import time

import httpx
import psycopg
import pytest

from shared.agents import AgentStatus
from shared.config import settings
from tests.e2e._db import wait_for_status
from tests.e2e._env import E2EEnv
from tests.e2e.fakes.scenarios.fork_identity import FORK_OK, FORK_PROMPT


@pytest.mark.scenario("tests.e2e.fakes.scenarios.fork_identity:build")
def test_fork_delivers_identity_marker_and_prompt(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    source_id = e2e_env.agent_id
    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    # turn 1: source runs a turn so it commits a checkpoint the fork can copy.
    page.fill('[data-testid="composer-input"]', "源 agent 跑一轮")
    page.click('[data-testid="composer-send"]')
    wait_for_status(source_id, AgentStatus.IDLING.value)

    # fork the source, carrying a redirect prompt.
    resp = httpx.post(
        f"{e2e_env.gateway_url}/api/agents",
        json={
            "spawner": "user",
            "fork_from": source_id,
            "prompt": FORK_PROMPT,
            "prompt_source": "user",
        },
        timeout=90.0,  # spawn waits launch-confirm (25s e2e) + generous headroom
    )
    resp.raise_for_status()
    forked_id = int(resp.json()["id"])
    assert forked_id != source_id

    try:
        # forked process boots, claims [fork marker, prompt], runs its turn, idles.
        wait_for_status(forked_id, AgentStatus.IDLING.value)

        # DB: the forked agent received exactly the fork lifecycle marker then the
        # prompt — both committed before launch, so both in its first claim batch.
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT kind FROM inbound_messages WHERE agent_id = %s ORDER BY id", (forked_id,)
            )
            kinds = [r[0] for r in cur.fetchall()]
        assert kinds == ["fork", "chat"], kinds

        # Timeline (REST): the identity marker + prompt are in the forked agent's
        # message context, and its FORK_OK reply proves the model saw both.
        # Poll, not one-shot: status flips to IDLING at claim entry while the
        # turn's checkpoint write can still be in flight — a single GET races
        # that commit (caught live: replies=[] with kinds already committed).
        deadline = time.monotonic() + 90.0
        while True:
            items = httpx.get(
                f"{e2e_env.gateway_url}/api/agents/{forked_id}/timeline?limit=1000", timeout=90.0
            ).json()["items"]
            replies = [it["payload"] for it in items if it["kind"] == "agent_chat"]
            if FORK_OK in replies or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        assert any(it["source"] == "lifecycle_fork" for it in items), (
            f"fork identity marker missing from forked timeline: {items}"
        )
        assert any(it["kind"] == "inbound_chat" and FORK_PROMPT in it["payload"] for it in items), (
            f"prompt missing from forked timeline: {items}"
        )
        assert FORK_OK in replies, (
            f"forked agent did not confirm marker+prompt in its first-turn context; "
            f"replies={replies}"
        )
    finally:
        # The test spawned this agent directly (not via the spawned_agent fixture),
        # so terminate it before the function-scoped gateway tears down — a process
        # still alive at gateway death lingers holding DB conns and races the next
        # test's spawn (same reasoning as the spawned_agent fixture teardown).
        with contextlib.suppress(Exception):
            httpx.post(f"{e2e_env.gateway_url}/api/agents/{forked_id}/terminate", timeout=5.0)
            wait_for_status(forked_id, AgentStatus.TERMINATED.value, timeout=10.0)
