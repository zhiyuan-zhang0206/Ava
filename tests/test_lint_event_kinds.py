"""Event-registry zero-enforcement gate — R2-C \u5355\u4e00\u4e8b\u5b9e\u6e90\u7eaa\u5f8b.

`shared/events/contract.py` \u7684 `EVENTS` \u662f event_name \u552f\u4e00\u4e8b\u5b9e\u6765\u6e90\uff1b\u672c\u6a21\u5757\u662f
**\u9a8c\u8bc1\u8005**\uff08\u4e0d\u662f\u63a5\u7f1d——\u6ce8\u518c\u8868\u4e4b\u5916\u4e0d\u518d\u6709\u624b\u5de5\u5feb\u7167\uff09\u3002\u56db\u4e2a\u5b88\u536b\uff1a

1. **\u524d\u5411**\uff1a\u751f\u4ea7\u4ee3\u7801\u91cc\u6bcf\u4e2a\u9759\u6001 `event=` \u5b57\u9762\u91cf\u5fc5\u987b\u5df2\u6ce8\u518c\u3002\u672a\u6ce8\u518c =
   `telemetry.emit` fail-fast\uff08\u65b0\u4ee3\u7801\uff09\u6216\u9759\u9ed8\u843d category=log 30d\uff08\u65e7\u8def\u5f84\uff09\uff0c
   \u4e8c\u8005\u90fd\u662f\u4e8b\u6545\u3002
2. **\u53cd\u5411**\uff1a`EVENTS` \u6bcf\u4e2a\u6761\u76ee\u5fc5\u987b\u6709\u751f\u4ea7\u8005\uff08`event=` / `label=` /
   `event_type=` \u5b57\u9762\u91cf\uff0c\u6216\u4e0b\u65b9 SQL/\u52a8\u6001\u6e05\u5355\uff09——\u9632\u5220\u9664/\u6539\u540d\u4e8b\u4ef6\u7559\u4e0b\u6ce8\u518c\u8868
   \u6b8b\u7559\uff08category \u6620\u5c04\u7ee7\u7eed\u5b58\u6d3b\u4e00\u4e2a\u65e0\u4eba\u4ea7\u751f\u7684\u4e8b\u4ef6\uff09\u3002
3. **\u6587\u6863\u6f02\u79fb**\uff1a`shared/events/registry.md` \u5fc5\u987b\u4e0e\u751f\u6210\u5668\u8f93\u51fa\u9010\u5b57\u4e00\u81f4\uff08R2-C\uff1a
   \u6587\u6863\u662f\u751f\u6210\u7269\uff09\u3002
4. **SQL \u952e\u6ce8\u5165**\uff1a\u8bfb\u53d6\u7aef SQL \u91cc\u7684 attributes \u952e\u5b57\u9762\u91cf\uff08`->>'` \u4e0e `?`
   \u4e24\u79cd\u5f62\u6001\uff09\u5fc5\u987b\u662f\u4e00\u4e2a payload TypedDict \u7684\u58f0\u660e\u952e\uff08`registered_payload_keys`\uff09
   ——"\u6539\u540d\u4e8b\u4ef6 = \u6ce8\u518c\u8868\u4e00\u884c + \u6240\u6709\u5f15\u7528\u70b9\u7f16\u8bd1/\u6d4b\u8bd5\u5931\u8d25"\u7684 SQL \u534a\u8fb9\u3002

\u9650\u5236\uff08\u7ee7\u627f\u81ea scan_kinds\uff09\uff1a\u9759\u6001\u5b57\u9762\u91cf\u626b\u63cf\u770b\u4e0d\u5230\u52a8\u6001\u4ea7\u751f\u7684\u540d\u5b57\uff08\u53d8\u91cf
`event_type`\u3001dict \u503c\u3001\u4e09\u5143\u8868\u8fbe\u5f0f\uff09——\u5b83\u4eec\u5728 `_SQL_OR_DYNAMIC_KINDS` \u91cc\u9010\u6761
\u6807\u6ce8\u4ea7\u751f\u70b9\u3002
"""

from __future__ import annotations

import re
from pathlib import Path

from shared.events import scan_kinds  # namespace package — pythonpath = ["."]
from shared.events.contract import EVENTS, registered_payload_keys, telemetry_events

_REPO = Path(__file__).resolve().parents[1]
_REGISTRY = _REPO / "shared" / "events" / "registry.md"

# Kinds whose production site has no static `event=` literal, so scan_kinds
# cannot see them. Each entry carries its emission site; removing a kind from
# the code means removing it here too (the reverse gate would otherwise flag
# the orphaned registry entry).
_SQL_OR_DYNAMIC_KINDS = frozenset(
    {
        # Positional emit calls from daemon alert paths, with no `event=` literal.
        "delivery_stalled",  # services/delivery_watchdog/daemon.py:_alert_stalled
        "loki_write_path_probe_failed",  # services/healthchecks/lgtm.py write-path probe
        "delivery_poisoned",  # services/delivery_watchdog/dispatch_guard.py:_alert_poisoned
        "heartbeat_nudged",  # services/heartbeat/daemon.py:_alert_idle
        # Dynamic emit: positional-argument form, no `event=` literal.
        "task_reminder_digest",  # task_maintenance/daemon.py:_run_reminders
        "task_escalation",  # task_maintenance/daemon.py:_run_escalate
        "watchdog_tick",  # services/watchdog/daemon.py:_TickProgress.record_completed (positional emit)
        "heartbeat_paused",  # ava/self.py:258 telemetry.emit("telemetry", ...)
        "frontend_interaction",  # gateway/routers/frontend_telemetry.py telemetry.emit("telemetry", ...)
        "pgbouncer_repaired",  # services/healthchecks/pgbouncer.py:_emit_repaired
        "editable_pth_repaired",  # shared/editable_install.py:repair_editable_ava_pth
        "editable_direct_url_repaired",  # shared/editable_install.py:repair_editable_direct_url
        "exec_editable_install_poisoned",  # shared/editable_install.py:guard_editable_install
        "source_tree_reset",  # shared/source_tree_guard.py:repair_source_tree
        "agent_boot_failed",  # agent/loop.py:_emit_boot_failure
        "sdk_call",  # agent/sdk_metering.py recorder (via shared/sdk_telemetry)
        # shared/plugin_activation.py:emit binds event=PLUGIN_ACTIVATION_EVENT (a
        # module constant, like sdk_call), so the literal scan cannot see it.
        "plugin_activation",
        "gateway_latency",  # gateway/_latency.py:emit_bucket telemetry.emit("telemetry", ...)
        "sse",  # gateway/_runtime_metrics.py:sse_opened/sse_closed positional emit
        "gateway_process",  # gateway/_runtime_metrics.py:_emit_snapshot positional emit
        "gateway_event_loop",  # gateway/_runtime_metrics.py:_emit_snapshot positional emit
        "auth401_rejected",  # gateway/_auth401_log.py:emit_auth401_count telemetry.emit("telemetry", ...)
        "agent_registry",  # gateway/_agent_max_id.py:emit_max_agent_id telemetry.emit("telemetry", ...)
        "memory_search_stats",  # services/memory_search/app.py:emit_memory_search_stats (positional emit)
        "pitr_remote_inventory",  # services/pitr/retention_scheduler.py:refresh (positional emit)
        "recovery_drill_failed",  # services/backup_scheduler/daemon.py:_run_due_local_dump_restore + services/pitr/base_scheduler_daemon.py:run (positional emit)
        "plugin_load_failed",  # agent/graph/_build.py:_report_plugin_load_failure telemetry.emit("telemetry", ...)
        "loki_query_budget",  # gateway/loki_query_budget.py:_emit_observation
        "telemetry_read_stale",  # gateway/telemetry_staleness.py:_emit
        "telemetry_read_recovered",  # gateway/telemetry_staleness.py:_emit
        "otlp_backend_disabled",  # shared/telemetry_otlp.py:_emit_backend_event
        "otlp_backend_recovered",  # shared/telemetry_otlp.py:_emit_backend_event
        "prom_query_budget",  # gateway/prom_metrics.py:_emit_budget_observation
        # Class-resolution markers select their name from the event level at
        # runtime; services/events_maintenance/resolution.py emits the reopen
        # markers and resolution_status, while the gateway emits resolved ones.
        "warning_resolved",
        "error_resolved",
        "warning_reopened",
        "error_reopened",
        "resolution_status",
        "checkpoint_table_sizes",  # services/events_maintenance/blob_vacuum.py telemetry.emit (positional)
        # Positional emit from both frozen-archive readers (task #2004).
        "archive_fetch_degraded",  # gateway/neighbors.py:_emit_archive_degraded + gateway/routers/fleet_graph.py:_emit_archive_degraded
        # Legacy bracketed name: the pre-W8-rename value, still a migrate_events.py
        # mapping target and present in existing DB rows. New code must not emit it;
        # the registration survives only to backfill the metric.
        "exec(cancelled)",
        "exec(failed)",
        "exec(thread-stuck)",
        "exec(timeout)",
        # audit dynamic event_type: not an `event_type="x"` literal, invisible to the scanner.
        "spawn",  # ops/agent_spawn.py:349 event_type = "fork" if ... else "spawn"
        "send_message",  # shared/db.py:497 inbound kind->event_type mapping value
        "terminate",  # shared/db.py:498 same as above
        "cancel",  # shared/db.py:500 same as above
        # Historic producer-less events (registry §7.4): existing DB rows and schema
        # comments still reference them; registration stays until the unified model
        # lands and retirement is confirmed.
        "report_activity",  # no current producer (DB 5,274 rows)
        "report_breached",  # no current producer (DB 14 rows)
    }
)


def _code_kinds() -> tuple[set[str], set[str], set[str]]:
    """(event= literals, label= literals, event_type= literals) from scan_kinds."""
    event_kinds, label_kinds, event_type_kinds, _sse_roles = scan_kinds.scan_code(_REPO)
    return set(event_kinds), set(label_kinds), set(event_type_kinds)


def test_every_static_event_kind_is_registered() -> None:
    """Forward gate: an `event=` kind outside `EVENTS` silently falls to
    category=log (30d retention) on the loguru path, and `telemetry.emit`
    fail-fasts on it elsewhere — either way it is a contract violation."""
    event_kinds, _, _ = _code_kinds()
    unregistered = sorted(k for k in event_kinds if k not in EVENTS)
    assert not unregistered, (
        "event= kind(s) missing from shared/events/contract.py EVENTS: "
        f"{unregistered}. Register each in the same PR that introduces it "
        "(one EventSpec line — shared/events/registry.md regenerates)."
    )


def test_registered_telemetry_events_have_producers() -> None:
    """Reverse drift guard: every EVENTS entry must have a code producer — an
    `event=` literal, a `label=` fallback, an `event_type=` literal, or a
    documented SQL/dynamic emission. Catches stale registry entries (deleted
    or renamed kind still listed) that would keep the category mapping alive
    for a kind nobody emits."""
    event_kinds, label_kinds, event_type_kinds = _code_kinds()
    produced = event_kinds | label_kinds | event_type_kinds | _SQL_OR_DYNAMIC_KINDS
    orphaned = sorted(telemetry_events() - produced)
    assert not orphaned, (
        "EVENTS telemetry kind(s) with no producer in code: "
        f"{orphaned}. Remove them from the registry, or add the emission site "
        "(or a `_SQL_OR_DYNAMIC_KINDS` entry with a comment) in the same PR."
    )


def test_registry_doc_matches_generated() -> None:
    """shared/events/registry.md is a generated artifact (R2-C): it must equal
    the generator's output byte-for-byte. A registry change without running
    `scripts/gen_event_registry.py` fails here (and in the pre-commit
    `events-registry-fresh` hook)."""
    from scripts.gen_event_registry import render  # namespace package

    generated = render()
    current = _REGISTRY.read_text(encoding="utf-8")
    assert current == generated, (
        "shared/events/registry.md is out of sync with the EVENTS registry — "
        "run .venv/bin/python scripts/gen_event_registry.py and commit the "
        "regenerated doc in the same PR."
    )


_ATTRIBUTES_KEY_RE = re.compile(r"""attributes(?:->>|\s*\?\s*)'([^']+)'""")

# The SQL-fragment definition site — `_sql_keys` builds `attributes->>'<key>'`
# from registered payload keys; every other file must not hand-write literals.
_EXEMPT_SQL_KEY_FILES = frozenset({"shared/events/contract.py"})


def test_attributes_key_literals_are_registered() -> None:
    """SQL key-injection gate: every attributes key literal (the `->>`
    and `?` forms) in the repo must be a declared payload key.

    A reader referencing a key no producer declares is a contract violation,
    not a query detail — it would silently NULL out after a payload rename.
    New read sites should consume the per-event SQL fragment constants
    (LLM_USAGE_KEYS etc.) from shared/events/contract.py instead of writing
    literals; this gate is the safety net for hand-written ones.
    """
    unregistered: list[tuple[str, int, str]] = []
    for path in sorted(_REPO.rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if any(
            seg in rel
            for seg in (
                ".venv",
                ".git",
                ".worktrees",
                "__pycache__",
                ".ruff_cache",
                ".pytest_cache",
            )
        ):
            continue
        if rel in _EXEMPT_SQL_KEY_FILES:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if line.lstrip().startswith("#"):
                continue
            for match in _ATTRIBUTES_KEY_RE.finditer(line):
                key = match.group(1)
                if key not in registered_payload_keys():
                    unregistered.append((rel, lineno, key))
    assert not unregistered, (
        "attributes key literal(s) not declared in any payload TypedDict "
        f"(shared/events/contract.py): {unregistered[:10]}. Declare the key "
        "on the event's payload TypedDict, or use the registry SQL fragment "
        "constants."
    )
