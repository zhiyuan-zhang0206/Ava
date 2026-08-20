"""Pre-compact history dump — JSONL snapshot of the full conversation (opt-in).

When ``turn_settings.agent.history_dump_enabled`` is on, every compaction path —
the claim node's agent-/user-triggered compact (``_claim_decide``) and the
before_llm auto-compact hook (``agent/hooks/compact.py``) — writes the complete
pre-compact ``state.messages`` to a JSONL file under the agent workspace
(``<workspace>/compact_dumps/<timestamp>.jsonl``) and injects a system note in
the fresh post-compact context pointing at the dump.

This is a forensics / trace-replay aid, not a retention mechanism: the summary
is still the only memory that survives a compaction. Off by default; enable
per cluster or per agent when a conversation needs auditability.

Wire format: one LangChain BaseMessage ``model_dump(mode="json")`` per line —
the same raw shape the gateway messages API serves (type / content /
tool_calls / id / additional_kwargs / ...). Replay recipe, per line:
``messages_from_dict([{"type": raw["type"], "data": {k: v for k, v in
raw.items() if k != "type"}}])`` (the ``type``/``data`` split is the
langchain message envelope). Rotation bounds disk growth: after each write
only the newest ``turn_settings.agent.history_dump_keep`` files are kept.

Injection-safety: the note is never merged into the live ``messages`` channel
— it rides ``context_reset.tail`` (via ``build_compact_transition``), which
``init_context`` lays down in the fresh context after the standing head and
the summary. The pre-compact channel is wiped with REMOVE_ALL, so the note
can never sit between an AIMessage and its ToolMessage (DeepSeek
anthropic-compatible endpoint rejects that shape with a 400 — see
``ava/design/injection-in-memory-exec-delta-20260811``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.messages import AnyMessage, HumanMessage

from agent.messages import NoteTag, system_note_message
from shared.config.turn_view import turn_settings
from shared.log import logger
from shared.paths import workspace_dir

# Subdirectory of the agent workspace holding the dumps.
_DUMP_DIRNAME = "compact_dumps"


def history_dump_dir(agent_id: int) -> Path:
    """The per-agent dump directory (``<workspace>/compact_dumps``), created on
    first use. Exported for tests; callers go through ``dump_history``."""
    d = workspace_dir(agent_id) / _DUMP_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def dump_history(messages: list[AnyMessage], agent_id: int) -> Path | None:
    """Write the full pre-compact conversation to a JSONL file; return its path.

    Returns ``None`` when the dump is disabled (``history_dump_enabled`` off —
    the default) or the write failed. Never raises: a dump is best-effort
    forensics and must not abort the compaction itself — the summary remains
    the only memory that survives. After a successful write, rotates the dump
    directory down to the newest ``history_dump_keep`` files.

    Each line is one message's ``model_dump(mode="json")`` (raw LangChain
    fields, same shape as GET /api/agents/{id}/messages); the replay recipe is
    in the module docstring.
    """
    if not turn_settings.agent.history_dump_enabled:
        return None
    try:
        d = history_dump_dir(agent_id)
        # Microsecond precision: two compactions within one second must not
        # overwrite each other (the name is also the sort key for rotation).
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        path = d / f"{stamp}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg.model_dump(mode="json")) + "\n")
        _rotate(d, max(1, turn_settings.agent.history_dump_keep))
        logger.info(
            "[{label}] {body}",
            label="history-dump",
            event="history_dump",
            body=f"dumped {len(messages)} messages to {path}",
        )
        return path
    except Exception as exc:
        logger.warning(
            "[{label}] {body}",
            label="history-dump",
            event="history_dump",
            body=f"dump failed for agent {agent_id}: {exc!r}",
        )
        return None


def _rotate(d: Path, keep: int) -> None:
    """Delete all but the ``keep`` newest ``*.jsonl`` files in ``d``.

    The timestamp filenames sort lexicographically in chronological order, so
    the newest are the largest names. Best-effort: a rotation failure is
    logged, not raised (the dump itself already succeeded).
    """
    try:
        dumps = sorted(d.glob("*.jsonl"))
        for stale in dumps[:-keep] if keep > 0 else dumps:
            stale.unlink()
    except Exception as exc:
        logger.warning(
            "[{label}] {body}",
            label="history-dump",
            event="history_dump",
            body=f"rotation failed in {d}: {exc!r}",
        )


def history_dump_note(path: Path) -> HumanMessage:
    """The system note telling the fresh post-compact context where the dump is.

    Only ever injected when the dump actually succeeded, and only into the new
    context (``context_reset.tail``), never into the pre-compact ``messages``
    channel — see the module docstring for the adjacency rationale.
    """
    return system_note_message(
        content=(
            f"Your pre-compact conversation history was dumped to {path} "
            f"(JSONL, one message per line) for forensics / replay. The summary "
            f"above remains your only memory."
        ),
        tag=NoteTag.HISTORY_DUMP,
        created_at=datetime.now(UTC),
    )
