"""Inbound envelope — dispatch message prefix to agent by source.

``source`` field value spec (mutually exclusive):

    | source                  | origin / writer                | wrapped prefix                                      |
    |-------------------------|--------------------------------|-----------------------------------------------------|
    | ``system``              | framework internal INSERT      | original text (no wrap)                             |
    | ``system:<subtype>``    | framework internal INSERT      | original text (no wrap)                             |
    | ``agent:N``             | peer agent N                   | "Agent N [ts]:\n\n..."                            |
    | ``user``                | the human user (the UI)        | "User [ts]:\n\n..."                               |
    | ``ui:page:<name>``      | an HTML page the agent started | "User [ts]:\n\n..."                               |
    | ``watcher:N``           | a watcher session's wake-up    | "Watcher (id N) [ts]:\n\n..."                       |
    | ``shell:N``             | a background shell command's completion notice | "Shell session (id N) [ts]:\n\n..."  |
    | ``schedule:N``          | a gateway-hosted schedule N       | "Schedule (id N) [ts]:\n\n..."                      |

``user`` covers every human-originated inbound — chat from the composer,
question / report replies, and the lifecycle triggers (restart / terminate /
resurrect) the user fires. ``ui:page:<name>`` is a callback POSTed by an HTML
page the agent itself started (a page registered via ``ava.ui.show``); it also
reads as "User", and the agent recovers which page posted from ``<name>``.
``watcher:N`` is a wake-up delivered from inside a watcher session (a scheduled
fire, or the exit notice sent when the watcher stops); N is the watcher's
session id, so the agent recovers which watcher fired from N. ``shell:N`` is
the completion notice of a background shell command (``ava.shell.run_background``);
N is the shell session id the command ran in. ``schedule:N`` is stamped by a
gateway-hosted schedule (`schedules` row N) when
it spawns / wakes / resurrects an agent; the agent recovers which schedule acted
from N.
"""

from __future__ import annotations

from datetime import datetime

from shared.caller_identity import PREFIXES as _CALLER_PREFIXES
from shared.caller_identity import CallerIdentity
from shared.config import format_timestamp, now_timestamp, settings

_AGENT_PREFIX = "agent:"
_PAGE_PREFIX = "ui:page:"
_SCHEDULE_PREFIX = "schedule:"
_SHELL_PREFIX = "shell:"
_SYSTEM_PREFIX = "system:"
_WATCHER_PREFIX = "watcher:"


def _check_positive_int_id(value: str, source: str) -> None:
    """Validate `value` is a positive integer (for watcher source id segment)."""
    try:
        n = int(value)
    except ValueError as e:
        raise ValueError(f"source must carry <non-negative-int> id, got {source!r}") from e
    if n < 0 or str(n) != value:
        raise ValueError(f"source must carry <non-negative-int> id, got {source!r}")


def validate_source(source: str) -> None:
    """Raise ValueError if `source` doesn't match the envelope dispatch whitelist.

    Same legal set as `wrap_inbound`, but only validates, doesn't build wrap
    string. Used at the boundary (gateway HTTP layer) to 422 reject early,
    preventing buggy clients from writing illegal source into
    `inbound_messages` and causing the agent claim node to hit ValueError and
    kill the whole agent process.
    """
    if source.startswith(_CALLER_PREFIXES):
        CallerIdentity.from_source(source)
        return
    if source == "system" or source.startswith(_SYSTEM_PREFIX):
        return
    if source == "user":
        return
    if source.startswith(_AGENT_PREFIX):
        return
    if source.startswith(_PAGE_PREFIX):
        return
    if source.startswith(_WATCHER_PREFIX):
        _check_positive_int_id(source.removeprefix(_WATCHER_PREFIX), source)
        return
    if source.startswith(_SHELL_PREFIX):
        _check_positive_int_id(source.removeprefix(_SHELL_PREFIX), source)
        return
    if source.startswith(_SCHEDULE_PREFIX):
        _check_positive_int_id(source.removeprefix(_SCHEDULE_PREFIX), source)
        return
    raise ValueError(
        f"Unrecognized inbound source: {source!r} — "
        "must be 'system' / 'system:<subtype>' / 'agent:N' / 'user' / 'ui:page:<name>' / "
        "'watcher:N' / 'shell:N' / 'schedule:N'"
    )


def wrap_inbound(content: str, source: str, *, created_at: datetime | None = None) -> str:
    """Dispatch envelope wrap by source.

    When ``created_at`` is given (the inbound row's original creation time),
    the timestamp uses that wall-clock so the agent sees a chronological timeline
    even when several messages are claimed in one batch. When None (legacy
    callers / system messages), the current time is used as before.
    """
    if source.startswith(_CALLER_PREFIXES):
        caller = CallerIdentity.from_source(source)
        instance = f" / {caller.instance}" if caller.instance is not None else ""
        label = "External agent" if caller.kind == "external_agent" else "Unknown caller"
        return f"{label} ({caller.subject}{instance}; asserted provenance):\n\n{content}"
    if source == "system" or source.startswith(_SYSTEM_PREFIX):
        return f"[system] {content}"
    # `ts` carries its own leading space so the off-state leaves no stray
    # space before the colon (gated by settings.general.message_timestamps).
    if settings.general.message_timestamps:
        formatted = format_timestamp(created_at) if created_at is not None else now_timestamp()
        ts = f" {formatted}"
    else:
        ts = ""
    if source == "user" or source.startswith(_PAGE_PREFIX):
        return f"User{ts}:\n\n{content}"
    if source.startswith(_AGENT_PREFIX):
        sender_id = source.removeprefix(_AGENT_PREFIX)
        return f"Agent {sender_id}{ts}:\n\n{content}"
    if source.startswith(_WATCHER_PREFIX):
        wid = source.removeprefix(_WATCHER_PREFIX)
        _check_positive_int_id(wid, source)
        return f"Watcher (id {wid}){ts}:\n\n{content}"
    if source.startswith(_SHELL_PREFIX):
        sid = source.removeprefix(_SHELL_PREFIX)
        _check_positive_int_id(sid, source)
        return f"Shell session (id {sid}){ts}:\n\n{content}"
    if source.startswith(_SCHEDULE_PREFIX):
        sid = source.removeprefix(_SCHEDULE_PREFIX)
        _check_positive_int_id(sid, source)
        return f"Schedule (id {sid}){ts}:\n\n{content}"
    raise ValueError(
        f"Unrecognized inbound source: {source!r} — "
        "must be 'system' / 'system:<subtype>' / 'agent:N' / 'user' / 'ui:page:<name>' / "
        "'watcher:N' / 'shell:N' / 'schedule:N'"
    )
