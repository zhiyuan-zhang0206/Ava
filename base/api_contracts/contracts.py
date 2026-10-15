# ava:ref base/api_contracts/docs/routes.ava.okf.md#route-policy
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Idempotency(StrEnum):
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"
    AT_LEAST_ONCE_WITH_KEY = "at_least_once_with_key"


class PauseSemantics(StrEnum):
    CONTROL_PLANE = "control-plane"
    DATA_PLANE = "data-plane"


@dataclass(frozen=True, slots=True)
class RouteContract:
    idempotency: Idempotency = Idempotency.IDEMPOTENT
    pause: PauseSemantics = PauseSemantics.DATA_PLANE
    note: str = ""


ROUTE_CONTRACTS: dict[tuple[str, str], RouteContract] = {
    ("GET", "/api/keyed/v1/agents/{agent_id}/compact-target"): RouteContract(),
    ("GET", "/api/keyed/v1/agents/{agent_id}/compact-commands/{command_id}"): RouteContract(),
    ("POST", "/api/keyed/v1/agents/{agent_id}/compact-history"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="original source receipt; durable prepared result and cold application proof are separate",
    ),
    ("GET", "/api/keyed/v1/agents/{agent_id}/native-work"): RouteContract(),
    ("GET", "/api/keyed/v1/agents/{agent_id}/restart-commands/{command_id}"): RouteContract(),
    ("POST", "/api/keyed/v1/agents/{agent_id}/restart-work"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="original ACTIVE restart acceptance and exact lifecycle execution proof",
    ),
    ("POST", "/api/keyed/v1/agents/{agent_id}/cancel-work"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="original managed work receipt and checkpoint/resource execution proof",
    ),
    ("PATCH", "/api/agents/{agent_id}"): RouteContract(note="label patch — CAS update"),
    ("GET", "/api/models"): RouteContract(),
    ("GET", "/api/agents"): RouteContract(note="bounded agent directory page"),
    ("GET", "/api/agents/roster"): RouteContract(
        note="live agent cards and required ancestor links in one snapshot"
    ),
    ("POST", "/api/keyed/v1/agents/{agent_id}/retry-launch"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="guarded observed-attempt retry; receipt and pointer commit together",
    ),
    ("POST", "/api/keyed/v1/agents"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="guarded plain creation requires a verified principal-v1 key; no legacy fallback",
    ),
    ("POST", "/api/keyed/v1/task-assignments"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="guarded atomic task/agent acceptance; principal-v1 key and no legacy fallback",
    ),
    ("GET", "/api/agents/{agent_id}"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/born-chain"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/to-user"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/to-user/resource"): RouteContract(),
    ("GET", "/api/alerts"): RouteContract(),
    ("GET", "/api/alerts/stream"): RouteContract(
        note="SSE tail — subscribing mid-pause just idles; the UI's initial fetch covers the gap"
    ),
    ("POST", "/api/alerts"): RouteContract(
        pause=PauseSemantics.CONTROL_PLANE,
        note="Grafana Alertmanager webhook — upsert per (fingerprint, starts_at); a 503 exhausts Grafana retries and the alert is lost",
    ),
    ("POST", "/api/auth/login"): RouteContract(note="login — repeats just mint a fresh cookie"),
    ("POST", "/api/auth/login-ticket"): RouteContract(
        note="issue a one-time login ticket — every repeat is a distinct short-lived row"
    ),
    ("POST", "/api/auth/login-ticket/redeem"): RouteContract(
        note="spend a login ticket — the atomic revoke makes a repeat a rejected replay"
    ),
    ("POST", "/api/auth/logout"): RouteContract(note="clear session cookie — idempotent"),
    ("GET", "/api/auth/check"): RouteContract(),
    ("GET", "/api/auth/sessions"): RouteContract(),
    ("POST", "/api/auth/sessions/{session_id}/revoke"): RouteContract(
        note="session revocation — guarded update; repeats cannot revoke twice"
    ),
    ("GET", "/api/bootstrap"): RouteContract(
        pause=PauseSemantics.CONTROL_PLANE,
        note="read-only config projection, still authenticated — a held unit's first start "
        "and every runner process's config resolution read it before the hold is released",
    ),
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
    ("GET", "/api/commands"): RouteContract(),
    ("GET", "/api/config"): RouteContract(),
    ("GET", "/api/config/resolved"): RouteContract(),
    ("GET", "/api/config/audit"): RouteContract(),
    ("PUT", "/api/config"): RouteContract(note="full config replace — PUT is idempotent"),
    ("GET", "/api/config/default-model"): RouteContract(),
    ("PUT", "/api/config/default-model"): RouteContract(
        note="set default model — PUT is idempotent"
    ),
    ("GET", "/api/agents/{agent_id}/conversation-snapshot"): RouteContract(
        note="composed switch refresh — timeline head window + token usage + pending in one read"
    ),
    ("GET", "/api/agents/{agent_id}/events/stream"): RouteContract(note="SSE live stream"),
    ("GET", "/api/agents/{agent_id}/events"): RouteContract(),
    ("GET", "/api/computer/traces"): RouteContract(
        note="computer-use task replay (Phase 3, task #1101): one task's desktop trail"
    ),
    ("GET", "/api/events"): RouteContract(),
    ("GET", "/api/event-resolutions"): RouteContract(),
    ("POST", "/api/event-resolutions"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="create class dismissal — INSERT; retries receive a conflict rather than a replay",
    ),
    ("POST", "/api/event-resolutions/{dismissal_id}/reopen"): RouteContract(
        note="guarded active-state transition — repeats cannot reopen twice"
    ),
    ("GET", "/api/fleet/graph"): RouteContract(),
    ("POST", "/api/frontend-telemetry"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="telemetry ingest — pure INSERT per event; a retry duplicates rows (client never retries)",
    ),
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
    ("POST", "/api/keyed/v1/guide/draft"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="raw draft identity and original immutable birth snapshot",
    ),
    ("GET", "/api/health"): RouteContract(
        pause=PauseSemantics.CONTROL_PLANE,
        note="gateway identity and database liveness — start must prove readiness before releasing its hold",
    ),
    ("GET", "/api/agents/{agent_id}/inspect/statistics"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/inspect/live"): RouteContract(
        note="uncached live skeleton — cheap window-independent inspector fields",
    ),
    ("GET", "/api/agents/{agent_id}/inspect/metrics"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/inspect/widgets"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/neighbors"): RouteContract(),
    ("GET", "/api/inventory"): RouteContract(),
    ("PUT", "/api/inventory"): RouteContract(note="full inventory replace — PUT is idempotent"),
    ("POST", "/api/agents/{agent_id}/impersonation/force-expire"): RouteContract(
        note="observed-session CAS close — repeated or stale requests leave the lease unchanged"
    ),
    ("POST", "/api/agents/{agent_id}/terminate"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="termination is not bound to the observed incarnation"
    ),
    ("POST", "/api/agents/{agent_id}/resurrect"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="resurrection is not bound to the observed incarnation"
    ),
    ("POST", "/api/agents/{agent_id}/restart"): RouteContract(
        Idempotency.NON_IDEMPOTENT,
        note="a repeat dispatches another restart; HTTP intent has no durable receipt",
    ),
    ("GET", "/api/agents/{agent_id}/sessions"): RouteContract(
        note="read-only: the agent's sessions with their understanding coverage and build estimate"
    ),
    ("POST", "/api/agents/{agent_id}/understanding/build"): RouteContract(
        note="chunk jobs and the pending rebuild merge into live ones; a repeat only records another build (dry_run writes nothing)"
    ),
    ("GET", "/api/agents/{agent_id}/understanding/builds/{build_id}"): RouteContract(
        note="read-only: a build's job statuses, rebuild state and actual cost"
    ),
    ("GET", "/api/mcp/clients"): RouteContract(),
    ("POST", "/api/keyed/v1/mcp/clients"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="guarded credential creation; replays recover metadata, never the one-time token",
    ),
    ("POST", "/api/mcp/clients/{client_id}/revoke"): RouteContract(
        note="client revocation — guarded update; repeats cannot revoke twice"
    ),
    ("GET", "/api/memory/graph"): RouteContract(),
    ("GET", "/api/memory/note"): RouteContract(note="pure read — one note by relative path"),
    ("GET", "/api/memory/pool"): RouteContract(
        note="pure read — consolidated pool git bundle for split-runner bootstrap"
    ),
    ("POST", "/api/memory/refresh"): RouteContract(note="re-scan — repeats are harmless"),
    ("POST", "/api/memory/search"): RouteContract(note="pure read"),
    ("GET", "/api/metrics"): RouteContract(),
    ("GET", "/api/metrics/agents"): RouteContract(),
    ("GET", "/api/ops/monitor"): RouteContract(),
    ("GET", "/api/notices"): RouteContract(
        note="unified inbox feed — open + awaiting + resolved page (R4 layer 2)"
    ),
    ("GET", "/api/notices/live"): RouteContract(),
    ("GET", "/api/notices/open"): RouteContract(),
    ("POST", "/api/keyed/v1/agents/{agent_id}/notices/{notice_id}/resolve"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="required verified principal key resolves only the explicit global notice and replays its original inbound",
    ),
    ("POST", "/api/agents/{agent_id}/notices"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="notice creation and superseding effects share one keyed transaction; replays return the original notice result",
    ),
    ("PATCH", "/api/agents/{agent_id}/notices/current/guarded-v1"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="observed global notice edit and immutable acceptance commit together; no client activation",
    ),
    ("POST", "/api/agents/{agent_id}/notices/current/dismiss/guarded-v1"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="withdraw only the observed global notice; keyed replay never selects a later notice",
    ),
    ("GET", "/api/okf/graph"): RouteContract(),
    ("POST", "/api/keyed/v1/packages/draft"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="raw draft identity and original immutable birth snapshot",
    ),
    ("GET", "/api/pages"): RouteContract(),
    ("GET", "/pages/{page_key}"): RouteContract(note="page reverse proxy"),
    ("GET", "/pages/{page_key}/{rest:path}"): RouteContract(note="page reverse proxy"),
    ("POST", "/api/keyed/v1/agents/{agent_id}/pages"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="versioned page registration commits replacement and original acceptance together",
    ),
    ("POST", "/api/keyed/v1/agents/{agent_id}/pages/{name}/close"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="versioned page close requires observed row identity; replay cannot close a later page",
    ),
    ("GET", "/api/agents/{agent_id}/pages"): RouteContract(note="list open pages"),
    ("GET", "/api/plugin-ui/{plugin}"): RouteContract(
        note="plugin page mount — trailing-slash redirect"
    ),
    ("GET", "/api/plugin-ui/{plugin}/{rest:path}"): RouteContract(
        note="plugin page mount — static read"
    ),
    ("GET", "/api/presets"): RouteContract(),
    ("GET", "/api/presets/{preset_id}"): RouteContract(),
    ("POST", "/api/presets"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="create preset — pure INSERT; a retry duplicates the row"
    ),
    ("PATCH", "/api/presets/{preset_id}"): RouteContract(note="update — repeats are harmless"),
    ("DELETE", "/api/presets/{preset_id}"): RouteContract(note="delete — repeats are harmless"),
    ("GET", "/api/schedules"): RouteContract(),
    ("GET", "/api/schedules/{schedule_id}"): RouteContract(),
    ("GET", "/api/schedules/{schedule_id}/logs"): RouteContract(),
    ("GET", "/api/schedules/{schedule_id}/runs"): RouteContract(),
    ("POST", "/api/schedules"): RouteContract(
        Idempotency.NON_IDEMPOTENT, note="create schedule — pure INSERT; a retry duplicates the row"
    ),
    ("POST", "/api/keyed/v1/schedules/draft"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="raw draft identity and original immutable birth snapshot",
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
    ("GET", "/api/settings"): RouteContract(),
    ("PUT", "/api/settings/{key}"): RouteContract(note="set one key — PUT is idempotent"),
    ("GET", "/api/agents/{agent_id}/shell/{session_id}"): RouteContract(),
    ("GET", "/api/skills"): RouteContract(),
    ("PUT", "/api/skills"): RouteContract(note="full replace — PUT is idempotent"),
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
    ),
    ("POST", "/api/agents/{agent_id}/messages/reconcile"): RouteContract(
        note="idempotent receipt recovery — heals the pending wake/resurrection tail for an uncertain same-key delivery"
    ),
    ("POST", "/api/agents/{agent_id}/system-note"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="deliver a framework system note (task assign/update/reminder) — renders as a system marker, not peer chat; resurrect is a body choice",
    ),
    ("GET", "/api/stats/dashboard"): RouteContract(),
    ("GET", "/api/stats/alert-classes"): RouteContract(),
    ("GET", "/api/stats/alert-classes/samples"): RouteContract(),
    ("GET", "/api/status"): RouteContract(),
    ("GET", "/api/system"): RouteContract(),
    ("GET", "/api/system/all"): RouteContract(),
    ("GET", "/api/tasks"): RouteContract(),
    ("PATCH", "/api/tasks/{task_id}"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="task patch requires a key; immutable result shares the notification transaction",
    ),
    ("GET", "/api/agents/{agent_id}/timeline"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/timeline/retained"): RouteContract(
        note="read-only retained boundary window, independent of the live checkpoint head"
    ),
    ("POST", "/api/agents/{agent_id}/reply-suggestion"): RouteContract(
        note="read-only draft: a decision gate and a chat model call, writing only llm_usage; completed results are cached per agent message",
    ),
    ("GET", "/api/agents/{agent_id}/run-timeline"): RouteContract(
        note="read-only: the understanding tree and the layer-0 message units in a window",
    ),
    ("GET", "/api/agents/{agent_id}/run-timeline/context"): RouteContract(
        note="read-only context breakdown of the LLM request at (or after) a message index — recomputed from the checkpoint, no state change",
    ),
    ("GET", "/api/agents/{agent_id}/run-timeline/messages"): RouteContract(
        note="read-only raw messages of a stitched-index range — long parts clip with text_truncated; full=true returns the whole body",
    ),
    ("GET", "/api/insights/run-timeline/links"): RouteContract(
        note="read-only: agent-to-agent audit events (chat messages, spawn, fork, terminate, restart, resurrect) with an end among the asked agents",
    ),
    ("GET", "/api/insights/run-timeline/link-content"): RouteContract(
        note="read-only: the full text of one chat message or notice behind a selected arrow",
    ),
    ("GET", "/api/insights/{rest:path}"): RouteContract(
        note="read-only: proxied untouched to the insights service, which builds every insights read from the record",
    ),
    ("GET", "/api/ui/contributions"): RouteContract(),
    ("GET", "/api/agents/{agent_id}/uploads/{filename}"): RouteContract(),
    ("POST", "/api/keyed/v1/agents/{agent_id}/uploads"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="immutable source acceptance and delivery intent; native receiver and one retained inbound recover together; no client activation",
    ),
    ("GET", "/api/keyed/v1/agents/{agent_id}/uploads/{batch_id}"): RouteContract(),
    (
        "GET",
        "/api/keyed/v1/agents/{agent_id}/uploads/{batch_id}/objects/{ordinal}",
    ): RouteContract(),
    ("POST", "/api/agents/{agent_id}/uploads"): RouteContract(
        Idempotency.AT_LEAST_ONCE_WITH_KEY,
        note="silent save only: the claimed manifest is published as immutable objects and the original receipt replays under the same key; no inbound is queued",
    ),
}


_TEMPLATE_PATH_PARAM = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*:path\}")
_TEMPLATE_PARAM = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")


def _template_regex(template: str) -> re.Pattern[str]:

    pattern = _TEMPLATE_PATH_PARAM.sub(".*", template)
    pattern = _TEMPLATE_PARAM.sub(r"[^/]+", pattern)
    return re.compile("^" + pattern + "$")


def match_path(template: str, path: str) -> bool:

    return _template_regex(template).match(path) is not None


def _index_bucket_key(template: str) -> str | None:

    parts = template.strip("/").split("/")
    if len(parts) < 2 or ":path}" in parts[0] or ":path}" in parts[1]:
        return None
    return parts[1]


def _contract_candidates(method: str | None, path: str) -> list[tuple[str, RouteContract]]:

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

    for tpl, contract in _contract_candidates(method, path):
        if match_path(tpl, path):
            return contract
    return None


def idempotency_for(method: str, path: str) -> Idempotency:

    c = contract_for(method, path)
    return c.idempotency if c is not None else Idempotency.NON_IDEMPOTENT


def exempt_from_pause(method: str, path: str) -> bool:

    return any(
        c.pause is PauseSemantics.CONTROL_PLANE and match_path(tpl, path)
        for tpl, c in _contract_candidates(method, path)
    )
