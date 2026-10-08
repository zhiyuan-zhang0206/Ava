"""Contract: scans the git-driving modules of the whole tree for subprocess.run timeouts."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


_GIT_DRIVING_MODULES = (
    "ops/cluster/operations.py",
    "base/deploy/git/cluster_drift.py",
)


@pytest.mark.parametrize("rel", _GIT_DRIVING_MODULES)
def test_git_driving_modules_do_not_bound_with_subprocess_run(rel: str) -> None:
    """No `subprocess.run(..., timeout=...)` in the modules that drive git: a
    timeout there must come from `run_bounded`, which bounds the tree."""
    tree = ast.parse((_REPO_ROOT / rel).read_text())
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "subprocess.run"
        and any(kw.arg == "timeout" for kw in node.keywords)
    ]
    assert not offenders, f"{rel}: use base.host.proc.run_bounded at line(s) {offenders}"
