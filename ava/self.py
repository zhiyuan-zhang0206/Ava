from __future__ import annotations

import json as _json
from typing import NoReturn

import ava
from ava._attach import attach as attach
from ava._sdk_validation import coerce_str, coerce_typed
from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.lifecycle import AgentRestart, AgentTermination, _SystemHalt
from shared.live_events import CompactRequest
from shared.redis_client import publish_best_effort_sync

# Deliberately NOT in __all_for_ava__ (importable, but out of the rendered SDK
# docs): AgentRestart / AgentTermination are framework control-flow exceptions
# raised on the success path of restart() / terminate() — the agent never
# catches them; InvalidConfigOverlay / UpdateError / UpdateTriggerFailed /
# NothingToUpdate are rare outcomes whose traceback explains itself when one
# fires.
__all_for_ava__ = [
    "AGENT_ID",
    "MACHINE_SPEC",
    "SELF_MACHINE_NAME",
    "attach",
    "compact",
    "pause_heartbeat",
    "restart",
    "terminate",
    "update",
]

# AGENT_ID is the agent-facing read of this process's identity. The canonical
# slot lives framework-internally in `ava._boot` (set once by the bootstrap at
# process startup); this is a re-export served by the module `__getattr__`
# below, NOT a stored attribute — so the kernel never reaches through this
# disable-able `ava.self` module to learn who it is, and `AVA_SDK_DISABLE` can
# remove the whole `self` namespace without stripping the identity.
#
# Annotation-only (no value binding) keeps the name out of the module dict so
# every `ava.self.AGENT_ID` access routes to `__getattr__`.
AGENT_ID: int

MACHINE_SPEC: tuple[str, str]
"""(name, description) of the machine this agent runs on."""

SELF_MACHINE_NAME: str
"""Name of the machine this agent runs on — the default target for
`ava.agents.spawn` when its `machine` argument is omitted."""


def __getattr__(name: str) -> object:
    if name == "AGENT_ID":
        # Re-export the framework-internal identity as a plain int (the None
        # placeholder before bootstrap). `ava.help(ava.self)` still documents
        # the name from the AnnAssign + PEP 224 docstring above — a bare value
        # routes through the doc-carrying constant renderer there.
        # Local import: the MACHINE_SPEC branch's `import ava` makes `ava` a
        # function-local name, so reach the bootstrap slot explicitly here.
        import ava._boot

        return ava._boot.agent_id()
    if name == "MACHINE_SPEC":
        import ava
        from shared.machine import machine_description, machine_name

        return ava.const(
            (machine_name(), machine_description() or ""),
            doc=(
                "(name, description) of the machine this agent runs on; "
                "description is free prose, empty if unset."
            ),
        )
    if name == "SELF_MACHINE_NAME":
        import ava
        from shared.machine import machine_name

        return ava.const(
            machine_name(),
            doc=(
                "Name of the machine this agent runs on; the default target "
                "for `ava.agents.spawn` when `machine` is omitted."
            ),
        )
    if name == "InvalidConfigOverlay":
        from shared.plugin_config_registry import InvalidConfigOverlay

        return InvalidConfigOverlay
    # Plugin members land on ava.self via register_namespace_member (ava_fleet
    # adds log / set_label / get_label). In an agent-launched persistent-shell
    # child they are absent until plugins load, and this module already exists so
    # ava.__getattr__ never fires — trigger the shared lazy load here, then retry.
    import sys as _sys

    import ava

    if ava._maybe_load_plugins_for_missing(name):
        return getattr(_sys.modules[__name__], name)
    raise AttributeError(f"module 'ava.self' has no attribute {name!r}")


# `InvalidConfigOverlay` is lazily bound via module __getattr__ —
# `from shared.plugin_config_registry import X` anywhere in ava.self triggers
# agent.__init__ → agent.graph._llm calling `ava.help(...)`
# which reverse-accesses an ava attribute, while ava.__init__ is still
# running line 91 `import ava.self` and ava.help isn't registered yet →
# AttributeError. Lazy makes this import chain only resolve when the
# caller actually accesses InvalidConfigOverlay.


# ── update() Exception hierarchy ───────────────────────────────────────────


class UpdateError(Exception): ...


class UpdateTriggerFailed(UpdateError):  # noqa: N818
    """The update could not be started. Nothing has changed; safe to retry."""


class NothingToUpdate(UpdateError):  # noqa: N818
    """Everyone is already on the latest code; there is nothing to update.

    This is not a failure: no one was restarted, and retrying will not help
    until newer code exists. Stop here rather than calling update() again.
    """


def _publish_self_inbound_wake() -> None:
    """Best-effort Redis wake for a self-inserted inbound (restart / terminate /
    compact / update), over the agent's own SDK redis handle (`ava.REDIS`).

    Same contract as `shared.db.publish_inbound_wake`: never raises (the claim
    loop's SELECT recheck delivers the row regardless), but a `NoPermissionError`
    is logged rather than swallowed — it means this agent's redis ACL user is not
    granted its own `<prefix>:inbound:*` channel (an ACL / channel-prefix
    misconfig). Channel derived via `inbound_channel` so it matches
    `RedisInboundListener` and stays inside the ACL grant."""
    from redis.exceptions import ResponseError

    from ava import _boot
    from shared.cluster import inbound_channel
    from shared.log import logger

    channel = inbound_channel(_boot.agent_id())
    try:
        ava.REDIS.publish(channel, "0")
    except ResponseError as exc:
        logger.warning(
            "self inbound wake publish to {ch!r} rejected by redis ({exc!r}) — "
            "this agent's redis ACL user lacks its inbound channel; the "
            "self-restart/terminate/compact wake falls back to the claim SELECT "
            "recheck. Check ensure_cluster_redis_acl.",
            ch=channel,
            exc=exc,
        )
    except Exception as exc:
        logger.debug(
            "self inbound wake publish to {ch!r} skipped ({exc!r}) — best-effort.",
            ch=channel,
            exc=exc,
        )


def restart(config_overlay: dict[str, object] | None = None) -> NoReturn:
    """`config_overlay`: a flat `{field_name: value}` dict merged into your
    persistent per-agent settings.
    """
    config_overlay = coerce_typed(config_overlay, "config_overlay", dict, allow_none=True)
    from ava import _boot

    _boot.assert_self_action("restart")
    payload_json: str | None = None
    if config_overlay:
        from shared.plugin_config_registry import validate_config_overlay

        validate_config_overlay(config_overlay)
        payload_json = _json.dumps({"config_overlay": dict(config_overlay)}, sort_keys=True)

    with ava.DB.cursor() as cur:
        if config_overlay:
            # ava.DB is autocommit: the overlay UPDATE commits before the restart
            # inbound is queued — they are not one transaction. If the process
            # dies in between, the overlay is staged and applied on the next
            # restart; calling restart() again re-merges the same keys
            # (idempotent), so this is safe.
            cur.execute(
                "UPDATE agents_meta "
                "SET config_overlay = COALESCE(config_overlay, '{}'::jsonb) || %s::jsonb "
                "WHERE id = %s",
                (_json.dumps(dict(config_overlay)), _boot.agent_id()),
            )
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, '', 'restart', 'self', %s::jsonb)",
            (_boot.agent_id(), payload_json),
        )
    _publish_self_inbound_wake()
    raise AgentRestart


def terminate() -> NoReturn:
    """Your conversation state is preserved; a message from a peer or the
    user resurrects you with full context.
    """
    from ava import _boot

    _boot.assert_self_action("terminate")
    with ava.DB.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'terminate', 'self')",
            (_boot.agent_id(),),
        )
    _publish_self_inbound_wake()
    raise AgentTermination


def pause_heartbeat(duration: float) -> None:
    """Suppress idle heartbeat check-ins for the next `duration` seconds.

    Only the heartbeat is suppressed; real wake-ups still reach you. A later
    call replaces the window. For a known wait, prefer the longest window that
    fits it.

    Args:
        duration: seconds, at most the configured heartbeat pause limit (default
            86400 = 24 hours; per-agent override via
            ava.self.restart(config_overlay=...)).
    """
    duration = coerce_typed(duration, "duration", (int, float))
    from ava import _boot

    if not duration > 0:
        raise ValueError(f"duration must be greater than 0 seconds, got {duration!r}")
    limit = turn_settings.agent.heartbeat_pause_max_seconds
    if duration > limit:
        raise ValueError(
            f"duration must be at most {limit:.0f} seconds (heartbeat pause limit; "
            "per-agent override via ava.self.restart("
            "config_overlay={'heartbeat_pause_max_seconds': ...}), cluster default via "
            "AVA_HEARTBEAT_PAUSE_MAX_SECONDS), "
            f"got {duration!r}"
        )
    with ava.DB.cursor() as cur:
        cur.execute(
            "INSERT INTO heartbeat_pause_log (agent_id, duration_s) VALUES (%s, %s)",
            (_boot.agent_id(), float(duration)),
        )
        cur.execute(
            "UPDATE agents_meta "
            "SET heartbeat_paused_until = now() + make_interval(secs => %s) "
            "WHERE id = %s",
            (float(duration), _boot.agent_id()),
        )
        from shared import telemetry

        telemetry.emit(
            "telemetry",
            "heartbeat_paused",
            level="info",
            agent_id=_boot.agent_id(),
            attributes={"duration_s": duration},
        )


def compact(summary: str) -> NoReturn:
    """Replace your whole message history with `summary` — it becomes your
    entire memory of everything before it; nothing raw is kept beside it.

    First persist durable state: your personal memory (`memory/` in your
    workspace plus its `MEMORY.md` index) and the shared pool (`ava.memory`,
    for facts other agents need). Then write `summary` — a first-person record
    of this conversation round. Fill every section; write "(none)" only when
    empty:

    - Requests
    - Progress
    - In flight
    - Dead ends
    - Pitfalls
    - Verbatim tail (exclude the compaction request that triggered this)
    """
    summary = coerce_str(summary, "summary")
    from ava import _boot

    _boot.assert_self_action("compact")
    with ava.DB.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind) "
            "VALUES (%s, %s, 'compact_summary')",
            (_boot.agent_id(), summary),
        )
        from shared.audit_events import insert_event_log

        insert_event_log(
            event_type="compact",
            agent_id=_boot.agent_id(),
            source="self",
            payload={"compact_kind": "summary", "length": len(summary)},
        )
    # Best-effort: a publish failure must not stop the wake + _SystemHalt below.
    # The durable compact_summary inbound is already committed; if this live-UI
    # event is lost the frontend recovers on its next fetch. Routed through the
    # never-raise primitive so redis can never interrupt this lifecycle exit.
    publish_best_effort_sync(
        settings.data_plane.events_channel,
        CompactRequest(
            agent_id=_boot.agent_id(),
            content=f"[compact requested, {len(summary)} chars]",
        ).model_dump_json(),
        context="compact_request",
    )
    # Also wake the claim node via Redis pub/sub (redundant for same-process
    # but ensures cross-process scenarios also receive the wake).
    _publish_self_inbound_wake()
    raise _SystemHalt


def update() -> NoReturn:
    """Removed — updates go through the CLI only.

        ava cluster update                 # smooth (default)  # lint-docstring: ok CLI command name
        ava cluster update --mode force    # force: ~10s drain  # lint-docstring: ok CLI command name

    Raises:
        RuntimeError: always — this method no longer exists.
    """
    raise RuntimeError(
        "ava.self.update() has been removed; update the cluster from the CLI "
        "instead: `ava cluster update` (smooth) or `ava cluster update --mode "
        "force` (force-kill stragglers)."
    )
