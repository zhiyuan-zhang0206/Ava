"""Event contract registry — the single source of truth for event names.

Current structure: base/events/docs/events.ava.okf.md.

``EVENTS`` is one ``EventSpec`` per event name (the stream's
``event_name`` field, OTel LogRecord semantics): writers add one entry;
producers emit through ``base.telemetry.emit`` (fail-fast on unregistered
names); readers consume payload keys through the derived SQL fragment
constants (a hand-written ``attributes->>'...'`` literal elsewhere fails the
SQL-key lint); base/events/registry.md is generated from this module.
Payload schemas and event declarations live in the domain modules of
``base/events/declarations/``; ``load_events`` merges them and this module derives the views.

Derived views live here and nowhere else: ``category_for_kind``,
``telemetry_events``, ``family_events``,
``payload_keys``, event tiers, plus the folded ``_LLM_ERROR_EVENTS`` family
and the ops grid constants.
"""

from __future__ import annotations

from typing import get_type_hints

from base.events.loader import load_events
from base.events.vocabulary import LLM_ERROR_FAMILY as LLM_ERROR_FAMILY
from base.events.vocabulary import OPS_BUCKET_S as OPS_BUCKET_S
from base.events.vocabulary import OPS_GRID_ORIGIN as OPS_GRID_ORIGIN
from base.events.vocabulary import Category as Category
from base.events.vocabulary import EventSpec as EventSpec
from base.events.vocabulary import EventTier as EventTier

EVENTS: dict[str, EventSpec] = load_events()


# ── derived views — the only spellings consumers may use ───────────────────


TIER_BY_EVENT: dict[str, EventTier] = {name: spec.tier for name, spec in EVENTS.items()}


def tier_for(event_name: str, category: str, level: str) -> EventTier:
    """Human-facing tier for one persisted event row.

    A registered name always reads through ``TIER_BY_EVENT`` first, so a
    registry/mapping drift raises instead of silently changing the events
    page. Unknown historical names remain useful observations. The row's
    severity and category deliberately take priority over that default tier:
    warning-or-higher is an anomaly, and an audit row is a business fact.
    """
    declared = TIER_BY_EVENT[event_name] if event_name in EVENTS else None
    if level.lower() in {"warning", "error", "critical"}:
        return "anomaly"
    if category == "audit":
        return "business"
    return declared if declared is not None else "observation"


def category_for_kind(event_name: str) -> Category:
    """Declared category for `event_name`, else ``"log"`` (loguru fallback)."""
    spec = EVENTS.get(event_name)
    return spec.category if spec is not None else "log"


def telemetry_events() -> frozenset[str]:
    """Every telemetry-category event name — replaces ``_TELEMETRY_KINDS``."""
    return frozenset(name for name, spec in EVENTS.items() if spec.category == "telemetry")


def family_events(family: str) -> tuple[str, ...]:
    """Event names in `family`, declaration order — replaces the hand-copied
    ``_LLM_ERROR_EVENTS`` tuples."""
    return tuple(name for name, spec in EVENTS.items() if spec.family == family)


def payload_keys(event_name: str) -> tuple[str, ...]:
    """Declared attribute keys for `event_name` (payload TypedDict order);
    empty for untyped payloads. A key a reader needs but no producer declared
    is a contract violation, not a query detail."""
    spec = EVENTS.get(event_name)
    if spec is None or spec.payload is None:
        return ()
    return tuple(get_type_hints(spec.payload))


# ── SQL fragment constants — the only key spellings read sites may use ──
# One dict per payload-bearing event, derived from the payload TypedDict: a
# renamed key empties the dict and every reader fails (KeyError). A literal
# ``attributes->>'...'`` elsewhere fails the SQL-key lint.


def _sql_keys(event_name: str) -> dict[str, str]:
    """``{key: "attributes->>'key'"}`` per declared payload key."""
    return {k: f"attributes->>'{k}'" for k in payload_keys(event_name)}


LLM_USAGE_KEYS = _sql_keys("llm_usage")
TURN_END_KEYS = _sql_keys("turn_end")
EXEC_KEYS = _sql_keys("exec")
CODE_KEYS = _sql_keys("code")
EXEC_FAILED_KEYS = _sql_keys("exec_failed")
HALT_KEYS = _sql_keys("halt")
SYNTAX_FIX_KEYS = _sql_keys("syntax_fix")
SSE_DROP_KEYS = _sql_keys("sse_drop")
EVENT_LOG_DROP_KEYS = _sql_keys("event_log_drop")
DELIVERY_STALLED_KEYS = _sql_keys("delivery_stalled")
DELIVERY_POISONED_KEYS = _sql_keys("delivery_poisoned")
DELIVERY_WAKE_SUPPRESSED_KEYS = _sql_keys("delivery_wake_suppressed")
FRONTEND_INTERACTION_KEYS = _sql_keys("frontend_interaction")
SDK_CALL_KEYS = _sql_keys("sdk_call")
SERVICE_STARTED_KEYS = _sql_keys("service_started")
AGENT_SPAWNED_KEYS = _sql_keys("agent_spawned")
NODE_EXIT_KEYS = _sql_keys("node_exit")
HEARTBEAT_PAUSED_KEYS = _sql_keys("heartbeat_paused")
TASK_UPDATE_KEYS = _sql_keys("task_update")
PROCESS_EXIT_KEYS = _sql_keys("process_exit")
RECALL_FILTER_KEYS = _sql_keys("recall_filter")
PASSIVE_RECALL_KEYS = _sql_keys("passive_recall")
GATEWAY_LATENCY_KEYS = _sql_keys("gateway_latency")
LOG_KEYS = _sql_keys("log")
PLUGIN_ACTIVATION_KEYS = _sql_keys("plugin_activation")


def registered_payload_keys() -> frozenset[str]:
    """Every declared attribute key — the SQL-key lint's registration surface."""
    return frozenset(k for spec in EVENTS.values() for k in payload_keys(spec.name))
