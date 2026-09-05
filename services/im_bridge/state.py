"""Disk-backed IM Bridge switch state and inbound outbox."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from shared.config import settings

_log = logging.getLogger("services.im_bridge.state")


def _switch_state_path() -> Path:
    """Per-chat switch persistence — survives daemon restarts/updates."""

    path = Path(settings.general.ava_home) / "state" / "im_bridge" / "switch_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_switch_state() -> dict[str, int]:
    """Restore {channel}:{chat_id} -> agent_id from disk; {} when absent."""

    path = _switch_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: int(v) for k, v in data.items() if isinstance(v, int) or str(v).isdigit()}


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


@dataclass
class _OutboxEntry:
    """One pending user message awaiting gateway delivery."""

    id: str
    channel: str
    chat_id: str
    agent_id: int
    text: str
    idempotency_key: str
    enqueued_at: float


def _outbox_path() -> Path:
    """Pending-inbound persistence — survives daemon restarts/updates."""

    path = Path(settings.general.ava_home) / "state" / "im_bridge" / "outbox.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_outbox() -> list[_OutboxEntry]:
    """Restore pending entries; malformed trailing lines (crash during an
    append) are skipped, never fatal."""

    path = _outbox_path()
    if not path.exists():
        return []
    entries: list[_OutboxEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(line)
            entries.append(
                _OutboxEntry(
                    id=data["id"],
                    channel=data["channel"],
                    chat_id=data["chat_id"],
                    agent_id=int(data["agent_id"]),
                    text=data["text"],
                    idempotency_key=data["idempotency_key"],
                    enqueued_at=float(data["enqueued_at"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            _log.warning("im_bridge: skipping malformed outbox line: %.120s", line)
    return entries


def _save_outbox(entries: list[_OutboxEntry]) -> None:
    """Rewrite the whole file atomically — pending count is small, so the
    full-rewrite keeps ordering and avoids partial-line reads."""

    path = _outbox_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        "".join(json.dumps(e.__dict__, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )
    tmp.replace(path)
