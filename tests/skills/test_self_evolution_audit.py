"""Pure leak-audit coverage for isolated self-evolution evaluation runs."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _audit_module():
    path = Path(".agents/skills/ava-self-evolution/reference")
    sys.path.insert(0, str(path))
    return importlib.import_module("audit")


def _paths(tmp_path: Path):
    audit = _audit_module()
    return audit.LeakPaths(
        memory_pool=str(tmp_path / "shared-memory"),
        self_evolution=str(tmp_path / "self-evolution"),
        workspaces_root=str(tmp_path / "workspaces"),
        agent_workspace=str(tmp_path / "workspaces" / "42"),
    )


def test_clean_code_has_no_findings(tmp_path: Path) -> None:
    audit = _audit_module()
    assert (
        audit.scan(["print('only the assigned task')"], agent_id=42, leak_paths=_paths(tmp_path))
        == []
    )


def test_shared_memory_read_invalidates(tmp_path: Path) -> None:
    audit = _audit_module()
    paths = _paths(tmp_path)
    findings = audit.scan(
        [f"open({paths.memory_pool!r} + '/MEMORY.md')"], agent_id=42, leak_paths=paths
    )
    assert [finding.surface for finding in findings] == ["memory"]
    assert audit.invalidated(findings) is True


def test_other_agent_workspace_read_invalidates(tmp_path: Path) -> None:
    audit = _audit_module()
    paths = _paths(tmp_path)
    findings = audit.scan(
        [f"open('{paths.workspaces_root}/41/result.md')"], agent_id=42, leak_paths=paths
    )
    assert [finding.surface for finding in findings] == ["results"]


def test_own_workspace_read_is_allowed(tmp_path: Path) -> None:
    audit = _audit_module()
    paths = _paths(tmp_path)
    assert (
        audit.scan([f"open('{paths.agent_workspace}/notes.md')"], agent_id=42, leak_paths=paths)
        == []
    )


def test_other_agent_result_endpoint_invalidates_but_own_does_not(tmp_path: Path) -> None:
    audit = _audit_module()
    paths = _paths(tmp_path)
    findings = audit.scan(
        [
            "requests.get('/api/agents/41/messages')",
            "requests.get('/api/agents/42/messages')",
        ],
        agent_id=42,
        leak_paths=paths,
    )
    assert [finding.surface for finding in findings] == ["results"]


def test_sdk_vector_is_reported_without_invalidating(tmp_path: Path) -> None:
    audit = _audit_module()
    findings = audit.scan(
        ["ava.tasks.list(); ava.agents.get_last_message(7)"],
        agent_id=42,
        leak_paths=_paths(tmp_path),
    )
    assert {finding.surface for finding in findings} == {"sdk"}
    assert {finding.tool for finding in findings} == {
        "ava.tasks.list",
        "ava.agents.get_last_message",
    }
    assert audit.invalidated(findings) is False
