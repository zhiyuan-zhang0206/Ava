"""Cross-process boundary contracts — the doorplates (R3).

Every cross-process boundary is a door. Each door carries a doorplate
declaring three things: can it be pushed again (idempotency), is it open
during a migration (pause exemption), who is responsible for it (truth
source). A doorplate is code, not a comment: consumers inherit behavior
from the doorplate instead of guessing.

This module is the ONLY place a route contract is declared — the doorplate
wall. Route authors add one entry per route they own; the pause middleware
reads exemptions from here (via `gateway.middleware.pause_policy`), the SDK reads
idempotency from here, and lint forces every gateway route to declare a
doorplate (`gateway/tests/test_route_contracts_gateway.py`).

Invariants (design concept v0.3):
1. Declared at the boundary definition — idempotency / exemption are
   declared here, never copied into callers or comments.
2. Server promises, clients inherit — callers derive retry / exemption
   behavior from the contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache


class Idempotency(StrEnum):
    """Delivery semantics a client may rely on when re-sending a request.

    IDEMPOTENT: safe to retry blindly — repeating the request cannot
        change the outcome (reads, CAS state transitions, upserts).
    NON_IDEMPOTENT: never auto-retry — a retry can duplicate the effect
        (pure INSERTs: spawn, create, upload).
    AT_LEAST_ONCE_WITH_KEY: safe to retry with the same ``Idempotency-Key``
        only when the handler records identity and immutable request matching
        in the business transaction. A durable receipt proves acceptance, not
        delivery or execution. Mutable response fields may be recomputed.
        The generic ``api_idempotency`` response cache does not make effects
        atomic; new keyed-effect routes must use transactional ownership.
    """

    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"
    AT_LEAST_ONCE_WITH_KEY = "at_least_once_with_key"


class PauseSemantics(StrEnum):
    """Whether a route stays reachable while the cluster is paused
    (mid-migration, everything else answers 503 + Retry-After).

    CONTROL_PLANE: must survive a migration — the quiesce that owns the
        pause window depends on it (control/observability surface, agent
        self-reports). Exempt from the pause 503.
    DATA_PLANE: default — 503 during the pause window.
    """

    CONTROL_PLANE = "control-plane"
    DATA_PLANE = "data-plane"


@dataclass(frozen=True, slots=True)
class RouteContract:
    """A route's doorplate: what clients may rely on at this boundary.

    Declared with the defaults most routes need (idempotent, data-plane),
    so a route author writes only the exceptions — the non-default
    idempotency, the exemption, and the reason that used to live in
    middleware comments. ``transactional_idempotency`` means the handler owns
    the key in the same commit as its business effect; generic response-cache
    middleware must then stay out of the crash-sensitive interval.
    """

    idempotency: Idempotency = Idempotency.IDEMPOTENT
    pause: PauseSemantics = PauseSemantics.DATA_PLANE
    note: str = ""
    transactional_idempotency: bool = False
    # True only for keyed routes already protected by the rollout baseline.
    # Newly protected routes need positive server negotiation before callers
    # may automatically retry an ambiguous outcome against a mixed-version gateway.
    legacy_keyed_retry: bool = False


# ─────────────────────────────────────────────────────────────────────
# The doorplate wall: every gateway route declares itself here, grouped by
# router file. Key = (HTTP method, FastAPI path template). Lint forces
# every app route to have an entry and every entry to be used.
#
# Pause-exemption audit: CONTROL_PLANE routes are exactly the surfaces
# that must stay reachable mid-migration — the /api/cluster/* control
# plane, the Grafana alerting webhook (a 503 inside the rollout window
# exhausts Grafana's webhook retries and the alert is lost exactly when
# alerting matters most), and the bootstrap config read a held runner's
# start needs. Everything else is data-plane.
# ─────────────────────────────────────────────────────────────────────
ROUTE_CONTRACTS: dict[tuple[str, str], RouteContract] = {
    # ── gateway/agents/router.py ───────────────────────────────────
    ("PATCH", "/api/agents/{agent_id}"): RouteContract(note="label patch — CAS update"),
    ("GET", "/api/models"): RouteContract(),
    ("GET", "/api/agents"): RouteContract(note="bounded agent directory page"),
    ("GET", "/api/agents/roster"): RouteContract(
        note="live agent cards and required ancestor links in one snapshot"
    ),
    ("POST", "/api/agents"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="spawn — keyed identity commits with birth and first prompt",
        transactional_idempotency=True,
    ),
    ("POST", "/api/agents/{agent_id}/retry-launch"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="explicit same-ID launch retry rotates the attempt key; each call dispatches once",
    ),
    ("GET", "/api/agents/{agent_id}"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/born-chain"): RouteContract(),
    # ── gateway/alerts/router.py ───────────────────────────────────
    ("GET", "/api/alerts"): RouteContract(),
    ("GET", "/api/alerts/stream"): RouteContract(
        note="SSE tail — subscribing mid-pause just idles; the UI's initial fetch covers the gap"
    ),
    ("POST", "/api/alerts"): RouteContract(
        pause=PauseSemantics.CONTROL_PLANE,
        note="Grafana Alertmanager webhook — upsert per (fingerprint, starts_at); a 503 exhausts Grafana retries and the alert is lost",
    ),
    # ── gateway/auth/router.py ───────────────────────────────────
    ("POST", "/api/auth/login"): RouteContract(note="login — repeats just mint a fresh cookie"),
    ("POST", "/api/auth/logout"): RouteContract(note="clear session cookie — idempotent"),
    ("GET", "/api/auth/check"): RouteContract(),
    ("GET", "/api/auth/sessions"): RouteContract(),
    ("POST", "/api/auth/sessions/{session_id}/revoke"): RouteContract(
        note="session revocation — guarded update; repeats cannot revoke twice"
    ),
    # ── gateway/cluster/bootstrap.py ───────────────────────────────────
    ("GET", "/api/bootstrap"): RouteContract(
        pause=PauseSemantics.CONTROL_PLANE,
        note="read-only config projection, still authenticated — a held unit's first start "
        "and every runner process's config resolution read it before the hold is released",
    ),
    # ── gateway/cluster/router.py ───────────────────────────────────
    ("POST", "/api/cluster/stopping"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        PauseSemantics.CONTROL_PLANE,
        note="control-plane op — host self-report during stop",
    ),
    ("GET", "/api/cluster/status"): RouteContract(
        pause=PauseSemantics.CONTROL_PLANE, note="control-plane read — observability during rollout"
    ),
    ("GET", "/api/cluster/roster"): RouteContract(
        pause=PauseSemantics.CONTROL_PLANE, note="control-plane read — observability during rollout"
    ),
    ("GET", "/api/cluster/admin/events"): RouteContract(
        pause=PauseSemantics.CONTROL_PLANE, note="control-plane read — observability during rollout"
    ),
    ("GET", "/api/cluster/machines"): RouteContract(
        pause=PauseSemantics.CONTROL_PLANE, note="control-plane read — observability during rollout"
    ),
    ("DELETE", "/api/cluster/machines/{name}"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        PauseSemantics.CONTROL_PLANE,
        note="control-plane op — deregisters a machine",
    ),
    ("POST", "/api/cluster/machines/{name}/staging"): RouteContract(
        Idempotency.IDEMPOTENT,
        PauseSemantics.CONTROL_PLANE,
        note="control-plane op — operator sets/clears the staging flag",
    ),
    ("POST", "/api/cluster/machines/{name}/pause"): RouteContract(
        Idempotency.IDEMPOTENT,
        PauseSemantics.CONTROL_PLANE,
        note="control-plane op — drains tasks, terminates the machine's agents, "
        "sets the pause latch (idempotent: re-pause is a safe no-op)",
    ),
    ("POST", "/api/cluster/machines/{name}/resume"): RouteContract(
        Idempotency.IDEMPOTENT,
        PauseSemantics.CONTROL_PLANE,
        note="control-plane op — clears the pause latch (idempotent no-op when not paused)",
    ),
    # ── gateway/routers/commands.py ───────────────────────────────────
    ("GET", "/api/commands"): RouteContract(),
    # ── gateway/routers/config.py ───────────────────────────────────
    ("GET", "/api/config"): RouteContract(),
    ("GET", "/api/config/resolved"): RouteContract(),
    ("GET", "/api/config/audit"): RouteContract(),
    ("PUT", "/api/config"): RouteContract(note="full config replace — PUT is idempotent"),
    ("GET", "/api/config/default-model"): RouteContract(),
    ("PUT", "/api/config/default-model"): RouteContract(
        note="set default model — PUT is idempotent"
    ),
    # ── gateway/agents/conversation.py ─────────────────────────────
    ("GET", "/api/agents/{agent_id}/conversation-snapshot"): RouteContract(
        note="composed switch refresh — timeline head window + token usage + pending in one read"
    ),
    # ── gateway/events/router.py ───────────────────────────────────
    ("GET", "/api/agents/{agent_id}/events/stream"): RouteContract(note="SSE live stream"),
    ("GET", "/api/agents/{agent_id}/events"): RouteContract(),
    ("GET", "/api/computer/traces"): RouteContract(
        note="computer-use task replay (Phase 3, task #1101): one task's desktop trail"
    ),
    ("GET", "/api/events"): RouteContract(),
    # ── gateway/events/resolutions.py ────────────────────────
    ("GET", "/api/event-resolutions"): RouteContract(),
    ("POST", "/api/event-resolutions"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="create class dismissal — INSERT; retries receive a conflict rather than a replay",
    ),
    ("POST", "/api/event-resolutions/{dismissal_id}/reopen"): RouteContract(
        note="guarded active-state transition — repeats cannot reopen twice"
    ),
    # ── gateway/routers/fleet_graph.py ───────────────────────────────────
    ("GET", "/api/fleet/graph"): RouteContract(),
    # ── gateway/routers/frontend_telemetry.py ─────────────────────────
    ("POST", "/api/frontend-telemetry"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="telemetry ingest — pure INSERT per event; a retry duplicates rows (client never retries)",
    ),
    # ── gateway/routers/grafana.py ───────────────────────────────────
    ("GET", "/grafana"): RouteContract(note="reverse proxy — semantics follow upstream"),
    ("GET", "/grafana/{rest:path}"): RouteContract(
        note="reverse proxy — semantics follow upstream"
    ),
    ("POST", "/grafana/{rest:path}"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="reverse proxy — semantics follow upstream"
    ),
    ("PATCH", "/grafana/{rest:path}"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="reverse proxy — semantics follow upstream"
    ),
    ("DELETE", "/grafana/{rest:path}"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="reverse proxy — semantics follow upstream"
    ),
    ("PUT", "/grafana/{rest:path}"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="reverse proxy — semantics follow upstream"
    ),
    # ── gateway/routers/guide.py ───────────────────────────────────
    ("POST", "/api/guide/draft"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="LLM generation incurs a fresh external request and token cost",
    ),
    # ── gateway/cluster/status.py (health) ───────────────────────────────────
    ("GET", "/api/health"): RouteContract(
        pause=PauseSemantics.CONTROL_PLANE,
        note="gateway identity and database liveness — start must prove readiness before releasing its hold",
    ),
    # ── gateway/inspect/router.py ───────────────────────────────────
    ("GET", "/api/agents/{agent_id}/inspect/statistics"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/inspect/live"): RouteContract(
        note="uncached live skeleton — cheap window-independent inspector fields",
    ),
    ("GET", "/api/agents/{agent_id}/inspect/metrics"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/inspect/widgets"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/neighbors"): RouteContract(),
    # ── gateway/extensions/inventory.py ───────────────────────────────────
    ("GET", "/api/inventory"): RouteContract(),
    ("PUT", "/api/inventory"): RouteContract(note="full inventory replace — PUT is idempotent"),
    # ── gateway/agents/lifecycle.py ───────────────────────────
    ("POST", "/api/agents/{agent_id}/impersonation/force-expire"): RouteContract(
        note="observed-session CAS close — repeated or stale requests leave the lease unchanged"
    ),
    ("POST", "/api/agents/{agent_id}/compact"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="each request enqueues a new compact command; no durable command receipt",
    ),
    ("POST", "/api/cancel"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="each request enqueues cancel; a delayed retry can cancel later work",
    ),
    ("POST", "/api/agents/{agent_id}/terminate"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="termination is not bound to the observed incarnation"
    ),
    ("POST", "/api/agents/{agent_id}/resurrect"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="resurrection is not bound to the observed incarnation"
    ),
    ("POST", "/api/agents/resurrect-billing"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="billing resurrection has no durable operation receipt; preview alone is read-only",
    ),
    ("POST", "/api/agents/{agent_id}/restart"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="a repeat dispatches another restart; HTTP intent has no durable receipt",
    ),
    # ── gateway/agents/understanding.py ────────────────────────────────────
    ("POST", "/api/agents/{agent_id}/understanding/close"): RouteContract(
        note="plans one closing job from stored state; a repeat finds the active job or an empty stretch"
    ),
    # ── gateway/mcp_server/router.py ─────────────────────────────
    ("GET", "/api/mcp/clients"): RouteContract(),
    ("POST", "/api/mcp/clients"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="client creation — plaintext token is revealed once",
    ),
    ("POST", "/api/mcp/clients/{client_id}/revoke"): RouteContract(
        note="client revocation — guarded update; repeats cannot revoke twice"
    ),
    # ── gateway/routers/memory.py ───────────────────────────────────
    ("GET", "/api/memory/graph"): RouteContract(),
    ("GET", "/api/memory/note"): RouteContract(note="pure read — one note by relative path"),
    ("GET", "/api/memory/pool"): RouteContract(
        note="pure read — consolidated pool git bundle for split-runner bootstrap"
    ),
    ("POST", "/api/memory/refresh"): RouteContract(note="re-scan — repeats are harmless"),
    ("POST", "/api/memory/search"): RouteContract(note="pure read"),
    # ── gateway/events/metrics.py ───────────────────────────────────
    ("GET", "/api/metrics"): RouteContract(),
    ("GET", "/api/metrics/agents"): RouteContract(),
    # ── gateway/cluster/ops_monitor.py ───────────────────────────────────
    ("GET", "/api/ops/monitor"): RouteContract(),
    # ── gateway/agents/notices.py ───────────────────────────────────
    ("GET", "/api/notices"): RouteContract(
        note="unified inbox feed — open + awaiting + resolved page (R4 layer 2)"
    ),
    ("GET", "/api/notices/live"): RouteContract(),
    ("GET", "/api/notices/open"): RouteContract(),
    ("POST", "/api/agents/{agent_id}/notices/{notice_id}/resolve"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        transactional_idempotency=True,
        note="notice resolution and reply share a durable keyed receipt; keyless legacy reads with reply remain distinct",
    ),
    ("POST", "/api/agents/{agent_id}/notices"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        transactional_idempotency=True,
        note="notice creation and superseding effects share one keyed transaction; replays return the original notice result",
    ),
    ("PATCH", "/api/agents/{agent_id}/notices/current"): RouteContract(
        note="edit current open notice — repeats are harmless"
    ),
    ("POST", "/api/agents/{agent_id}/notices/current/dismiss"): RouteContract(
        note="withdraw current open notice — CAS, repeats are harmless"
    ),
    # ── gateway/routers/okf_graph.py ───────────────────────────────────
    ("GET", "/api/okf/graph"): RouteContract(),
    # ── gateway/extensions/packages.py ───────────────────────────────────
    ("POST", "/api/packages/draft"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="LLM generation incurs a fresh external request and token cost",
    ),
    # ── gateway/routers/pages.py ───────────────────────────────────
    ("GET", "/api/pages"): RouteContract(),
    ("GET", "/pages/{page_key}"): RouteContract(note="page reverse proxy"),
    ("GET", "/pages/{page_key}/{rest:path}"): RouteContract(note="page reverse proxy"),
    ("POST", "/api/agents/{agent_id}/pages"): RouteContract(
        note="register page — upsert, repeats are harmless"
    ),
    ("DELETE", "/api/agents/{agent_id}/pages/{name}"): RouteContract(
        note="close page — CAS, repeats are harmless"
    ),
    ("GET", "/api/agents/{agent_id}/pages"): RouteContract(note="list open pages"),
    # ── gateway/extensions/plugin_ui.py ───────────────────────────────────
    ("GET", "/api/plugin-ui/{plugin}"): RouteContract(
        note="plugin page mount — trailing-slash redirect"
    ),
    ("GET", "/api/plugin-ui/{plugin}/{rest:path}"): RouteContract(
        note="plugin page mount — static read"
    ),
    # ── gateway/routers/presets.py ───────────────────────────────────
    ("GET", "/api/presets"): RouteContract(),
    ("GET", "/api/presets/{preset_id}"): RouteContract(),
    ("POST", "/api/presets"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="create preset — pure INSERT; a retry duplicates the row"
    ),
    ("PATCH", "/api/presets/{preset_id}"): RouteContract(note="update — repeats are harmless"),
    ("DELETE", "/api/presets/{preset_id}"): RouteContract(note="delete — repeats are harmless"),
    # ── gateway/schedules/router.py ───────────────────────────────────
    ("GET", "/api/schedules"): RouteContract(),
    ("GET", "/api/schedules/{schedule_id}"): RouteContract(),
    ("GET", "/api/schedules/{schedule_id}/logs"): RouteContract(),
    ("GET", "/api/schedules/{schedule_id}/runs"): RouteContract(),
    ("POST", "/api/schedules"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="create schedule — pure INSERT; a retry duplicates the row"
    ),
    ("POST", "/api/schedules/draft"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="LLM generation incurs a fresh external request and token cost",
    ),
    ("POST", "/api/schedules/{schedule_id}/start"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="schedule control enqueues a fresh sync; no durable command receipt",
    ),
    ("POST", "/api/schedules/{schedule_id}/stop"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="schedule control enqueues a fresh sync; no durable command receipt",
    ),
    ("POST", "/api/schedules/{schedule_id}/restart"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="schedule control enqueues a fresh sync; no durable command receipt",
    ),
    ("PUT", "/api/schedules/{schedule_id}"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="script edits append versions and can restart the process"
    ),
    ("DELETE", "/api/schedules/{schedule_id}"): RouteContract(note="delete — repeats are harmless"),
    # ── gateway/routers/settings.py ───────────────────────────────────
    ("GET", "/api/settings"): RouteContract(),
    ("PUT", "/api/settings/{key}"): RouteContract(note="set one key — PUT is idempotent"),
    # ── gateway/routers/shell.py ───────────────────────────────────
    ("GET", "/api/agents/{agent_id}/shell/{session_id}"): RouteContract(),
    # ── gateway/extensions/skills.py ───────────────────────────────────
    ("GET", "/api/skills"): RouteContract(),
    ("PUT", "/api/skills"): RouteContract(note="full replace — PUT is idempotent"),
    # ── gateway/agents/state.py ───────────────────────────────────
    ("GET", "/api/agents/{agent_id}/messages"): RouteContract(
        note="raw checkpoint history — implicit requests return newest 100; start_index pages backward"
    ),
    ("GET", "/api/agents/{agent_id}/traces/{trace_id}/messages"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/last-message"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/pending"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/token-usage"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/context-breakdown"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/system"): RouteContract(),
    ("POST", "/api/agents/{agent_id}/messages"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="enqueue chat inbound — one logical message must land exactly once; clients retry with an Idempotency-Key",
        transactional_idempotency=True,
        legacy_keyed_retry=True,
    ),
    ("POST", "/api/agents/{agent_id}/messages/reconcile"): RouteContract(
        note="idempotent receipt recovery — heals the pending wake/resurrection tail for an uncertain same-key delivery"
    ),
    ("POST", "/api/agents/{agent_id}/system-note"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        transactional_idempotency=True,
        note="deliver a framework system note (task assign/update/reminder) — renders as a system marker, not peer chat; resurrect is a body choice",
    ),
    # ── gateway/cluster/status.py ───────────────────────────────────
    ("GET", "/api/stats/dashboard"): RouteContract(),
    # ── gateway/cluster/alert_classes.py ────────────────────────────
    ("GET", "/api/stats/alert-classes"): RouteContract(),
    ("GET", "/api/stats/alert-classes/samples"): RouteContract(),
    ("GET", "/api/status"): RouteContract(),
    ("GET", "/api/system"): RouteContract(),
    ("GET", "/api/system/all"): RouteContract(),
    # ── gateway/routers/tasks.py ───────────────────────────────────
    ("GET", "/api/tasks"): RouteContract(),
    ("PATCH", "/api/tasks/{task_id}"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="task effect and notification share a transaction; request has no replay receipt",
    ),
    # ── gateway/agents/timeline.py ───────────────────────────────────
    ("GET", "/api/agents/{agent_id}/timeline"): RouteContract(),
    # ── gateway/run_timeline/router.py, messages.py ──────────────────
    ("GET", "/api/agents/{agent_id}/run-timeline"): RouteContract(
        note="read-only: the understanding tree and the layer-0 message units in a window",
    ),
    ("GET", "/api/agents/{agent_id}/run-timeline/messages"): RouteContract(
        note="read-only raw messages of a stitched-index range — long parts clip with text_truncated; full=true returns the whole body",
    ),
    # ── gateway/extensions/ui_contributions.py ───────────────────────────────────
    ("GET", "/api/ui/contributions"): RouteContract(),
    # ── gateway/routers/uploads.py ───────────────────────────────────
    ("GET", "/api/agents/{agent_id}/uploads/{filename}"): RouteContract(),
    ("POST", "/api/agents/{agent_id}/uploads"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="save files to disk + enqueue inbound; a retry duplicates files",
    ),
}


# ── template matching (shared by middleware + SDK) ────────────────────

_TEMPLATE_PATH_PARAM = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*:path\}")
_TEMPLATE_PARAM = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")


@lru_cache(maxsize=512)
def _template_regex(template: str) -> re.Pattern[str]:
    """Compile a FastAPI path template to an anchored regex.

    ``{param}`` matches one segment; ``{param:path}`` matches the rest
    (slashes included). Cached — the pause middleware matches on every
    request.
    """
    pattern = _TEMPLATE_PATH_PARAM.sub(".*", template)
    pattern = _TEMPLATE_PARAM.sub(r"[^/]+", pattern)
    return re.compile("^" + pattern + "$")


def match_path(template: str, path: str) -> bool:
    """Whether a concrete request ``path`` matches a route ``template``."""
    return _template_regex(template).match(path) is not None


# ── route lookup (audit gateway.md P2-14) ─────────────────────────────
#
# contract_for / exempt_from_pause run on every request, twice (pause +
# idempotency middlewares), and a compiled-regex match per ROUTE_CONTRACTS
# entry (~120) is the cost to avoid. Most templates share their second path
# segment with the concrete paths they match (`/api/agents/{agent_id}`
# matches paths whose second segment is ``agents``), so only the templates
# of that segment are regex-matched, then the few wildcard templates (a
# `{...:path}` segment in the first two slots, which can match any path)
# last. The segment filter is a string split, cheap next to a regex match,
# so nothing is indexed or cached.


def _index_bucket_key(template: str) -> str | None:
    """The second path segment of a template, or None when the template can
    match paths of any second segment (a `{...:path}` wildcard in the first
    two slots)."""
    parts = template.strip("/").split("/")
    if len(parts) < 2 or ":path}" in parts[0] or ":path}" in parts[1]:
        return None
    return parts[1]


def _contract_candidates(method: str | None, path: str) -> list[tuple[str, RouteContract]]:
    """The (template, contract) pairs that could match ``path``: templates sharing its
    second segment first, then the wildcard templates. `method` None = any method
    (exempt_from_pause does not filter by method)."""
    bucket = _index_bucket_key(path)
    keyed = [
        (tpl, contract, _index_bucket_key(tpl))
        for (m, tpl), contract in ROUTE_CONTRACTS.items()
        if method is None or m == method
    ]
    candidates: list[tuple[str, RouteContract]] = []
    if bucket is not None:
        candidates.extend((tpl, c) for tpl, c, key in keyed if key == bucket)
    candidates.extend((tpl, c) for tpl, c, key in keyed if key is None)
    return candidates


def contract_for(method: str, path: str) -> RouteContract | None:
    """The contract for a concrete request (``method`` + actual ``path``),
    or None when no route declares it.

    Used by the SDK to inherit retry behavior: the caller passes the
    actual request path; the contract's idempotency says what a retry may
    do.
    """
    for tpl, contract in _contract_candidates(method, path):
        if match_path(tpl, path):
            return contract
    return None


def idempotency_for(method: str, path: str) -> Idempotency:
    """The idempotency semantics a caller must honor for a request.

    Unknown routes default to NON_IDEMPOTENT: a route that was not declared
    (or a path the SDK misspelled) must never be blindly retried — a retry
    could duplicate the side effect of a POST the doorplate never promised
    to dedup (#698 spawn-duplicate class). Lint forces every gateway route
    to be declared, so an unknown path is a caller bug, and the safe
    default for a bug is "do not auto-retry".
    """
    c = contract_for(method, path)
    return c.idempotency if c is not None else Idempotency.NON_IDEMPOTENT


def exempt_from_pause(method: str, path: str) -> bool:
    """Whether ``(method, path)`` is declared control-plane (survives a
    migration).

    The single predicate behind `gateway.middleware.pause_policy.should_bypass_pause`
    — kept here so every consumer shares one matching implementation. The
    method matters: two methods can share one path template with different
    pause semantics (POST /api/alerts is the control-plane webhook while
    GET /api/alerts is ordinary data-plane).
    """
    return any(
        c.pause is PauseSemantics.CONTROL_PLANE and match_path(tpl, path)
        for tpl, c in _contract_candidates(method, path)
    )
