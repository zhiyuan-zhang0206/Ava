from __future__ import annotations

import ast
from pathlib import Path

from scripts.lint import fixture_scope as _lint

_REPO_ROOT = Path(__file__).resolve().parents[2]


_E2E_CONFTEST = _REPO_ROOT / "tests" / "e2e" / "conftest.py"


def test_the_real_e2e_conftest_in_its_pre_fix_shape_is_flagged() -> None:
    src = _E2E_CONFTEST.read_text(encoding="utf-8")
    pre_fix = src.replace(
        '@pytest.fixture(scope="package")',
        '@pytest.fixture(scope="session")',
        1,
    )
    assert pre_fix != src, "the fixture's decorator no longer matches — update this test"
    found = _lint.findings_in_source(pre_fix, "tests/e2e/conftest.py", has_package_init=True)
    assert len(found) == 1
    assert "e2e_process_env" in found[0][1]
    for key in ("MEIVA_HOME", "MEIVA_GATEWAY_URL"):
        assert f"os.environ['{key}']" in found[0][1]


def test_the_real_e2e_conftest_is_flagged_if_the_package_init_is_deleted() -> None:
    src = _E2E_CONFTEST.read_text(encoding="utf-8")
    found = _lint.findings_in_source(src, "tests/e2e/conftest.py", has_package_init=False)
    messages = [m for _, m in found]
    assert any("tests/e2e/__init__.py does not exist" in m for m in messages)
    assert any("e2e_process_env" in m and "session-scoped" in m for m in messages)
    assert _lint.findings_in_source(src, "tests/e2e/conftest.py", has_package_init=True) == []


def test_setup_env_keys_matches_the_real_fixtures_body() -> None:
    literal, dynamic = _lint.setup_env_keys(
        _E2E_CONFTEST.read_text(encoding="utf-8"), "e2e_process_env"
    )
    assert dynamic == frozenset()
    assert len(literal) == 14
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
