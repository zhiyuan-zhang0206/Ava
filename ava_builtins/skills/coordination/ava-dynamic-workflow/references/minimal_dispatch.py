"""Small, single-writer dispatch example with durable receipts and no retry loop.

Call run(handoff, tasks) from an Ava agent. Each task has an id and a
self-contained prompt including its input. Start with one representative task,
then add peers if useful. The caller validates domain results and chooses when
to collect, wait, or pause. This example does not launch a watcher or enforce
budgets. A changed task needs a new run directory.

Dispatch intent and the spawn receipt cannot be committed atomically. An
interruption between them leaves an ambiguous intent: stop and reconcile it
with actual peers instead of automatically spawning again. Serialize callers.
"""

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

import ava


def _save(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def _result(path: Path, task_id: str, fingerprint: str) -> dict | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("task_id") != task_id
        or value.get("input_hash") != fingerprint
        or not isinstance(value.get("result"), dict)
    ):
        raise ValueError(f"invalid or stale result: {path}; reconcile before continuing")
    return value["result"]


def run(handoff: Path, tasks: list[dict[str, str]]) -> dict:
    """Dispatch new units once; return results and known pending peer IDs.

    Re-entry preserves matching results and never redispatches a known peer.
    Failed or ambiguous spawn attempts propagate before any waiting. Recovery
    and retries are decisions for the calling agent, not automatic defaults.
    """
    if not tasks:
        raise ValueError("configure at least one task")
    handoff.mkdir(parents=True, exist_ok=True)
    state_path = handoff / "dispatch.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    prepared, results, pending = [], {}, {}
    seen = set()
    # Validate the whole batch before creating any peer.
    for task in tasks:
        task_id, prompt = task["id"], task["prompt"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id) or task_id in seen or not prompt.strip():
            raise ValueError("task IDs must be unique slugs and prompts must be nonempty")
        seen.add(task_id)
        fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        record = state.get(task_id)
        if record is not None:
            if record["input_hash"] != fingerprint:
                raise ValueError(f"task {task_id} changed; use a new run directory")
            if record["phase"] not in ("dispatching", "assigned"):
                raise ValueError(f"unknown dispatch phase for {task_id}")
        path = handoff / f"{task_id}.json"
        result = _result(path, task_id, fingerprint)
        if result is not None:
            results[task_id] = result
        elif record is not None:
            if record["phase"] == "dispatching":
                raise RuntimeError(
                    f"ambiguous dispatch for {task_id}; reconcile actual peers first"
                )
            pending[task_id] = record["agent_id"]
        else:
            prepared.append((task_id, prompt, fingerprint, path))
    for task_id, prompt, fingerprint, path in prepared:
        state[task_id] = {"input_hash": fingerprint, "phase": "dispatching"}
        _save(state_path, state)
        envelope = {"task_id": task_id, "input_hash": fingerprint, "result": {}}
        peer_id = ava.agents.spawn(
            prompt=(
                f"{prompt}\n\nWrite your result atomically to {path}: write a temporary file "
                "and replace the target when complete. Use this JSON envelope, filling result "
                f"with your domain output: {json.dumps(envelope)}. "
                "The file is the routine handoff; report blockers or budget decisions directly "
                "to the responsible peer instead of making partial output look complete. "
                "Choose whether to idle or end yourself after delivery based on follow-up needs."
            ),
            idempotency_key=str(uuid4()),
        )
        state[task_id] = {"input_hash": fingerprint, "phase": "assigned", "agent_id": int(peer_id)}
        _save(state_path, state)
        pending[task_id] = int(peer_id)
    return {"results": results, "pending": pending}
