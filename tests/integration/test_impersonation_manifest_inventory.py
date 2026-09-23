"""Static census for every production audit-construction root.

The manifest protocol can only certify a source census when a newly-added
audit producer is forced to declare whether it is controller-local, central,
or deliberately outside the borrowed-identity boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_AUDIT_HELPERS = frozenset(
    {"insert_event_log", "insert_event_log_async", "insert_event_log_many", "prepare_event_log"}
)
_INVENTORY: dict[str, str] = {
    "agent/_runloop.py:183": "ineligible",
    "agent/_runloop.py:354": "ineligible",
    "agent/corpse_reap.py:127": "ineligible",
    "agent/corpse_reap.py:216": "ineligible",
    "agent/hooks/compact.py:544": "ineligible",
    "agent/hosted_ownership.py:534": "ineligible",
    "agent/hosted_ownership.py:559": "ineligible",
    "agent/hosted_ownership.py:612": "ineligible",
    "ava/self.py:308": "local",
    "ava/skills.py:713": "local",
    "ava_builtins/plugins/ava_fleet/_task_update.py:345": "local",
    "ava_builtins/plugins/ava_fleet/plugin.py:63": "local",
    "ava_builtins/plugins/ava_fleet/task_registry.py:227": "local",
    "cli/commands/_managed_writer_mode.py:164": "ineligible",
    "gateway/mcp_endpoint.py:213": "ineligible",
    "gateway/mcp_endpoint.py:225": "ineligible",
    "ops/agent_spawn.py:459": "central",
    "ops/agent_wake.py:278": "central",
    "ops/billing_recovery.py:416": "ineligible",
    "ops/ops_exit.py:146": "central",
    "ops/ops_exit.py:297": "central",
    "ops/ops_lifecycle.py:626": "central",
    "ops/publication_recovery.py:349": "ineligible",
    "ops/publication_recovery.py:401": "ineligible",
    "ops/publication_recovery.py:539": "ineligible",
    "services/computer/mcp_daemon.py:288": "central",
    "services/computer/mcp_daemon.py:314": "central",
    "shared/chat_delivery.py:209": "central",
    "shared/db.py:337": "central",
    "shared/db.py:415": "central",
    "shared/db.py:622": "ineligible",
    "shared/env_audit.py:176": "ineligible",
}


def _audit_roots(root: Path) -> set[str]:
    """Return all production call sites that construct an audit event."""
    found: set[str] = set()
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if relative.parts[0] in {"tests", "scripts", ".venv"} or relative == Path(
            "shared/audit_events.py"
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
            )
            direct_audit = (
                name == "prepare_event"
                and bool(node.args)
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "audit"
            )
            if name in _AUDIT_HELPERS or direct_audit:
                found.add(f"{relative}:{node.lineno}")
    return found


def _assert_classified(roots: set[str], inventory: dict[str, str]) -> None:
    missing = sorted(roots - inventory.keys())
    stale = sorted(inventory.keys() - roots)
    if missing or stale:
        raise AssertionError(f"unclassified audit roots={missing}; stale inventory={stale}")
    if set(inventory.values()) - {"local", "central", "ineligible"}:
        raise AssertionError("audit inventory has an unknown classification")


def test_every_production_audit_root_has_one_manifest_classification() -> None:
    root = Path(__file__).parents[2]
    roots = _audit_roots(root)
    _assert_classified(roots, _INVENTORY)
    # The central registration paths must be visible at their roots, not hidden
    # behind an implicit generic hook that can accidentally tag a system event.
    for location, classification in _INVENTORY.items():
        if classification != "central":
            continue
        path = root / location.rsplit(":", 1)[0]
        source = path.read_text(encoding="utf-8")
        assert "stage_central_expected_event" in source or "emit_staged_central_event" in source, (
            location
        )


def test_an_unclassified_new_audit_construction_root_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "new_producer.py").write_text(
        "from shared.audit_events import insert_event_log\n"
        "def emit():\n"
        "    insert_event_log(event_type='send_message', agent_id=1, source='agent:1')\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="unclassified audit roots"):
        _assert_classified(_audit_roots(root), {})
