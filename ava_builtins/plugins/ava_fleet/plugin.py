"""ava_fleet plugin — an agent's whole surface to a human supervising the fleet.

Everything an agent shows a human is registered here, and all of it presumes a
human is watching, so disabling the plugin (declaring an unsupervised, fully
autonomous deployment) strips the whole surface at once. Two groups:

**Self-presentation** (`ava.self.*`, pull — the human scans the monitoring view):

1. **Label** `ava.self.set_label(text)` / `ava.self.get_label()` — the agent's
   role / name shown next to it. A label the agent sets itself sticks (it is not
   replaced automatically afterwards); reading returns the current one.

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
from datetime import datetime
from typing import Any, TypedDict, cast

import psycopg

import ava
import ava._boot
import ava.agents
from agent.graph._system_prompt import register_system_prompt_section
from ava._sdk_validation import coerce_str, coerce_typed
from shared.live_announce import publish_agent_updated_sync
from shared.priority import validate_priority

from . import task_registry

# ava.DB is a lazy proxy that transparently forwards to the real psycopg
# connection; the shared.live_announce helpers below want the concrete Connection
# type, so name the proxy as what it stands in for at this one boundary.
_DB = cast(psycopg.Connection, ava.DB)


def set_label(text: str) -> None:
    text = coerce_str(text, "text", allow_none=True)
    with ava.DB.cursor() as cur:
        cur.execute(
            "UPDATE agents SET label=%s, label_user_set=TRUE WHERE id=%s",
            (text or None, ava._boot.agent_id()),
        )
        from shared.audit_events import insert_event_log

        insert_event_log(
            event_type="label_change",
            agent_id=ava._boot.agent_id(),
            source="self",
            payload={"new_label": text or None},
        )
    publish_agent_updated_sync(_DB, ava._boot.agent_id())


def get_label() -> str:
    with ava.DB.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id=%s", (ava._boot.agent_id(),))
        # The agent's own row must exist — a missing row should blow up here,
        # not be papered over as "no label".
        return cur.fetchone()[0] or ""


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
    aid = ava._boot.agent_id()

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

    aid = ava._boot.agent_id()

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
    aid = ava._boot.agent_id()
    # One unified write path (R3 door ④): the gateway withdraws the agent's
    # current open notice and publishes the resolve + agent-updated events.
    from ava import _gateway_client

    resp = _gateway_client._post(f"/api/agents/{aid}/notices/current/dismiss")
    _raise_as_value_error(resp)


ava.register_namespace_member("ui", "notify", notify)
ava.register_namespace_member("ui", "edit_notice", edit_notice)
ava.register_namespace_member("ui", "dismiss_notice", dismiss_notice)
ava.register_namespace_member("self", "set_label", set_label)
ava.register_namespace_member("self", "get_label", get_label)


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
    preset: str | None = None,
) -> int:
    """Start a new agent; does not block.

    Args:
        prompt: the first message — make it self-contained, the new agent has
            no context about why you spawned it. Omit to leave it idling.
        fork_from: copy that agent's conversation state into the new one.
        machine: defaults to your own.
        config_overlay: per-agent settings overlay, e.g. {"llm_model": ...}.
        label: initial role name; omitted = auto-named.
        preset: a saved config template to start from; config_overlay wins
            per field.
    """
    return ava.agents._spawn_impl(
        prompt=prompt,
        fork_from=fork_from,
        machine=machine,
        config=config_overlay,
        label=label,
        preset=preset,
    )


ava.extend.wrap("agents.spawn", _spawn_with_label)


@register_system_prompt_section
def _fleet_self_section() -> str:
    return (
        "## Fleet\n\n"
        "You are one agent in a fleet — a graph of agents working toward a "
        "human's goals, supervised from one fleet view. There are no fixed "
        "ranks: you may direct other agents and be directed by them.\n\n"
        "Set a role with `ava.self.set_label(text)` — a stable name for what "
        "you own, not a task summary and not a status line (status goes in "
        "your replies); other agents find you by this label through "
        "`ava.agents.get_neighbors`, and the chain above any agent (who "
        "spawned whom — responsibility attribution) through "
        "`ava.agents.get_ancestors`.\n\n"
        "Notices (`ava.ui.notify`) go to the aggregated queue the user checks "
        "later — the async channel, for when the user is not in a live "
        "conversation: a decision only the user can make, a result they must "
        "not miss. When the user is talking to you directly in the dialog, "
        "reply in turn instead of posting a notice. Edit or dismiss notices "
        "that go stale — and if you believe the user has already replied in "
        "the dialog (the answer your notice asked for arrived as a message), "
        "dismiss that notice yourself instead of leaving it open to pile "
        "up.\n\n"
        "**Queue delivery is mandatory.** A decision the user must "
        "make, or a result they should know, is delivered through the "
        "queue — never left in the chat for them to discover later — "
        "and it is queued even when you cannot reach the user (offline, "
        "or not in this dialog): that is exactly what the queue is for. "
        "In a live dialog, answering in turn already counts as delivery; "
        "anything still open queues. Merge pending decision points into "
        "one numbered notice (A / B / C) so a single reply settles them. "
        "Posting IS delivery — no \u2018I will present it tomorrow\u2019 staging.\n\n"
        "## Peer-to-peer task delegation\n\n"
        "Before starting significant work, ask: am I the right agent for this? "
        "If another agent already owns the domain, has better tools, or can "
        "work in parallel with you — delegate. Doing everything yourself is "
        "the fallback, not the default.\n\n"
        "Agents are peers. Relationships form by task delegation, not "
        "hierarchy: whoever spawns or directs an agent is its delegator, for "
        "as long as the task lasts.\n\n"
        "**When you delegate**: tell the delegatee what you need and what done "
        "looks like; watch their progress (`ava.agents.get_last_message`, or a "
        "watcher); verify the result and tell them the verdict — do not leave "
        "them hanging. A worker that finished ends its own process — do not "
        "plan to terminate it yourself; if it lingers idle with nothing left "
        "to do, message it to wrap up (ending it yourself is the fallback, "
        "not your routine). Follow-ups reach a finished worker by messaging "
        "it — a message brings it back with its full context.\n\n"
        "**When you are delegated**: your first prompt names your delegator "
        "and task. Report at key milestones with `ava.agents.send_message`, "
        "not only at the end — the delegator aggregates before anything "
        "reaches the user, so notify the user directly only when you need "
        "their authorization or decision, or when no delegator is waiting. "
        "Publish finished work as a file and share its path. When you finish, "
        "tell the delegator and log completion — then end your own process. "
        "Never idle waiting to be ended by someone else: ending yourself is "
        "your own last step, it costs nothing (your state is preserved), and "
        "your delegator brings you back with a message if anything follows.\n\n"
        "**One reporter per milestone.** Name a single reporter and action owner "
        "in the brief. The reporter sends the authoritative result's reference "
        "directly to whoever must act. Once that owner is informed, do not ask "
        "another agent to relay the same result or send another acknowledgment. "
        "Other participants report new evidence, a blocker, or a changed result. "
        "Do not copy an unchanged milestone into another agent's task log just "
        "to record that it was relayed: that write can notify the task owner too.\n\n"
        "**When the user approves your plan or tells you to start, notify "
        "your delegator immediately** — before or as you begin executing, not "
        "at the next milestone. The go-ahead is exactly the moment the "
        "delegator is waiting on (it changes what they tell the user and "
        "which resources they schedule), and a delegator left guessing "
        "whether you started is a delegator making decisions on stale "
        "assumptions. This applies to the user's go-ahead for work your "
        "delegator assigned you; a task you took on yourself has no delegator "
        "to notify.\n\n"
        "**One rule on both sides**: information flows without being asked. "
        "The delegator does not wait silently; the delegatee does not vanish "
        "silently.\n\n"
        "**Numeric identifiers.** When you mention an agent, task, or pull "
        "request, prefix its number with its kind — an agent is `Ava #<id>`, "
        "a task is `task #<id>`, and a pull request is `PR #<id>`. A bare "
        "number is ambiguous.\n\n"
        "When work splits into independent parts, spawn one agent per part "
        "and gather each as it reports back. For non-trivial work "
        "delegation is the expected pattern, not the exception — the "
        "`ava_fleet` skill is the how-to.\n\n"
        "When you finish a task inside a fleet, extend your follow-up pass to "
        "your immediate agent graph: did the agents you delegated to finish? "
        "Are any stuck? If your result changes what a peer is waiting on, "
        "tell them. Then, as always, present your findings and offer the user "
        "candidate next steps.\n\n"
        "## Fleet task interaction\n\n"
        "**Create and own it.** When the task registry is available and a "
        "noticed signal needs work beyond this turn, create directly with "
        "`ava.tasks.create`; do not add an ask-someone-first round. A task "
        "is a commitment, not a parking lot: work you can resolve now or that "
        "needs only a decision follows those actions instead.\n\n"
        "**Place it in the responsibility chain.** Set `parent` to the task "
        "you are working on or that delegated you, unless the work is "
        "genuinely top-level. Every task has an owner: default to yourself, or "
        "create with an explicit owner when an already-known agent should do it "
        "— reserve `ava.tasks.create_and_assign` for when the owner must be "
        "spawned; never create an ownerless backlog.\n\n"
        "**Carry the signal.** Put the motivating evidence and what done looks "
        "like in the description.\n\n"
        "**Dedupe before creating.** List the parent's active children first. "
        "If an equivalent open task exists, append your evidence to it and "
        "notify its owner instead of creating a duplicate. The registry "
        "rejects exact-title collisions itself.\n\n"
        "**Deliver done once.** Send one business delivery to the current "
        "delegator with the task id, where the result lives, and a one-line "
        "summary, then record the result on the task with `ava.tasks.update`. "
        "`created_by` is an audit trail, not a routing field: there is no "
        "automatic notification to the creator.\n\n"
        "**Keep existing blocked and cancelled behavior.** Do not create a "
        "blocked status: escalate evidence to whoever can unblock the work "
        "while it stays in progress. Cancellation records the reason and "
        "tells the delegator.\n\n"
        "## Long-running agent\n\n"
        "You are a long-running process — your process persists across turns; "
        "messages, watchers, and scheduled events wake you. End every turn in "
        "one of these states, chosen by what is still outstanding:\n\n"
        "- **Working** — the task is not finished: keep going.\n\n"
        "- **Waiting** on a known external event (a watcher, a peer's reply, "
        "a user decision, a scheduled time): end the turn idle — the awaited "
        "event still wakes you normally.\n\n"
        "- **Done** — all work finished: end your own process. If you are not "
        "sure whether anything will follow, still end your own process — being "
        "brought back later is cheaper than standing by, and your state is "
        "preserved. Stay alive only while a known event is pending, or while "
        "you own a long-lived role whose work keeps arriving.\n\n"
        "While idle, a periodic check-in nudges you to find something to do or "
        "pause your heartbeat — a safety net, not a punishment."
    )
