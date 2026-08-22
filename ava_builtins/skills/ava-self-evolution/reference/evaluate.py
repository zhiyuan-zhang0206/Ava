"""Evaluate a skill by re-running its tasks with fresh agents and scoring them.

This is the measurement half of the optimization loop (see the Evaluation Loop
section of SKILL.md). Given a skill and a set of that skill's dataset tasks, it
spawns a fresh agent per task (with `ava.agents.spawn`, so the agent picks up
the current skill text), lets each run the task, then scores the resulting
trace with `rubric.py`. Comparing the mean score before vs after a skill edit
is the signal that tells you whether the edit helped.

Two-phase, because a task run takes minutes and a single execute_code block is
capped well below that. So you cannot spawn and block for the result in one
turn:

    state = launch(skill, tasks)      # spawns fresh agents, returns immediately
    # ... let them run: watch until poll(state) shows none pending ...
    report = gather(state)            # collect + score each finished agent

`launch` must run inside an agent process (it spawns). `poll` / `gather` only
read the database, so they work from anywhere.

SAFETY: only tasks that are safe to re-run are spawned — spawning replays the
task for real, so a task that sent a message or hit an external API is skipped
(the `is_replay_safe` gate below). Do not evaluate on side-effecting
tasks.

Import it from an agent's code (the skill directory name has a hyphen, so add
the reference directory to sys.path and `import evaluate`), not as a CLI —
`launch` needs a live agent identity.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# PYTHONSAFEPATH=1 keeps the script's own directory off sys.path — restore
# it for the sibling import (the reference dir is a script dir, not a package).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: PTH100, PTH120
from collect import collect_one
from rubric import scores

import ava
from shared.db import connect
from shared.paths import ava_home

# Tool-call prefixes that make a run unsafe to re-run: fleet/user-facing side
# effects, process lifecycle, arbitrary OS (shell can rm / curl / git push),
# scheduling, and real file writes. A run is replay-safe iff NONE of its
# tools_called match these — leaving pure read/compute tasks.
UNSAFE_PREFIXES = (
    "ava.agents.",
    "ava.ui.",
    "ava.self.",
    "ava.shell.",
    "ava.watcher.",
    "ava.files.edit",
    "ava.files.write",
    "ava.files.delete",
    "ava.mcps.",
)


def is_replay_safe(rec: dict[str, Any]) -> tuple[bool, str]:
    """Return (safe, reason). A run is safe to re-run only if every tool it
    called is pure read/compute and it actually carries a task prompt."""
    if not rec["task_prompt"].strip():
        return False, "no task prompt"
    hits = sorted({tool for tool in rec["tools_called"] if tool.startswith(UNSAFE_PREFIXES)})
    if hits:
        return False, "side-effect tools: " + ", ".join(hits)
    return True, "pure read/compute"


# A spawned eval agent is finished once it stops working: it either idled after
# delivering, or terminated.
DONE_STATUSES = ("idling", "terminated")


def _eval_dir() -> Path:
    d = ava_home() / "self_evolution" / "eval"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(skill: str, stamp: str) -> Path:
    safe = skill.replace("/", "_").replace(":", "_")
    return _eval_dir() / f"{safe}-{stamp}.json"


def launch(skill: str, tasks: list[dict[str, Any]], *, model: str | None = None) -> dict[str, Any]:
    """Spawn one fresh agent per replay-safe task to re-run it under the current
    skill text. Returns (and persists) the eval state; pass it to `gather` once
    the agents finish. Must run inside an agent process."""
    import ava

    runs, skipped = [], []
    for task in tasks:
        safe, why = is_replay_safe(task)
        if not safe:
            skipped.append({"source_agent_id": task["agent_id"], "reason": why})
            continue
        overlay: dict[str, object] = {"eval_isolation": True}
        if model:
            overlay["llm_model"] = model
        eval_id = ava.agents.spawn(prompt=task["task_prompt"], config_overlay=overlay)
        runs.append(
            {
                "eval_agent_id": eval_id,
                "source_agent_id": task["agent_id"],
                "prompt": task["task_prompt"],
            }
        )
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    state = {"skill": skill, "stamp": stamp, "runs": runs, "skipped": skipped}
    path = _state_path(skill, stamp)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    state["path"] = str(path)
    return state


def _status_of(agent_ids: list[int]) -> dict[int, str]:
    if not agent_ids:
        return {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, status FROM agents_meta WHERE id = ANY(%s)", [agent_ids])
        return {r[0]: r[1] for r in cur.fetchall()}


def poll(state: dict[str, Any]) -> dict[str, list[int]]:
    """Split the eval agents into done vs still-pending by their current status.
    Call each turn until `pending` is empty, then `gather`."""
    ids = [r["eval_agent_id"] for r in state["runs"]]
    status = _status_of(ids)
    done = [i for i in ids if status.get(i) in DONE_STATUSES]
    pending = [i for i in ids if i not in done]
    return {"done": done, "pending": pending}


def _mean(score_dicts: list[dict[str, float]]) -> dict[str, float]:
    if not score_dicts:
        return {"completion": 0.0, "efficiency": 0.0, "overall": 0.0}
    keys = ("completion", "efficiency", "overall")
    return {k: round(sum(s[k] for s in score_dicts) / len(score_dicts), 3) for k in keys}


def gather(state: dict[str, Any]) -> dict[str, Any]:
    """Collect + score every finished eval agent. Agents still running are
    listed under `pending` and left out of the mean."""
    progress = poll(state)
    per_task = []
    for run in state["runs"]:
        if run["eval_agent_id"] not in progress["done"]:
            continue
        rec = collect_one(run["eval_agent_id"])
        per_task.append(
            {
                "eval_agent_id": run["eval_agent_id"],
                "source_agent_id": run["source_agent_id"],
                "label": rec["label"],
                "scores": scores(rec),
            }
        )
    return {
        "skill": state["skill"],
        "stamp": state["stamp"],
        "n": len(per_task),
        "mean": _mean([t["scores"] for t in per_task]),
        "per_task": per_task,
        "pending": progress["pending"],
        "skipped": state["skipped"],
    }


def latest_state(skill: str) -> dict[str, Any]:
    """Reload the most recent eval state for a skill (state does not survive
    across turns in memory, so `gather` reloads it from disk)."""
    safe = skill.replace("/", "_").replace(":", "_")
    files = sorted(_eval_dir().glob(f"{safe}-*.json"))
    if not files:
        raise FileNotFoundError(f"no eval state for skill {skill!r} under {_eval_dir()}")
    return json.loads(files[-1].read_text(encoding="utf-8"))


# ─────────────── debrief (the backward pass) ───────────────


def debrief(state: dict, skill_name: str) -> list[dict]:
    """Ask each eval agent that just ran a task: "what would you change?"

    This is the backward pass of the evaluation loop — the agent that actually
    did the work has first-hand experience of what the skill got wrong.  Its
    self-reflection is 80% of the gradient signal; trace-mining by a separate
    worker is the other 20%.

    Returns one record per debriefed agent: {eval_agent_id, prompt, debrief_sent}.
    """
    results: list[dict] = []
    status = _status_of([entry["eval_agent_id"] for entry in state["runs"]])
    for entry in state["runs"]:
        eval_agent_id = entry["eval_agent_id"]
        if status.get(eval_agent_id) not in DONE_STATUSES:
            continue
        try:
            ava.agents.send_message(
                eval_agent_id,
                f"You just completed a task using the skill '{skill_name}'. "
                "Based on your experience running this task, what specific "
                "changes to the skill instructions would have helped you "
                "complete it better, faster, or with fewer errors? "
                "Be concrete: point to steps, prompt wording, missing context, "
                "or ambiguous instructions. Reply in 3-5 lines.",
            )
        except Exception:
            continue
        entry["debrief_sent"] = True
        results.append(
            {
                "eval_agent_id": eval_agent_id,
                "prompt": entry["prompt"],
                "debrief_sent": True,
            }
        )
    return results


def collect_debriefs(state: dict) -> list[dict]:
    """Read each debriefed eval agent's last message and pair it with the task."""
    results: list[dict] = []
    for entry in state["runs"]:
        if not entry.get("debrief_sent"):
            continue
        try:
            msg = ava.agents.get_last_message(entry["eval_agent_id"])
        except Exception:
            msg = None
        results.append(
            {
                "eval_agent_id": entry["eval_agent_id"],
                "prompt": entry["prompt"],
                "reflection": msg,
            }
        )
    return results
