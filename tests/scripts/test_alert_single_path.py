"""Alerts have one way in: Grafana -> webhook -> the gateway's `POST /api/alerts` ingest.

No process other than that ingest writes the `alerts` table, calls the IM alert fan-out or posts
to the ingest endpoint. A new in-code alert path (a probe writing its own row, a direct IM push
shaped like an alert) fails here and belongs in `rules.yml` as a rule over a signal instead.
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
