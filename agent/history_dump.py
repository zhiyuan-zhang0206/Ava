"""Pre-compact history dump — JSONL snapshot of the full conversation.

Every compaction path — the claim node's agent-/user-triggered compact
(``_claim_decide``) and the before_llm auto-compact hook (``agent/hooks/compact.py``)
— writes the complete pre-compact ``state.messages`` to a JSONL file under the agent
workspace (``<workspace>/message-history/<start>__<end>.jsonl``) and injects a system
note in the fresh post-compact context pointing at the dump.

The filename carries two UTC stamps: ``start`` is the earliest message timestamp in
the dumped conversation (where the round began), ``end`` the compaction moment —
microsecond precision, so two dumps can never collide. The dump is the agent-side
retrieval aid behind "the compaction summary dropped a detail I need": grep it.
Enabled by default via ``turn_settings.agent.history_dump_enabled``; disable per
cluster or per agent to save disk. Each dump is
bounded by the context window itself and rotation keeps only the newest
``turn_settings.agent.history_dump_keep``, so the workspace cost is bounded by
``keep x context``.

Wire format: one LangChain BaseMessage ``model_dump(mode="json")`` per line —
the same raw shape the gateway messages API serves (type / content /
tool_calls / id / additional_kwargs / ...). Replay recipe, per line:
``messages_from_dict([{"type": raw["type"], "data": {k: v for k, v in
raw.items() if k != "type"}}])`` (the ``type``/``data`` split is the
langchain message envelope).

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
from shared.message_kwargs import read_ava_kwargs
from shared.paths import workspace_dir

# Subdirectory of the agent workspace holding the dumps.
_DUMP_DIRNAME = "message-history"


def history_dump_dir(agent_id: int) -> Path:
    """The per-agent dump directory (``<workspace>/message-history``), created on
    first use. Exported for tests; callers go through ``dump_history``."""
    d = workspace_dir(agent_id) / _DUMP_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _earliest_message_ts(messages: list[AnyMessage]) -> datetime | None:
    """The earliest ``ava_created_at`` across the dumped messages, or None.

    Every message shape but the SystemMessage carries the stamp (the
    ``agent/messages.py`` builders and the AIMessage stamp in
    ``agent/graph/_llm.py``), so this is the moment the round's context began.
    An unparseable value is skipped rather than sinking the whole dump; a
    stamp-less history falls back to the compaction moment at the caller.
    """
    earliest: datetime | None = None
    for msg in messages:
        raw = read_ava_kwargs(msg).get("ava_created_at")
        if raw is None:
            continue
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if earliest is None or ts < earliest:
            earliest = ts
    return earliest


def dump_history(messages: list[AnyMessage], agent_id: int) -> Path | None:
    """Write the full pre-compact conversation to a JSONL file; return its path.

    Returns ``None`` when the dump is disabled (``history_dump_enabled`` off —
    on by default) or the write failed. Never raises: a dump is best-effort and
    must not abort the compaction itself. After a successful write, rotates the
    dump directory down to the newest ``history_dump_keep`` files.

    The name is ``<start>__<end>.jsonl``, both UTC: start = the earliest
    message ``ava_created_at`` (second precision; falls back to the write
    moment when no stamp is parseable), end = the write moment (microsecond
    precision, so consecutive dumps can never share a name). Both stamps are
    fixed-width, and a dump's start is non-decreasing from one dump to the
    next, so lexical name order == chronological order.

    Each line is one message's ``model_dump(mode="json")`` (raw LangChain
    fields, same shape as GET /api/agents/{id}/messages); the replay recipe is
    in the module docstring.
    """
    if not turn_settings.agent.history_dump_enabled:
        return None
    try:
        d = history_dump_dir(agent_id)
        end = datetime.now(UTC)
        start = _earliest_message_ts(messages) or end
        path = d / f"{start:%Y%m%dT%H%M%SZ}__{end:%Y%m%dT%H%M%S.%fZ}.jsonl"
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

    The fixed-width UTC stamps keep name order == chronological order (see
    ``dump_history``), so the newest are the largest names. Best-effort: a
    rotation failure is logged, not raised (the dump itself already succeeded).
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
            f"(JSONL, one message per line). Grep it to recover details the "
            f"summary dropped; recent dumps are kept in the same folder."
        ),
        tag=NoteTag.HISTORY_DUMP,
        created_at=datetime.now(UTC),
    )


def workspace_section_hint() -> str:
    """The ``# Workspace`` system-prompt sentence: where the dumps live and the
    grep recipe for recovering details the compact summary dropped. Empty while
    the feature is off, so the section never points at a folder that stays
    empty.

    Lives beside the feature (not in ``agent/graph/_system_prompt.py``) so the
    folder name comes from ``_DUMP_DIRNAME`` and the gate from the same
    settings read as the dump itself; that module also sits at its line-budget
    ceiling, so the call site is one line.
    """
    if not turn_settings.agent.history_dump_enabled:
        return ""
    return (
        f" Pre-compact message history is dumped under `{_DUMP_DIRNAME}/` "
        "(JSONL, one message per line) — grep it to recover details lost to "
        f"compaction, e.g. `grep -rn 'keyword' {_DUMP_DIRNAME}/`."
    )
