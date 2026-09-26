"""ava_fleet plugin — an agent's whole surface to a human supervising the fleet.

Everything an agent shows a human is registered here, and all of it presumes a
human is watching, so disabling the plugin (declaring an unsupervised, fully
autonomous deployment) strips the whole surface at once. Two groups:

**Self-presentation** (`ava.self.*`, pull — the human scans the monitoring view):

1. **Label** `ava.self.set_label(text)` — the agent's role / name shown next to
   it, and stated in the agent's own agent-ID context note at each window
   establishment. A label the agent sets itself sticks (it is not replaced
   automatically afterwards).

**Notices to the user's queue** (`ava.ui.*`, push — the agent grabs triage):

Notices land in an aggregated, asynchronous queue the user checks later — they
are for when the user is not in a live conversation with the agent. In a live
dialog the user reads your replies as you write them, so answer directly and
do not post a notice.

2. **Post** `ava.ui.notify(title, content, require_response=..., blocking=...,
   priority=...)` — post one notice. `require_response=False` is an FYI the user
   may glance at or ignore; `require_response=True` needs an answer (and
   `blocking=True` if you are stalled until it arrives).
3. **Edit** `ava.ui.edit_notice(...)` — revise the notice you posted that the
   user has not acted on yet (at most one is open, so no id is needed).
4. **Dismiss** `ava.ui.dismiss_notice()` — withdraw the open notice that is no
   longer relevant, so the user does not spend time on a stale entry.

Each call publishes an update so the view live-refreshes. The plugin also bundles
the `ava_fleet` skill (the orchestration discipline). Disabled -> members absent
(`ava.ui` falls back to pages-only), no prompt, no skill, zero overhead.
"""

__description__: str = (
    "Fleet supervision surface — the agent self-reports its role/label and posts "
    "notices to the user's queue (FYI or needing a response), which it can edit "
    "or dismiss; the monitoring view shows all of it live."
)

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

import ava
import ava.agent_identity
import ava.agents
from ava.sdk_validation import coerce_str, coerce_typed
from shared.tasks.priority import validate_priority

from . import task_registry


def set_label(text: str) -> None:
    text = coerce_str(text, "text", allow_none=True)
    agent_id = ava.agent_identity.require_agent_id()
    with ava.DB.cursor() as cur:
        cur.execute(
            "UPDATE agents SET label=%s, label_user_set=TRUE WHERE id=%s",
            (text or None, agent_id),
        )
        from shared.audit_events import insert_event_log

        insert_event_log(
            event_type="label_change",
            agent_id=agent_id,
            source="self",
            payload={"new_label": text or None},
        )
    # Per-call import: plugin autoload stays off the redis/live-events stack (task #3816).
    from shared.live_announce import publish_agent_updated_sync

    publish_agent_updated_sync(agent_id)


# Sentinel for edit_notice: distinguishes "argument not passed" from an explicit
# None (clear the field). Using None for both would silently turn "leave content
# alone" into "erase content". Carries a repr because it renders as the default
# in the SDK stub the agent reads — a bare object() would print its memory
# address there.


class _Unset:
    def __repr__(self) -> str:
        return "<unchanged>"


_UNSET: object = _Unset()


# ── notice row shape (one `agent_notices` row, the pending summary) ──────────
# The lightweight summary notify() returns; a TypedDict, so an agent still
# reads it as a plain dict.


class PendingNoticeRow(TypedDict):
    """The lightweight summary row for one open notice (notify's pending list)."""

    id: int
    title: str
    created_at: datetime
    priority: str


class Notice(int):
    """The notice's id (an int, usable wherever a notice id is expected).
    `.pending_notices` / `.pending_count` are your still-open notices;
    `.superseded` lists the ids this call auto-resolved."""

    pending_count: int
    pending_notices: list[PendingNoticeRow]
    superseded: list[int]

    def __new__(
        cls,
        id: int,
        *,
        pending_count: int,
        pending_notices: list[PendingNoticeRow],
        superseded: list[int] | None = None,
    ) -> "Notice":
        instance = super().__new__(cls, id)
        instance.pending_count = pending_count
        instance.pending_notices = pending_notices
        instance.superseded = superseded or []
        return instance


def _raise_as_value_error(resp: Any) -> None:
    """Raise gateway 422 validation errors as ValueError — the SDK's
    validation contract (fail fast with a clear message). Any other error
    propagates through the normal wire contract."""
    from ava import _gateway_client

    try:
        _gateway_client._raise_from_response(resp)
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status != 422:
            raise
        detail = e.response.json().get("detail")  # type: ignore[attr-defined]
        raise ValueError(detail if isinstance(detail, str) else "invalid request") from e


def notify(
    title: str,
    content: str | None = None,
    *,
    require_response: bool = False,
    blocking: bool = False,
    priority: str = "P2",
    task: int | None = None,
    expire_at: datetime | timedelta | str | None = None,
) -> "Notice":
    """Post one notice, replacing any previous open notice.

    - `require_response=False` (default) -- an FYI; the user may read, reply,
      or ignore it. Post sparingly: a flood of low-value notices buries the
      ones that matter.
    - `require_response=True` -- a decision only the user can make (an action
      that is irreversible, outward-facing, spends real money, or reaches a
      real person). A decision the agent that spawned you could make goes to
      that agent via ava.agents.send_message instead.
    - `blocking=True` -- you cannot make progress until it is answered.

    Does not block: post, end your turn, and idle -- the reply arrives later
    as an ordinary message. You can have one open notice at a time; a new
    notify() auto-resolves the old one ("superseded"). Use edit_notice to add
    to the current notice instead of replacing it.

    Args:
        content: optional detail. Offer discrete choices as A / B / C so the
            user can reply with one letter (the reply is always free text).
        task: groups your notices by task in the user's queue.
        expire_at: lifetime deadline as datetime, timedelta, or ISO string;
            omitted defaults to the cluster-configured TTL limit.

    Returns:
        The notice id (an int); its `.superseded` attribute lists the ids
        this call auto-resolved.
    """
    title = coerce_str(title, "title")
    content = coerce_str(content, "content", allow_none=True)
    require_response = coerce_typed(require_response, "require_response", bool)
    blocking = coerce_typed(blocking, "blocking", bool)
    priority = coerce_str(priority, "priority")
    task = coerce_typed(task, "task", int, allow_none=True)
    if not title.strip():
        raise ValueError("title must be non-empty")
    validate_priority(priority)
    if blocking and not require_response:
        raise ValueError("blocking=True requires require_response=True (an FYI never stalls you)")

    expire_at_iso: str | None = None
    if expire_at is not None:
        from shared.daemon.schedules.watcher import normalize_when

        due_at = normalize_when(expire_at)
        if due_at < datetime.now(UTC):
            raise ValueError(
                f"expire_at is in the past: {due_at.isoformat()}. "
                "Provide a future time, or use a positive timedelta."
            )
        expire_at_iso = due_at.isoformat()

    aid = ava.agent_identity.require_agent_id()

    # One unified write path (R3 door ④): the gateway performs the whole
    # lifecycle atomically — supersede the previous open notice + insert the
    # new one in one transaction, then publish the events.
    from ava import _gateway_client

    resp = _gateway_client._post(
        f"/api/agents/{aid}/notices",
        {
            "title": title,
            "content": content,
            "priority": priority,
            "require_response": require_response,
            "blocking": blocking,
            "task_id": task,
            "expire_at": expire_at_iso,
        },
    )
    _raise_as_value_error(resp)
    data = resp.json()
    pending_rows: list[PendingNoticeRow] = [
        PendingNoticeRow(
            id=int(r["id"]),
            title=r["title"],
            created_at=datetime.fromisoformat(r["created_at"]),
            priority=r["priority"],
        )
        for r in data["pending_notices"]
    ]
    return Notice(
        int(data["id"]),
        pending_count=int(data["pending_count"]),
        pending_notices=pending_rows,
        superseded=[int(i) for i in data["superseded"]],
    )


def edit_notice(
    *,
    title: str = _UNSET,  # type: ignore[assignment]
    content: str | None = _UNSET,  # type: ignore[assignment]
    priority: str = _UNSET,  # type: ignore[assignment]
    blocking: bool = _UNSET,  # type: ignore[assignment]
) -> None:
    """require_response cannot be changed -- to turn an FYI into a question,
    dismiss this notice and post a fresh one.

    Args:
        content: pass None to clear.
        blocking: only valid on a notice that needs a response.
    """
    if title is not _UNSET:
        title = coerce_str(title, "title")
    if content is not _UNSET:
        content = coerce_str(content, "content", allow_none=True)
    if priority is not _UNSET:
        priority = coerce_str(priority, "priority")
    if blocking is not _UNSET:
        blocking = coerce_typed(blocking, "blocking", bool)
    if title is not _UNSET and not title.strip():
        raise ValueError("title must be non-empty")
    if priority is not _UNSET:
        validate_priority(priority)

    body: dict[str, object] = {}
    if title is not _UNSET:
        body["title"] = title
    if content is not _UNSET:
        body["content"] = content
    if priority is not _UNSET:
        body["priority"] = priority
    if blocking is not _UNSET:
        body["blocking"] = blocking
    if not body:
        raise ValueError("edit_notice needs at least one field to change")

    aid = ava.agent_identity.require_agent_id()

    # One unified write path (R3 door ④): the gateway edits the agent's
    # current open notice and re-publishes the posted event.
    from ava import _gateway_client

    resp = _gateway_client._patch(
        f"/api/agents/{aid}/notices/current",
        body,
    )
    _raise_as_value_error(resp)


def dismiss_notice() -> None:
    """Withdraw the open notice. At most one notice is open per agent (notify
    auto-resolves the previous one), so no id is needed."""
    aid = ava.agent_identity.require_agent_id()
    # One unified write path (R3 door ④): the gateway withdraws the agent's
    # current open notice and publishes the resolve + agent-updated events.
    from ava import _gateway_client

    resp = _gateway_client._post(f"/api/agents/{aid}/notices/current/dismiss")
    _raise_as_value_error(resp)


ava.register_namespace_member("ui", "notify", notify)
ava.register_namespace_member("ui", "edit_notice", edit_notice)
ava.register_namespace_member("ui", "dismiss_notice", dismiss_notice)
ava.register_namespace_member("self", "set_label", set_label)


# ── ava.tasks SDK namespace — the task registry (see task_registry.py) ───────
# A whole-module namespace (like ava_code's ava.cwd), not members hung on an
# existing group: the registry is a cohesive new surface, and the core top level
# stays small. register_sdk_expand promotes it into the in-prompt SDK reference
# so agents discover ava.tasks.* without drilling in.
ava.register_namespace("tasks", task_registry)
ava.register_sdk_expand("tasks")


# ── wrap ava.agents.spawn to add the fleet-only `label` arg ─────────────────
# Replace-wrapper: it re-implements spawn to expose `label` (adding a keyword to
# the surface, per the wrap contract) by calling `ava.agents._spawn_impl`
# directly instead of `inner`. Declared short-circuit — `label` cannot thread
# through the core spawn signature, so the wrapper owns the whole call; `inner`
# is accepted only to satisfy the wrap protocol.
def _spawn_with_label(
    inner: Callable[..., int],  # noqa: ARG001 — replace-wrapper; short-circuits inner (see note above)
    prompt: str | None = None,
    fork_from: int | None = None,
    machine: str | None = None,
    config_overlay: dict[str, object] | None = None,
    label: str | None = None,
) -> int:
    """Start a new agent; does not block.

    Args:
        prompt: the first message — make it self-contained, the new agent has
            no context about why you spawned it. Omit to leave it idling.
        fork_from: copy that agent's conversation state into the new one.
        machine: defaults to your own.
        config_overlay: per-agent settings overlay, e.g. {"llm_model": ...};
            a preset is named inside it as {"preset": "name"} (task #4086).
        label: initial role name; omitted = auto-named.
    """
    return ava.agents._spawn_impl(
        prompt=prompt,
        fork_from=fork_from,
        machine=machine,
        config=config_overlay,
        label=label,
    )


ava.extend.wrap("agents.spawn", _spawn_with_label)
