"""Completion-notice policy decisions and hourly digest rendering."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, cast


class CompletionNoticePolicy(StrEnum):
    """How admitted platform completions reach an agent."""

    ALL = "all"
    HOURLY = "hourly"


_MAX_DIGEST_LOGS = 20


@dataclass(frozen=True)
class CompletionNotice:
    """One platform-generated completion notification (no outcome is recorded)."""

    source: str
    content: str

    @property
    def summary(self) -> str:
        """The completion line without its optional output-tail rider."""
        return self.content.split("\n\n", maxsplit=1)[0]


@dataclass(frozen=True)
class CompletionDigest:
    """One agent's undelivered notices for a completed UTC hour."""

    agent_id: int
    window_start: datetime
    event_ids: list[int]
    notices: list[CompletionNotice]


def validate_completion_notice_policy(value: str) -> CompletionNoticePolicy:
    """Return a supported policy or fail loudly on a stored invalid value."""
    try:
        return CompletionNoticePolicy(value)
    except ValueError as exc:
        raise ValueError(
            f"completion_notice_policy must be one of "
            f"{[policy.value for policy in CompletionNoticePolicy]!r}, got {value!r}"
        ) from exc


def current_default_completion_notice_policy() -> CompletionNoticePolicy:
    """Read the live cluster default independently of the process profile."""
    from base.config.service_read import current_field_values

    value = current_field_values()["completion_notice_policy"]
    if not isinstance(value, str):
        raise TypeError(f"completion_notice_policy must be a string, got {value!r}")
    return validate_completion_notice_policy(value)


def effective_completion_notice_policy(
    config_overlay: Mapping[str, object] | None, default: str
) -> CompletionNoticePolicy:
    """Resolve an agent's persisted override over the live cluster default."""
    value = (config_overlay or {}).get("completion_notice_policy", default)
    if not isinstance(value, str):
        raise TypeError(f"completion_notice_policy must be a string, got {value!r}")
    return validate_completion_notice_policy(value)


def immediate_delivery_required(policy: CompletionNoticePolicy, notice: CompletionNotice) -> bool:
    """Whether one completion notice must enter the agent inbox immediately."""
    del notice
    return policy is CompletionNoticePolicy.ALL


def policy_for_agent(conn: Any, agent_id: int, default: str) -> CompletionNoticePolicy:
    """Read the agent's live completion-notice policy from its config overlay."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
        row = cursor.fetchone()
    if row is None:
        raise LookupError(f"agent {agent_id} does not exist")
    overlay = row[0]
    if overlay is not None and not isinstance(overlay, Mapping):
        raise ValueError(f"agent {agent_id} config_overlay is not an object: {overlay!r}")
    return effective_completion_notice_policy(
        cast(Mapping[str, object] | None, overlay),
        default,
    )


def delivery_required_for_agent(
    conn: Any,
    agent_id: int,
    notice: CompletionNotice,
    default: str,
) -> bool:
    """Apply the one policy decision at the gateway delivery boundary.

    Hourly events are committed before an immediate failure notification is
    allowed through. This makes the event table the restart-safe digest buffer
    and the authoritative count source for the canary conservation check.
    """
    # An outbox replay must retain the policy decision made by its first
    # successful gateway admission. For a notice buffered under hourly, a
    # later config flip to `all` must not turn the replay into a second direct
    # inbox message. Sources are session-scoped, so the event identity is
    # stable for that replay.
    if hourly_notice_recorded(conn, agent_id, notice):
        return False
    policy = policy_for_agent(conn, agent_id, default)
    if policy is CompletionNoticePolicy.HOURLY:
        record_hourly_notice(conn, agent_id, notice)
    return immediate_delivery_required(policy, notice)


def hourly_notice_recorded(conn: Any, agent_id: int, notice: CompletionNotice) -> bool:
    """Whether this exact event was already admitted to the digest."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM completion_notice_events WHERE agent_id = %s AND source = %s",
            (agent_id, notice.source),
        )
        return cursor.fetchone() is not None


def format_digest(*, agent_id: int, window_start: datetime, notices: list[CompletionNotice]) -> str:
    """Render one stable, source-marked hourly digest for a non-empty window."""
    if not notices:
        raise ValueError("cannot format an empty completion-notice digest")
    timestamp = window_start.isoformat()
    lines = [
        f"[completion-digest] Agent {agent_id}, hour starting {timestamp}: "
        f"{len(notices)} completion notices.",
        f"Logs (latest {_MAX_DIGEST_LOGS}):",
    ]
    lines.extend(f"- {notice.summary}" for notice in notices[-_MAX_DIGEST_LOGS:])
    omitted = len(notices) - _MAX_DIGEST_LOGS
    if omitted > 0:
        lines.append(f"- {omitted} older completion(s) omitted")
    return "\n".join(lines)


def record_hourly_notice(conn: Any, agent_id: int, notice: CompletionNotice) -> None:
    """Persist one hourly-policy event before either immediate or digest delivery."""
    with conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO completion_notice_events "
            "(agent_id, source, content) VALUES (%s, %s, %s) "
            "ON CONFLICT (agent_id, source) DO NOTHING",
            (agent_id, notice.source, notice.content),
        )


def pending_digests(conn: Any, now: datetime) -> list[CompletionDigest]:
    """Read every completed, non-empty hour that still needs its digest."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, agent_id, source, content, "
            "date_trunc('hour', created_at) "
            "FROM completion_notice_events "
            "WHERE digest_inbound_id IS NULL AND created_at < date_trunc('hour', %s) "
            "ORDER BY agent_id, date_trunc('hour', created_at), id",
            (now,),
        )
        rows = cursor.fetchall()
    grouped: dict[tuple[int, datetime], list[tuple[int, CompletionNotice]]] = defaultdict(list)
    for event_id, agent_id, source, content, window_start in rows:
        grouped[(int(agent_id), cast(datetime, window_start))].append(
            (int(event_id), CompletionNotice(source=str(source), content=str(content)))
        )
    return [
        CompletionDigest(
            agent_id=agent_id,
            window_start=window_start,
            event_ids=[event_id for event_id, _ in events],
            notices=[notice for _, notice in events],
        )
        for (agent_id, window_start), events in grouped.items()
    ]


def mark_digest_delivered(conn: Any, event_ids: list[int], inbound_id: int) -> None:
    """Attach one committed digest inbound to every event it summarizes."""
    with conn.cursor() as cursor:
        cursor.execute(
            "UPDATE completion_notice_events SET digest_inbound_id = %s "
            "WHERE id = ANY(%s) AND digest_inbound_id IS NULL",
            (inbound_id, event_ids),
        )


def prune_delivered_notices(conn: Any, before: datetime) -> int:
    """Delete delivered buffer rows once their canary inspection window closes."""
    with conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM completion_notice_events "
            "WHERE digest_inbound_id IS NOT NULL AND created_at < %s",
            (before,),
        )
        return cursor.rowcount
