"""Alerts have one way in: Grafana -> webhook -> the gateway's `POST /api/alerts` ingest.

No process other than that ingest writes the `alerts` table, calls the IM alert fan-out or posts
to the ingest endpoint. A new in-code alert path (a probe writing its own row, a direct IM push
shaped like an alert) fails here and belongs in `rules.yml` as a rule over a signal instead.

Deletion sentinel (task #4996): the fields, modules and symbols the alerting migration removed
must stay removed. A stale-branch merge (#4207 over #4245) silently restored four of them once —
these tests fail when any of them comes back, before a deploy can carry the resurrection.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ROOTS = ("agent", "ava", "ava_builtins", "base", "cli", "gateway", "ops", "services", "schedules")

# The only module that writes alert rows and fans them out: the ingest core and its one router.
_INGEST = {"base/telemetry/alerts.py", "gateway/alerts/router.py"}
_WRITERS = {"upsert_alert", "stamp_notified", "notify_im"}


def _production_modules() -> list[Path]:
    paths: list[Path] = []
    for root in _ROOTS:
        for path in (_REPO / root).rglob("*.py"):
            parts = path.relative_to(_REPO).parts
            if "tests" in parts or path.name.startswith("test_") or ".venv" in parts:
                continue
            paths.append(path)
    return paths


def test_only_the_gateway_ingest_writes_alert_rows_or_fans_them_out() -> None:
    offenders: list[str] = []
    for path in _production_modules():
        rel = path.relative_to(_REPO).as_posix()
        if rel in _INGEST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in _WRITERS:
                    offenders.append(f"{rel}:{node.lineno} calls {name}")
            if isinstance(node, ast.alias) and node.name in _WRITERS:
                offenders.append(f"{rel}:{node.lineno} imports {node.name}")
    assert offenders == []


def test_nothing_but_grafana_posts_to_the_ingest_endpoint() -> None:
    offenders: list[str] = []
    for path in _production_modules():
        rel = path.relative_to(_REPO).as_posix()
        if rel in {
            "gateway/alerts/router.py",  # the route itself
            "gateway/app.py",  # its pause-policy route table
            "cli/commands/observability/observatory_urls.py",  # the URL rendered into Grafana
        }:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if '"/api/alerts"' in line or '/api/alerts"' in line or "}/api/alerts" in line:
                if "ContractRoute" in line or "RouteContract" in line:
                    continue
                offenders.append(f"{rel}:{number}")
    assert offenders == []


# ── the deletion sentinel (task #4996): what #4245 (and the SDK-buffer removal) deleted ──

_DELETED_FIELDS = (
    "db_pool_slow_acquire_warn_cooldown_seconds",
    "transition_warning_seconds",
    "transition_error_seconds",
    "inspect_metrics_degraded_cooldown_seconds",
    "delivery_watchdog_alert_grace_seconds",
    "browser_reach_failure_threshold",
    "venv_probe_failure_threshold",
    "brew_pin_probe_failure_threshold",
)
_DELETED_ALIASES = (
    "AVA_DB_POOL_SLOW_ACQUIRE_WARN_COOLDOWN_SECONDS",
    "AVA_ALERTS_TRANSITION_WARNING_SECONDS",
    "AVA_ALERTS_TRANSITION_ERROR_SECONDS",
    "AVA_ALERTS_INSPECT_METRICS_DEGRADED_COOLDOWN_SECONDS",
    "AVA_DELIVERY_WATCHDOG_ALERT_GRACE_SECONDS",
    "AVA_BROWSER_REACH_FAILURE_THRESHOLD",
    "AVA_VENV_PROBE_FAILURE_THRESHOLD",
    "AVA_BREW_PIN_PROBE_FAILURE_THRESHOLD",
)
# Paths as they read after the #4207 reorg (the old services/ paths map onto the groups).
_DELETED_MODULES = (
    "agent/graph/exec/_alerts.py",
    "agent/graph/exec/tests/test_exec_alerts.py",
    "base/agents/impersonation_event_alerts.py",
    "base/deploy/tests/test_transition.py",
    "base/deploy/transition.py",
    "cli/commands/cluster/health_alerts.py",
    "cli/commands/cluster/tests/test_deploy_mutex.py",
    "gateway/cluster/tests/test_agents_internals_cluster.py",
    "services/agent_runner/agent_host/boot_defer.py",
    "services/supervision/ava_root/alerts.py",
    "services/supervision/ava_root/tests/test_ava_root_alerts.py",
    "services/supervision/ava_root/tests/test_ava_root_terminal_escalation.py",
    "services/wake/delivery_watchdog/tests/test_scan_alert_gates.py",
)
_DELETED_SYMBOLS = (
    "_DeployWindowGate",
    "transition_severity",
    "_fire_machine_offline",
    "take_findings",
    "_pending_findings",
    "terminal_escalate_rounds",
)


def test_deleted_config_fields_stay_unregistered() -> None:
    """The removed keys must not re-enter the settings registry (a merge restored one once)."""
    from base.config import FIELD_INFOS, get_config_metadata

    for name in _DELETED_FIELDS:
        assert name not in FIELD_INFOS, f"{name} must not re-enter the settings registry"
    env_vars = {meta.env_var for meta in get_config_metadata()}
    for alias in _DELETED_ALIASES:
        assert alias not in env_vars, f"{alias} must not re-enter the config surface"


def test_deleted_modules_stay_deleted() -> None:
    """The modules the alerting migration removed must not come back as files."""
    for rel in _DELETED_MODULES:
        assert not (_REPO / rel).exists(), f"{rel} was deleted and must not return"


def test_deleted_symbols_absent_from_production_code() -> None:
    """The removed symbols must not appear as identifiers in production code."""
    offenders: list[str] = []
    for path in _production_modules():
        rel = path.relative_to(_REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.alias):
                name = node.asname or node.name.rsplit(".", 1)[-1]
            if name in _DELETED_SYMBOLS:
                offenders.append(f"{rel}:{getattr(node, 'lineno', '?')} {name}")
    assert offenders == []
