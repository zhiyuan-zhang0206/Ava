"""Disk-backed IM Bridge switch state and inbound outbox."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter

from base.paths import ava_home

_log = logging.getLogger("services.entrypoints.im_bridge.state")


def _switch_state_path() -> Path:
    """Per-chat switch persistence — survives daemon restarts/updates."""

    path = ava_home() / "state" / "im_bridge" / "switch_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_switch_state() -> dict[str, int]:
    """Restore {channel}:{chat_id} -> agent_id from disk; {} when absent."""

    path = _switch_state_path()
    if not path.exists():
        return {}
    return TypeAdapter(dict[str, int]).validate_json(path.read_text(encoding="utf-8"), strict=True)


def _save_switch_state(state: dict[str, int]) -> None:
    """Persist atomically so a crash never leaves a half-written file."""

    path = _switch_state_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# -- inbound outbox (Task #1032) -------------------------------------------
#
# A user message whose gateway enqueue fails after every retry is persisted
# here instead of dropped: the platform offset has already moved, so the
# message cannot be re-delivered by the adapter — AtLeastOnce means the
# bridge keeps it. A background replay loop drains the file with backoff;
# the Idempotency-Key survives the outbox, so even a replay after a lost
# gateway response cannot duplicate the message server-side.


class _OutboxEntry(BaseModel):
    """One pending user message awaiting gateway delivery."""

    model_config = ConfigDict(strict=True, extra="forbid")

    id: str
    channel: str
    chat_id: str
    agent_id: int
    text: str
    idempotency_key: str
    enqueued_at: float


def _outbox_path() -> Path:
    """Pending-inbound persistence — survives daemon restarts/updates."""

    path = ava_home() / "state" / "im_bridge" / "outbox.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_outbox() -> list[_OutboxEntry]:
    """Restore pending entries; an existing invalid journal fails without rewriting it."""

    path = _outbox_path()
    if not path.exists():
        return []
    entries: list[_OutboxEntry] = []
    # Split on "\n" only - str.splitlines() also breaks on U+0085 /
    # U+2028 / U+2029, which are legal unescaped inside a JSON string
    # (the writer emits them raw); a split there truncates the line and
    # the entry is dropped.
    for line in path.read_text(encoding="utf-8").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        data = json.loads(line)
        entries.append(_OutboxEntry.model_validate(data))
    return entries


def _save_outbox(entries: list[_OutboxEntry]) -> None:
    """Rewrite the whole file atomically — pending count is small, so the
    full-rewrite keeps ordering and avoids partial-line reads."""

    path = _outbox_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        "".join(json.dumps(e.model_dump(mode="json"), ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )
    tmp.replace(path)
