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
    "agent/_runloop.py::_record_permanent_reject_outcome": "ineligible",
    "agent/_runloop.py::_handle_fatal_llm_error": "ineligible",
    "agent/corpse_reap.py::reap_crash_corpses": "ineligible",
    "agent/corpse_reap.py::reap_recrashed_corpse": "ineligible",
    "agent/hooks/compact.py::auto_compact_for_llm": "ineligible",
    "agent/hosted_ownership.py::admit_hosted_runtime": "ineligible",
    "agent/hosted_ownership.py::admit_hosted_runtime#2": "ineligible",
    "agent/hosted_ownership.py::settle_hosted_runtime": "ineligible",
    "ava/self.py::compact": "local",
    "ava/skills.py::_insert_skill_events": "local",
    "ava_builtins/plugins/ava_fleet/_task_update.py::_log_task_update": "local",
    "ava_builtins/plugins/ava_fleet/plugin.py::set_label": "local",
    "ava_builtins/plugins/ava_fleet/task_registry.py::_insert_task": "local",
    "gateway/mcp_endpoint.py::_AuditMiddleware.__call__": "ineligible",
    "gateway/mcp_endpoint.py::_AuditMiddleware.__call__#2": "ineligible",
    "ops/agent_spawn.py::_announce_created_agent": "central",
    "ops/agent_wake.py::_stage_resurrect_event": "central",
    "ops/billing_recovery.py::_record_run_event": "ineligible",
    "ops/ops_exit.py::_stage_termination_event": "central",
    "ops/ops_exit.py::mark_agent_closed": "central",
    "ops/ops_lifecycle.py::_recover_crash_marked_blocking": "central",
    "services/computer/mcp_daemon.py::ComputerMcpDaemon._emit_action": "central",
    "services/computer/mcp_daemon.py::ComputerMcpDaemon._emit_session_event": "central",
    "shared/agents/messages/chat_delivery.py::_insert_chat_inbound_once": "central",
    "shared/db.py::insert_inbound_message": "central",
    "shared/db.py::announce_spawn_prompt": "central",
    "shared/db.py::insert_restart_completed_inbound": "central",
    "shared/db.py::insert_compact_request_inbound": "ineligible",
    "shared/env_audit.py::_emit_audit_event": "ineligible",
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
        for scope, lines in visitor.scope_lines.items():
            for ordinal, _line in enumerate(sorted(lines), start=1):
                suffix = f"#{ordinal}" if ordinal > 1 else ""
                found.add(f"{relative}::{scope}{suffix}")
    return found


class _AuditRootVisitor(ast.NodeVisitor):
    """Find helper calls plus direct telemetry construction through aliases."""

    def __init__(self) -> None:
        self.scope_lines: dict[str, list[int]] = {}
        self._scope: list[str] = []
        self._audit_modules: set[str] = set()
        self._audit_functions: set[str] = set()
        self._telemetry_modules: set[str] = set()
        self._telemetry_functions: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "shared":
                root = alias.asname or alias.name
                self._audit_modules.add(f"{root}.audit_events")
                self._telemetry_modules.add(f"{root}.telemetry")
            if alias.name == "shared.audit_events":
                self._audit_modules.add(alias.asname or alias.name)
            if alias.name == "shared.telemetry":
                self._telemetry_modules.add(alias.asname or "shared.telemetry")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._register_shared_module_aliases(node)
        self._register_function_aliases(
            node,
            "shared.audit_events",
            _AUDIT_HELPERS,
            self._audit_functions,
        )
        self._register_function_aliases(
            node,
            "shared.telemetry",
            _DIRECT_AUDIT_EMITTERS,
            self._telemetry_functions,
        )
        self.generic_visit(node)

    def _register_shared_module_aliases(self, node: ast.ImportFrom) -> None:
        if node.module == "shared":
            for alias in node.names:
                if alias.name == "audit_events":
                    self._audit_modules.add(alias.asname or alias.name)
                if alias.name == "telemetry":
                    self._telemetry_modules.add(alias.asname or alias.name)

    @staticmethod
    def _register_function_aliases(
        node: ast.ImportFrom,
        module: str,
        helpers: frozenset[str],
        destination: set[str],
    ) -> None:
        if node.module == module:
            destination.update(
                alias.asname or alias.name for alias in node.names if alias.name in helpers
            )

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_audit_helper(node) or self._is_direct_audit_emitter(node):
            scope = ".".join(self._scope) if self._scope else "<module>"
            self.scope_lines.setdefault(scope, []).append(node.lineno)
        self.generic_visit(node)

    def _is_audit_helper(self, node: ast.Call) -> bool:
        if isinstance(node.func, ast.Name):
            return node.func.id in _AUDIT_HELPERS or node.func.id in self._audit_functions
        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _AUDIT_HELPERS
            and _attribute_name(node.func.value) in self._audit_modules
        )

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
    # A one-for-one root replacement in the same scope keeps its key; count
    # changes and scope renames require an inventory update.
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
        path = root / location.split("::", 1)[0]
        source = path.read_text(encoding="utf-8")
        assert "stage_central_expected_event" in source or "emit_staged_central_event" in source, (
            location
        )


def test_scope_keys_survive_line_drift_and_require_count_and_name_updates(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "producer.py"
    source = (
        "from shared.audit_events import insert_event_log\n"
        "insert_event_log(event_type='module')\n"
        "class Producer:\n"
        "    async def emit(self):\n"
        "        insert_event_log(event_type='first')\n"
        "        insert_event_log(event_type='second')\n"
    )
    path.write_text(source, encoding="utf-8")
    inventory = {
        "producer.py::<module>": "ineligible",
        "producer.py::Producer.emit": "local",
        "producer.py::Producer.emit#2": "local",
    }
    _assert_classified(_audit_roots(root), inventory)

    path.write_text("\n" * 5 + source, encoding="utf-8")
    _assert_classified(_audit_roots(root), inventory)

    path.write_text(source + "        insert_event_log(event_type='third')\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"producer\.py::Producer\.emit#3"):
        _assert_classified(_audit_roots(root), inventory)

    path.write_text(source.replace("def emit", "def renamed"), encoding="utf-8")
    with pytest.raises(AssertionError, match=r"stale inventory=.*Producer\.emit"):
        _assert_classified(_audit_roots(root), inventory)


def test_an_unclassified_new_audit_construction_root_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "new_producer.py").write_text(
        "from shared.audit_events import insert_event_log\n"
        "def emit():\n"
        "    insert_event_log(event_type='send_message', agent_id=1, source='agent:1')\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match=r"new_producer\.py::emit"):
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
    with pytest.raises(AssertionError, match=r"new_producer\.py::emit"):
        _assert_classified(_audit_roots(root), {})


def test_an_unclassified_aliased_audit_helper_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "new_producer.py").write_text(
        "from shared.audit_events import insert_event_log as audit\n"
        "def emit():\n"
        "    audit(event_type='send_message', agent_id=1, source='agent:1')\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match=r"new_producer\.py::emit"):
        _assert_classified(_audit_roots(root), {})


def test_an_unclassified_import_shared_audit_emitter_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "new_producer.py").write_text(
        "import shared\ndef emit():\n    shared.telemetry.emit('audit', 'send_message')\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match=r"new_producer\.py::emit"):
        _assert_classified(_audit_roots(root), {})


def test_an_unclassified_import_shared_audit_helper_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "new_producer.py").write_text(
        "import shared\n"
        "def emit():\n"
        "    shared.audit_events.insert_event_log(\n"
        "        event_type='send_message', agent_id=1, source='agent:1'\n"
        "    )\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match=r"new_producer\.py::emit"):
        _assert_classified(_audit_roots(root), {})
