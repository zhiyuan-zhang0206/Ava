"""Canonical id-free unified-event bytes shared by every sink."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any


def event_payload(event: Any) -> dict[str, Any]:
    """Return the persistent shape without its derived surrogate id."""
    return {
        "ts": event.ts.isoformat(),
        "trace_id": event.trace_id,
        "span_id": event.span_id,
        "agent_id": event.agent_id,
        "machine": event.machine,
        "cluster": event.cluster,
        "process": event.process,
        "category": event.category,
        "event_name": event.event_name,
        "level": event.level,
        "source": event.source,
        "target_agent_id": event.target_agent_id,
        "attributes": event.attributes,
    }


def event_line(event: Any) -> str:
    """Serialize the one byte representation used by JSONL, OTLP, and Loki."""
    return json.dumps(event_payload(event), default=str, separators=(",", ":"), ensure_ascii=False)


def event_line_digest(event: Any) -> str:
    """Return the full byte digest retained by producer receipts."""
    return sha256(event_line(event).encode()).hexdigest()
