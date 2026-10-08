"""Actual peer usage -> ordinary observer message -> preserved partial handoff.

No browser or paid model: scripted choices run on the real gateway/agent host.
Two cases represent a goal supervisor and a script orchestrator. Neither case
proves that an unprompted language model will choose the correct disposition.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from base.config import settings
from base.host.atomic_io import write_text_atomic
from tests.components.base.poll_until import poll_until
from tests.e2e._db import chat_and_wait, checkpoint_values, wait_for_status
from tests.e2e._ports import GATEWAY_URL
from tests.e2e.fakes._recording import model_inputs, reset_record, scratch_root

_OBSERVER = (
    Path(__file__).resolve().parents[3]
    / "ava_builtins/skills/coordination/ava-being-a-long-running-agent/scripts/agent_usage.py"
)


@pytest.fixture
def budget_world(spawned_agent: int) -> Iterator[Path]:
    reset_record()
    root = scratch_root("budget")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    (root / "owner").write_text(str(spawned_agent))
    yield root
    shutil.rmtree(root, ignore_errors=True)
    reset_record()


def _state(root: Path, agent_id: int) -> dict[str, Any]:
    return json.loads((root / f"{agent_id}.json").read_text())


def _paused(root: Path, agent_id: int) -> tuple[bool, object]:
    path = root / f"{agent_id}.json"
    state = _state(root, agent_id) if path.exists() else {}
    return state.get("status") == "paused" and len(model_inputs(agent_id)) >= 4, state


def _terminate(agent_id: int) -> None:
    # Test-owned cleanup, after assertions that the reminder never terminated peers.
    # Force also reaps a failed scripted model stuck in the kernel retry loop.
    httpx.post(
        f"{GATEWAY_URL}/api/agents/{agent_id}/terminate",
        json={"force": True},
        timeout=10,
    ).raise_for_status()
    wait_for_status(agent_id, "terminated")


@pytest.mark.scenario("tests.e2e.fakes.scenarios.budget_handoff:build")
@pytest.mark.parametrize("role", ["goal supervisor", "dynamic workflow orchestrator"])
def test_usage_reminder_preserves_handoff_across_late_checkpoint_and_restart(
    spawned_agent: int, budget_world: Path, role: str
) -> None:
    owner = spawned_agent
    root = budget_world
    (root / "role").write_text(role)
    peers: list[int] = []
    observer: subprocess.Popen[str] | None = None
    try:
        chat_and_wait(owner, f"Act as a {role}; start one unit and preserve progress.")
        peers = _state(root, owner)["peers"]
        child = peers[0]
        poll_until(
            lambda: ((root / f"{child}.json").exists(), child),
            timeout=60,
            interval=0.3,
            what="spawned peer saves its artifact",
        )
        wait_for_status(child, "idling")
        # Begin polling before the fork exists; it must be rediscovered on a
        # later poll rather than frozen into the observer's initial ID set.
        env = os.environ.copy()
        env["AVA_AGENT_ID"] = str(owner)
        report_path = root / "usage.jsonl"
        with report_path.open("w") as output:
            observer = subprocess.Popen(  # noqa: S603 — fixed interpreter and repository script
                [
                    sys.executable,
                    str(_OBSERVER),
                    "--agent-id",
                    str(owner),
                    "--lineage",
                    "all",
                    "--lifetime",
                    "--token-limit",
                    "75",
                    "--notify-agent",
                    str(owner),
                    "--poll-seconds",
                    "0.3",
                ],
                env=env,
                stdout=output,
                stderr=subprocess.PIPE,
                text=True,
            )
        poll_until(
            lambda: (bool(report_path.read_text().splitlines()), report_path.read_text()),
            timeout=30,
            interval=0.3,
            what="first observer report",
        )
        first = json.loads(report_path.read_text().splitlines()[0])
        assert {a["agent_id"] for a in first["agents"]} == {owner, child}
        assert first["totals"]["total_tokens"] < 75
        fork = httpx.post(
            f"{GATEWAY_URL}/api/agents",
            json={
                "spawner": f"agent:{owner}",
                "fork_from": child,
            },
            timeout=90,
        )
        fork.raise_for_status()
        peers.append(int(fork.json()["id"]))
        state = _state(root, owner)
        state["peers"] = peers
        write_text_atomic(root / f"{owner}.json", json.dumps(state))
        httpx.post(
            f"{GATEWAY_URL}/api/agents/{peers[-1]}/messages",
            json={
                "content": "Preserve another partial result and wait.",
                "source": f"agent:{owner}",
            },
            timeout=10,
        ).raise_for_status()
        _, err = observer.communicate(timeout=90)
        assert observer.returncode == 0, err
        count = _assert_handoffs(owner, peers, root, report_path)
        _assert_recovery(owner, root, count)
    finally:
        if observer is not None and observer.poll() is None:
            observer.terminate()
            observer.communicate(timeout=10)
        cleanup_ids = set(peers)
        if (root / f"{owner}.json").exists():
            cleanup_ids.update(_state(root, owner)["peers"])
        for peer in cleanup_ids:
            _terminate(peer)


def _restart_applied(agent_id: int) -> tuple[bool, object]:
    with psycopg.connect(settings.data_plane.db_url) as conn:
        row = conn.execute(
            "SELECT status FROM inbound_messages WHERE agent_id = %s AND kind = 'restart' "
            "ORDER BY id DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
    return row == ("done",), row


def _assert_recovery(owner: int, root: Path, count: tuple[Any, ...] | None) -> None:
    handoff = (root / f"{owner}.json").read_text()
    calls_before = [len(model_inputs(peer)) for peer in _state(root, owner)["peers"]]
    chat_and_wait(owner, "Late checkpoint: a partial result landed; inspect the handoff.")
    assert (root / f"{owner}.recovered").exists()
    assert (root / f"{owner}.json").read_text() == handoff
    httpx.post(f"{GATEWAY_URL}/api/agents/{owner}/restart", timeout=30).raise_for_status()
    poll_until(
        lambda: _restart_applied(owner),
        timeout=60,
        interval=0.3,
        what="restart applied",
    )
    (root / f"{owner}.recovered").unlink()
    chat_and_wait(owner, "Recover from restart and check the recorded resume condition.")
    assert (root / f"{owner}.recovered").exists()
    assert (root / f"{owner}.json").read_text() == handoff
    assert checkpoint_values(owner)["messages"]
    assert [len(model_inputs(peer)) for peer in _state(root, owner)["peers"]] == calls_before
    with psycopg.connect(settings.data_plane.db_url) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agents_meta").fetchone() == count


def _assert_handoffs(
    owner: int, peers: list[int], root: Path, report_path: Path
) -> tuple[Any, ...] | None:
    report = json.loads(report_path.read_text().splitlines()[-1])
    assert {a["agent_id"] for a in report["agents"]} == {owner, *peers}
    assert report["totals"]["total_tokens"] >= 75
    for aid in [owner, *peers]:
        poll_until(
            lambda aid=aid: _paused(root, aid),
            timeout=90,
            interval=0.3,
            what=f"peer {aid} preserves a budget handoff",
        )
        wait_for_status(aid, "idling")
        saved = _state(root, aid)
        assert saved["goal_met"] is False
        assert saved["remaining"] and saved["resume_condition"]
        assert all(Path(p).read_text().startswith("Verified partial") for p in saved["artifacts"])
    assert any(
        "Usage budget reminder:" in m["text"]
        for call in model_inputs(owner)
        for m in call
        if m["type"] == "human"
    )
    with psycopg.connect(settings.data_plane.db_url) as conn:
        count = conn.execute("SELECT COUNT(*) FROM agents_meta").fetchone()
        assert conn.execute(
            "SELECT COUNT(*) FROM inbound_messages WHERE kind = 'terminate' AND agent_id = ANY(%s)",
            ([owner, *peers],),
        ).fetchone() == (0,)
    return count
