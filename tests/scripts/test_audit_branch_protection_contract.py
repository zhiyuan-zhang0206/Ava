"""Contract: the branch-protection audit accepts the real .trunk/trunk.yaml declaration of the full gate."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]


_SCRIPT = _REPO_ROOT / "scripts" / "audit" / "branch_protection.py"


def _audit() -> ModuleType:
    if not _SCRIPT.exists():
        pytest.fail("scripts/audit/branch_protection.py is not implemented")
    spec = importlib.util.spec_from_file_location("branch_protection", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_trunk_gate_findings_accept_the_real_declaration() -> None:
    audit = _audit()
    text = (_REPO_ROOT / ".trunk" / "trunk.yaml").read_text()
    assert audit.trunk_gate_findings(text) == []


def test_real_trunk_yaml_declares_the_full_gate() -> None:
    text = (_REPO_ROOT / ".trunk" / "trunk.yaml").read_text()
    document = yaml.safe_load(text)  # type: ignore[name-defined]
    statuses = document["merge"]["required_statuses"]
    assert len(statuses) == 12
    assert "secret scan (Gitleaks)" in statuses
    assert "qa-approved-gate" not in statuses
    assert "backend (pytest + pyright)" in statuses
    assert "frontend (eslint + tsc + vitest)" in statuses
    assert "e2e (Playwright happy path)" in statuses
    assert not any("${{" in name for name in statuses)
