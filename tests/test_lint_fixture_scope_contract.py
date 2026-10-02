"""Contract: the fixture-scope lint flags the real tests/e2e/conftest.py in its pre-fix shape and when its package init is deleted, and reads the real fixture body's env keys."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

_lint = importlib.import_module("scripts.lint.fixture_scope")


_REPO_ROOT = Path(__file__).resolve().parent.parent


_E2E_CONFTEST = _REPO_ROOT / "tests" / "e2e" / "conftest.py"


def test_the_real_e2e_conftest_in_its_pre_fix_shape_is_flagged() -> None:
    """The defect this lint exists for, replayed on the real file.

    Flipping the one keyword back to `session` reproduces `main` as it stood before
    the 2026-07-29 fix. If this does not fire, the lint is decorative regardless of
    what the synthetic cases say.
    """
    src = _E2E_CONFTEST.read_text(encoding="utf-8")
    pre_fix = src.replace(
        '@pytest.fixture(scope="package", autouse=True)',
        '@pytest.fixture(scope="session", autouse=True)',
        1,
    )
    assert pre_fix != src, "the fixture's decorator no longer matches — update this test"
    found = _lint.findings_in_source(pre_fix, "tests/e2e/conftest.py", has_package_init=True)
    assert len(found) == 1
    assert "_e2e_process_env" in found[0][1]
    for key in ("AVA_HOME", "AVA_GATEWAY_URL"):
        assert f"os.environ['{key}']" in found[0][1]


def test_the_real_e2e_conftest_is_flagged_if_the_package_init_is_deleted() -> None:
    """The other half. `tests/e2e/__init__.py` is load-bearing and looks like cruft, so
    the lint has to notice its absence rather than trusting the keyword."""
    src = _E2E_CONFTEST.read_text(encoding="utf-8")
    found = _lint.findings_in_source(src, "tests/e2e/conftest.py", has_package_init=False)
    messages = [m for _, m in found]
    assert any("tests/e2e/__init__.py does not exist" in m for m in messages)
    assert any("_e2e_process_env" in m and "session-scoped" in m for m in messages)
    assert _lint.findings_in_source(src, "tests/e2e/conftest.py", has_package_init=True) == []


def test_setup_env_keys_matches_the_real_fixtures_body() -> None:
    # Ties the primitive to the file it guards: the twelve keys the real body
    # assigns (it was thirteen until the hibernation chain deletion dropped
    # AVA_HIBERNATE_ENABLED — Task #1976 phase 2 — twelve until the
    # always-authenticated data plane retired AVA_RUNNER_DB_PASSWORD, and eleven
    # until the direct-process stack started with an empty AVA_CLUSTER_SECRET).
    # The count is what keeps the `literal == declared` assertion below from passing
    # vacuously (both empty), so it tracks the fixture body — update it when the body
    # gains or drops an assignment, do not relax it.
    literal, dynamic = _lint.setup_env_keys(
        _E2E_CONFTEST.read_text(encoding="utf-8"), "_e2e_process_env"
    )
    assert dynamic == frozenset()
    assert len(literal) == 12
    tree = ast.parse(_E2E_CONFTEST.read_text(encoding="utf-8"))
    declared = {
        e.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_env_keys" for t in node.targets)
        and isinstance(node.value, ast.Tuple)
        for e in node.value.elts
        if isinstance(e, ast.Constant)
    }
    assert literal == declared
