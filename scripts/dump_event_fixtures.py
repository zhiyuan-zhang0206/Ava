"""Dump one sample JSON fixture per SystemEvent — used by Python and TS wire
roundtrip equivalence tests.

Each fixture file = one JSON instance of an event class (all required
fields filled with reasonable sample values, field order follows
`_Base` -> subclass declaration order). Filename = role string (e.g.
`chat_start.json`, `exec_output_chunk.json`).

When the backend adds a new required field to an event class:
- The Python side's Pydantic strict validation will fail on old fixtures (missing field)
- Fixtures must be updated (re-run this script) to commit
- The TS side's wire test per-role required-field list must also be updated

When the frontend (TS) adds a new field without backend syncing: pyright
will flag the fixture for type mismatch, or the fixture will be missing
the field.

Usage:
    .venv/bin/python scripts/dump_event_fixtures.py [out_dir]

Default out_dir = `tests/fixtures/events/`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Put project root on sys.path so `from shared.live_events import ...` finds the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _samples() -> dict[str, dict]:
    """One sample dict per event — field values only need to be type- and
    business-correct, not real.

    When adding a new event class to the Event union, you must add a
    sample here; otherwise dump_event_fixtures will skip it and the
    Python wire test will not catch it."""
    agent_id = 7
    return {
        "chat_start": {"role": "chat_start", "agent_id": agent_id, "item_id": "5.0"},
        "chat_delta": {
            "role": "chat_delta",
            "agent_id": agent_id,
            "item_id": "5.0",
            "content": "hello",
        },
        "code_start": {"role": "code_start", "agent_id": agent_id, "item_id": "5.1"},
        "code_delta": {
            "role": "code_delta",
            "agent_id": agent_id,
            "item_id": "5.1",
            "content": "print(1)",
        },
        "reasoning_start": {
            "role": "reasoning_start",
            "agent_id": agent_id,
            "item_id": "5.0",
        },
        "reasoning_delta": {
            "role": "reasoning_delta",
            "agent_id": agent_id,
            "item_id": "5.0",
            "content": "let me think",
        },
        "exec_start": {"role": "exec_start", "agent_id": agent_id, "item_id": "6.0"},
        "exec_output_chunk": {
            "role": "exec_output_chunk",
            "agent_id": agent_id,
            "item_id": "6.0",
            "content": "stdout chunk\n",
        },
        "exec_output": {
            "role": "exec_output",
            "agent_id": agent_id,
            "item_id": "6.0",
            "content": "Code execution output:\n\nstdout chunk\n",
        },
        "compact_request": {
            "role": "compact_request",
            "agent_id": agent_id,
            "content": "[compact requested, 5 chars]",
        },
        "compact_done": {"role": "compact_done", "agent_id": agent_id},
        "error": {
            "role": "error",
            "agent_id": agent_id,
            "content": "something broke",
        },
        "cancelled": {"role": "cancelled", "agent_id": agent_id},
        "inbound_arrived": {
            "role": "inbound_arrived",
            "agent_id": agent_id,
            "inbound_id": 42,
            "kind": "chat",
            "source": "user",
            "content": "hello agent",
        },
        "inbound_committed": {
            "role": "inbound_committed",
            "agent_id": agent_id,
            "inbound_id": 42,
        },
        "page_opened": {
            "role": "page_opened",
            "agent_id": agent_id,
            "page_id": 3,
            "name": "cleanup",
            "port": 8765,
            "title": "Cleanup picker",
            "url": f"http://localhost:8000/api/pages/{agent_id}-cleanup/",
        },
        "page_closed": {
            "role": "page_closed",
            "agent_id": agent_id,
            "name": "cleanup",
        },
        "token_usage": {
            "role": "token_usage",
            "agent_id": agent_id,
            "input_tokens": 1234,
            "output_tokens": 56,
            "reasoning_tokens": 0,
        },
        "llm_done": {"role": "llm_done", "agent_id": agent_id},
        "timeline_snapshot": {
            "role": "timeline_snapshot",
            "agent_id": agent_id,
            "items": [
                {
                    "item_id": "1.0",
                    "kind": "inbound_chat",
                    "payload": "test",
                    "source": None,
                    "created_at": "2026-01-01T00:00:00Z",
                    "inbound_id": None,
                }
            ],
            "msg_count": 2,
        },
        "agent_spawned": {
            "role": "agent_spawned",
            "agent_id": agent_id,
            "snapshot": {
                "agent_id": agent_id,
                "spawner": "user",
                "fork_source_agent_id": None,
                "fork_source_checkpoint_id": None,
                "status": "idling",
                "pid": None,
                "spawned_at": "2026-01-01T00:00:00Z",
                "started_at": None,
                "last_active_at": "2026-01-01T00:00:00Z",
                "last_inbound_at": "2026-01-01T00:00:00Z",
                "label": None,
                "machine": "test-host",
                "supports_vision": True,
                "notices_awaiting_response": [],
                "unread_notice_count": 0,
                "heartbeat_paused_until": None,
                "liveness_state": "online",
                "last_probe_at": None,
            },
        },
        "agent_updated": {
            "role": "agent_updated",
            "agent_id": agent_id,
            "snapshot": {
                "agent_id": agent_id,
                "spawner": "user",
                "fork_source_agent_id": None,
                "fork_source_checkpoint_id": None,
                "status": "running",
                "pid": 12345,
                "spawned_at": "2026-01-01T00:00:00Z",
                "started_at": "2026-01-01T00:00:05Z",
                "last_active_at": "2026-01-01T00:00:10Z",
                "last_inbound_at": "2026-01-01T00:00:07Z",
                "label": "demo",
                "machine": "test-host",
                "supports_vision": True,
                "notices_awaiting_response": [],
                "unread_notice_count": 0,
                "heartbeat_paused_until": None,
                "liveness_state": "online",
                "last_probe_at": None,
            },
        },
        "notice_posted": {
            "role": "notice_posted",
            "agent_id": agent_id,
            "notice_id": 7,
            "priority": "P1",
            "title": "deploy finished",
            "task_id": 3,
        },
        "notice_resolved": {
            "role": "notice_resolved",
            "agent_id": agent_id,
            "notice_id": 7,
        },
        "task_created": {
            "role": "task_created",
            "agent_id": agent_id,
            "task_id": 3,
        },
        "task_updated": {
            "role": "task_updated",
            "agent_id": agent_id,
            "task_id": 3,
        },
    }


def main() -> int:
    from shared.live_events import EVENT_ADAPTER

    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/events")
    out_dir.mkdir(parents=True, exist_ok=True)

    samples = _samples()
    written = 0

    # Pydantic validation: each sample must parse via EVENT_ADAPTER (verify
    # the role field matches + all required fields are given + types
    # match). Validate first, then dump, so the produced JSON is the
    # Pydantic-canonical form (e.g. field order, null vs missing).
    for filename, sample in samples.items():
        validated = EVENT_ADAPTER.validate_python(sample)
        out_path = out_dir / f"{filename}.json"
        out_path.write_text(validated.model_dump_json(indent=2) + "\n", encoding="utf-8")
        written += 1

    print(f"wrote {written} event fixtures -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
