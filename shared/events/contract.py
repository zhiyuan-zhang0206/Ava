"""Event contract registry — the single source of truth for event names (R2-C).

Design: design-r2/design-concept.md §4.3 + okf/design/r2-single-source-of-truth (C).

``EVENTS`` is one ``EventSpec`` per event name (the ``events`` table's
``event_name`` column, OTel LogRecord semantics): writers add one entry;
producers emit through ``shared.telemetry.emit`` (fail-fast on unregistered
names); readers consume payload keys through the derived SQL fragment
constants (a hand-written ``attributes->>'...'`` literal elsewhere fails the
SQL-key lint); shared/events/registry.md is generated from this module.
Payload schemas and registry declarations live in focused shard modules;
this facade preserves the stable import surface.

Derived views live here and nowhere else: ``category_for_kind``,
``telemetry_events``, ``lineage_event_names``, ``family_events``,
``payload_keys``, event tiers, plus the folded ``_LLM_ERROR_EVENTS`` family
and the ops grid constants.
"""

from __future__ import annotations

from dataclasses import dataclass as dataclass
from datetime import UTC as UTC
from datetime import datetime as datetime
from typing import Any as Any
from typing import Literal as Literal
from typing import LiteralString as LiteralString
from typing import NotRequired as NotRequired
from typing import TypedDict as TypedDict
from typing import get_type_hints as get_type_hints

from shared.events.payloads import LLM_ERROR_FAMILY as LLM_ERROR_FAMILY
from shared.events.payloads import OPS_BUCKET_S as OPS_BUCKET_S
from shared.events.payloads import OPS_GRID_ORIGIN as OPS_GRID_ORIGIN
from shared.events.payloads import AgentSpawned as AgentSpawned
from shared.events.payloads import Category as Category
from shared.events.payloads import CompactionCompleted as CompactionCompleted
from shared.events.payloads import ComputerAction as ComputerAction
from shared.events.payloads import ComputerSessionEnd as ComputerSessionEnd
from shared.events.payloads import ComputerSessionStart as ComputerSessionStart
from shared.events.payloads import DeliveryPoisoned as DeliveryPoisoned
from shared.events.payloads import DeliveryStalled as DeliveryStalled
from shared.events.payloads import DeliveryWakeSuppressed as DeliveryWakeSuppressed
from shared.events.payloads import EventLogDrop as EventLogDrop
from shared.events.payloads import EventTier as EventTier
from shared.events.payloads import ExecChildBoot as ExecChildBoot
from shared.events.payloads import ExecEnvelope as ExecEnvelope
from shared.events.payloads import ExecFailed as ExecFailed
from shared.events.payloads import ExecPayload as ExecPayload
from shared.events.payloads import ExecSubprocessKilled as ExecSubprocessKilled
from shared.events.payloads import FrontendInteraction as FrontendInteraction
from shared.events.payloads import Halt as Halt
from shared.events.payloads import HeartbeatNudged as HeartbeatNudged
from shared.events.payloads import HeartbeatPaused as HeartbeatPaused
from shared.events.payloads import IdleWake as IdleWake
from shared.events.payloads import LlmProviderError as LlmProviderError
from shared.events.payloads import LlmRetry as LlmRetry
from shared.events.payloads import LlmUsage as LlmUsage
from shared.events.payloads import NodeExit as NodeExit
from shared.events.payloads import NodeExitEntry as NodeExitEntry
from shared.events.payloads import PluginActivation as PluginActivation
from shared.events.payloads import RetentionClass as RetentionClass
from shared.events.payloads import SdkCall as SdkCall
from shared.events.payloads import ServiceStarted as ServiceStarted
from shared.events.payloads import SilentIdle as SilentIdle
from shared.events.payloads import Spawn as Spawn
from shared.events.payloads import SseDrop as SseDrop
from shared.events.payloads import StatusChange as StatusChange
from shared.events.payloads import SyntaxFix as SyntaxFix
from shared.events.payloads import TaskEscalation as TaskEscalation
from shared.events.payloads import TaskReminderDigest as TaskReminderDigest
from shared.events.payloads import TaskUpdate as TaskUpdate
from shared.events.payloads import TurnEnd as TurnEnd
from shared.events.registry import _EVENTS_RUNTIME
from shared.events.registry import _audit as _audit
from shared.events.registry import _telemetry as _telemetry
from shared.events.registry import _telemetry_audit as _telemetry_audit
from shared.events.registry_ops import _EVENTS_OPS
from shared.events.system import AgentBootFailed as AgentBootFailed
from shared.events.system import AgentRegistry as AgentRegistry
from shared.events.system import ArchiveFetchDegraded as ArchiveFetchDegraded
from shared.events.system import Auth401Rejected as Auth401Rejected
from shared.events.system import CheckpointTableSizes as CheckpointTableSizes
from shared.events.system import EventClassReopened as EventClassReopened
from shared.events.system import EventSpec as EventSpec
from shared.events.system import GateAuthProbeFailed as GateAuthProbeFailed
from shared.events.system import GatewayEventLoop as GatewayEventLoop
from shared.events.system import GatewayLatency as GatewayLatency
from shared.events.system import GatewayProcess as GatewayProcess
from shared.events.system import HookTiming as HookTiming
from shared.events.system import HostDispatcherScanFailed as HostDispatcherScanFailed
from shared.events.system import LogPayload as LogPayload
from shared.events.system import LokiQueryBudget as LokiQueryBudget
from shared.events.system import LokiQueryFailed as LokiQueryFailed
from shared.events.system import MemorySearchStats as MemorySearchStats
from shared.events.system import OtlpBackendDisabled as OtlpBackendDisabled
from shared.events.system import OtlpBackendRecovered as OtlpBackendRecovered
from shared.events.system import PageServeDirMissing as PageServeDirMissing
from shared.events.system import PassiveRecall as PassiveRecall
from shared.events.system import PitrRemoteInventory as PitrRemoteInventory
from shared.events.system import PluginLoadFailed as PluginLoadFailed
from shared.events.system import ProcessExit as ProcessExit
from shared.events.system import PromQueryBudget as PromQueryBudget
from shared.events.system import PromQueryFailed as PromQueryFailed
from shared.events.system import RecallFilter as RecallFilter
from shared.events.system import RecoveryDrillFailed as RecoveryDrillFailed
from shared.events.system import ResolutionStatus as ResolutionStatus
from shared.events.system import ResolvedMarker as ResolvedMarker
from shared.events.system import ScheduleStalled as ScheduleStalled
from shared.events.system import SseLifecycle as SseLifecycle
from shared.events.system import TelemetryReadRecovered as TelemetryReadRecovered
from shared.events.system import TelemetryReadStale as TelemetryReadStale
from shared.events.system import WatchdogTick as WatchdogTick

EVENTS: dict[str, EventSpec] = {**_EVENTS_RUNTIME, **_EVENTS_OPS}


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


def lineage_event_names() -> frozenset[str]:
    """Every event name declared ``retention_class="lineage"``.

    The single source for both permanent copies: the Loki ``retention_stream``
    selector (validated by ``shared.loki_index_labels``) and the lineage JSONL
    mirror (``shared.telemetry``). A name added here reaches both."""
    return frozenset(name for name, spec in EVENTS.items() if spec.retention_class == "lineage")


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


def registered_payload_keys() -> frozenset[str]:
    """Every declared attribute key — the SQL-key lint's registration surface."""
    return frozenset(k for spec in EVENTS.values() for k in payload_keys(spec.name))


def sql_join(*parts: str) -> LiteralString:
    """Join static SQL fragments into one query (``LiteralString``).

    Direct ``cur.execute`` read sites build through this helper so ruff's
    S608 heuristic does not misread a registry constant as user input, and
    psycopg's injection guard stays intact. Parts must be literals or
    registry-derived constants — never request-path values.
    """
    return "".join(parts)  # type: ignore[return-value]
