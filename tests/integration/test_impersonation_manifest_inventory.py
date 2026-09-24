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
_DIRECT_AUDIT_EMITTERS = frozenset({"emit", "prepare_event"})
_INVENTORY: dict[str, str] = {
    "agent/_runloop.py:183": "ineligible",
    "agent/_runloop.py:354": "ineligible",
    "agent/corpse_reap.py:127": "ineligible",
    "agent/corpse_reap.py:216": "ineligible",
    "agent/hooks/compact.py:544": "ineligible",
    "agent/hosted_ownership.py:536": "ineligible",
    "agent/hosted_ownership.py:561": "ineligible",
    "agent/hosted_ownership.py:614": "ineligible",
    "ava/self.py:308": "local",
    "ava/skills.py:713": "local",
    "ava_builtins/plugins/ava_fleet/_task_update.py:345": "local",
    "ava_builtins/plugins/ava_fleet/plugin.py:63": "local",
    "ava_builtins/plugins/ava_fleet/task_registry.py:227": "local",
    "cli/commands/_managed_writer_mode.py:164": "ineligible",
    "gateway/mcp_endpoint.py:213": "ineligible",
    "gateway/mcp_endpoint.py:225": "ineligible",
    "ops/agent_spawn.py:277": "central",
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
    "shared/db.py:393": "central",
    "shared/db.py:467": "central",
    "shared/db.py:674": "ineligible",
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
        visitor = _AuditRootVisitor()
        visitor.visit(tree)
        found.update(f"{relative}:{line}" for line in visitor.lines)
    return found


class _AuditRootVisitor(ast.NodeVisitor):
    """Find helper calls plus direct telemetry construction through aliases."""

    def __init__(self) -> None:
        self.lines: set[int] = set()
        self._telemetry_modules: set[str] = set()
        self._telemetry_functions: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "shared.telemetry":
                self._telemetry_modules.add(alias.asname or "shared.telemetry")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "shared":
            self._telemetry_modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "telemetry"
            )
        if node.module == "shared.telemetry":
            for alias in node.names:
                if alias.name in _DIRECT_AUDIT_EMITTERS:
                    self._telemetry_functions.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_audit_helper(node) or self._is_direct_audit_emitter(node):
            self.lines.add(node.lineno)
        self.generic_visit(node)

    def _is_audit_helper(self, node: ast.Call) -> bool:
        if isinstance(node.func, ast.Name):
            return node.func.id in _AUDIT_HELPERS
        return isinstance(node.func, ast.Attribute) and node.func.attr in _AUDIT_HELPERS

    def _is_direct_audit_emitter(self, node: ast.Call) -> bool:
        if not _is_audit_category(node):
            return False
        if isinstance(node.func, ast.Name):
            return node.func.id == "prepare_event" or node.func.id in self._telemetry_functions
        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _DIRECT_AUDIT_EMITTERS
            and _attribute_name(node.func.value) in self._telemetry_modules
        )


def _is_audit_category(node: ast.Call) -> bool:
    return (
        bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value == "audit"
    )


def _attribute_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_attribute_name(node.value)}.{node.attr}"
    return ""


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


def test_an_unclassified_aliased_direct_audit_emitter_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "new_producer.py").write_text(
        "from shared import telemetry as events\n"
        "def emit():\n"
        "    events.emit('audit', 'send_message')\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match=r"new_producer\.py:3"):
        _assert_classified(_audit_roots(root), {})
