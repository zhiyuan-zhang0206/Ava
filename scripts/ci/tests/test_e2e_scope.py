"""The e2e skip rule is fixed in code and wired identically into every e2e job."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts.ci import e2e_scope

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_UNIT_TESTS = [
    "base/lm/tests/test_catalog.py",
    "cli/commands/tests/helper.py",
    "agent/graph/exec/tests/resources/test_resource_admission_exec.py",
    "meizel/tests/test_roles.py",
    "scripts/ci/tests/conftest.py",
]
_NOT_UNIT_TESTS = [
    "tests/components/base/test_poll.py",  # top-level tests are imported by e2e
    "tests/fixtures/gateway_config.py",
    "tests/e2e/test_browser.py",
    "base/lm/engine.py",  # product code
    "base/lm/tests/data.json",  # not Python
    "base/lm/tests/meizel.toml",
    "ui/web/tests/thing.py",
    "base/lm/testsuite/test_x.py",  # not a `tests` directory
    "schedules/tests/test_template.py",  # templates are not packages with unit tests
    "base/tests",
]


def _skip(**overrides: Any) -> bool:
    arguments: dict[str, Any] = {
        "event": "pull_request",
        "head_ref": "feature",
        "mode": "enforce",
        "decision": "SELECTED",
        "changed": _UNIT_TESTS[:2],
    }
    return e2e_scope.skips_e2e(**{**arguments, **overrides})


@pytest.mark.parametrize("path", _UNIT_TESTS)
def test_package_internal_test_python_files_qualify(path: str) -> None:
    assert e2e_scope.is_package_unit_test(path)


@pytest.mark.parametrize("path", _NOT_UNIT_TESTS)
def test_nothing_else_qualifies(path: str) -> None:
    assert not e2e_scope.is_package_unit_test(path)


def test_only_unit_test_changes_with_an_enforced_subset_skip_e2e() -> None:
    assert _skip()
    assert _skip(changed=_UNIT_TESTS)


@pytest.mark.parametrize(
    "overrides",
    [
        {"decision": "FULL"},
        {"decision": "SKIP"},
        {"decision": ""},  # the selector failed or did not run
        {"mode": "shadow"},
        {"mode": ""},
        {"event": "push"},
        {"event": "workflow_dispatch"},
        {"head_ref": "trunk-merge/pr-1/abc"},
        {"head_ref": "trunk-temp/batch"},
        {"changed": []},
        {"changed": [*_UNIT_TESTS, "base/lm/engine.py"]},
        {"changed": [*_UNIT_TESTS, "tests/components/base/test_poll.py"]},
        {"changed": [*_UNIT_TESTS, "meizel.toml"]},
    ],
)
def test_any_other_case_keeps_e2e(overrides: dict[str, Any]) -> None:
    assert not _skip(**overrides)


def test_the_cli_prints_the_step_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    changed = tmp_path / "changed.txt"
    changed.write_text("\n".join(_UNIT_TESTS) + "\n")
    arguments = ["--changed-files", str(changed), "--event", "pull_request"]

    assert e2e_scope.main([*arguments, "--mode", "enforce", "--decision", "SELECTED"]) == 0
    assert capsys.readouterr().out == "skip=true\n"
    assert e2e_scope.main([*arguments, "--mode", "enforce", "--decision", "FULL"]) == 0
    assert capsys.readouterr().out == "skip=false\n"


def test_a_package_test_helper_that_e2e_imports_is_never_skipped_past(tmp_path: Path) -> None:
    """The premise of the rule: a unit-test edit inside a package cannot reach an e2e test."""
    helper = tmp_path / "base" / "sessions" / "tests" / "poll.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("from base.sessions.tests import deeper\n")
    (helper.parent / "deeper.py").write_text("")
    (helper.parent / "unrelated.py").write_text("")
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    (e2e / "test_flow.py").write_text(
        "from base.sessions.tests.poll import wait\nfrom tests.fixtures import thing\n"
    )

    reached = e2e_scope.e2e_reached_unit_tests(tmp_path)

    assert reached == {"base/sessions/tests/poll.py", "base/sessions/tests/deeper.py"}
    assert not _skip(changed=["base/sessions/tests/deeper.py"], reached=reached)
    assert _skip(changed=["base/sessions/tests/unrelated.py"], reached=reached)


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimportlib.import_module(name)\n",
        "__import__(name)\n",
        "import runpy\nrunpy.run_module(name)\n",
        "import runpy\nrunpy.run_path('somewhere.py')\n",
        "exec(code)\n",
        "def broken(:\n",
    ],
    ids=[
        "computed-import",
        "computed-dunder-import",
        "computed-run-module",
        "run-path",
        "exec",
        "syntax-error",
    ],
)
def test_a_file_the_walk_cannot_understand_fails_closed(tmp_path: Path, source: str) -> None:
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    (e2e / "test_flow.py").write_text(source)

    reached = e2e_scope.e2e_reached_unit_tests(tmp_path)

    assert reached is None
    assert not _skip(changed=["base/sessions/tests/unrelated.py"], reached=reached)


def test_a_literal_dynamic_import_is_followed_like_an_import_statement(tmp_path: Path) -> None:
    helper = tmp_path / "base" / "sessions" / "tests" / "poll.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("")
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    (e2e / "test_flow.py").write_text(
        "import importlib\nimportlib.import_module('base.sessions.tests.poll')\n"
    )

    assert e2e_scope.e2e_reached_unit_tests(tmp_path) == {"base/sessions/tests/poll.py"}


def test_a_relative_import_inside_a_package_test_file_is_followed(tmp_path: Path) -> None:
    tests = tmp_path / "base" / "sessions" / "tests"
    tests.mkdir(parents=True)
    (tests / "poll.py").write_text("from .deeper import wait\n")
    (tests / "deeper.py").write_text("")
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    (e2e / "test_flow.py").write_text("from base.sessions.tests.poll import wait\n")

    assert e2e_scope.e2e_reached_unit_tests(tmp_path) == {
        "base/sessions/tests/poll.py",
        "base/sessions/tests/deeper.py",
    }


def test_a_package_test_helper_reached_through_a_top_level_test_module_counts(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "gateway" / "tests" / "agents" / "auth.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("")
    fixtures = tmp_path / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "client.py").write_text("from gateway.tests.agents.auth import credentials\n")
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir()
    (e2e / "test_flow.py").write_text("from tests.fixtures.client import client\n")

    assert e2e_scope.e2e_reached_unit_tests(tmp_path) == {"gateway/tests/agents/auth.py"}


def test_a_known_dynamic_entry_whose_call_is_gone_is_stale(tmp_path: Path) -> None:
    kept = tmp_path / "tests" / "e2e" / "kept.py"
    kept.parent.mkdir(parents=True)
    kept.write_text("import runpy\nrunpy.run_module('services.daemon')\n")
    (tmp_path / "tests" / "e2e" / "rewritten.py").write_text("print('no dynamic call any more')\n")
    (tmp_path / "tests" / "e2e" / "broken.py").write_text("def broken(:\n")
    known = {
        "tests/e2e/kept.py": frozenset({"run_module"}),
        "tests/e2e/rewritten.py": frozenset({"run_module"}),
        "tests/e2e/deleted.py": frozenset({"exec"}),
        "tests/e2e/broken.py": frozenset({"exec"}),
    }

    assert e2e_scope.stale_known_dynamic(tmp_path, known) == [
        "tests/e2e/broken.py: exec (file missing or unreadable)",
        "tests/e2e/deleted.py: exec (file missing or unreadable)",
        "tests/e2e/rewritten.py: run_module",
    ]


def test_every_known_dynamic_entry_still_has_its_call() -> None:
    """A stale entry would quietly excuse a new dynamic import of the same name; delete it with the call."""
    assert e2e_scope.stale_known_dynamic(_REPO_ROOT) == []


def test_the_real_e2e_walk_understands_every_import() -> None:
    """A new dynamic import in e2e flips the walk to fail-closed; this keeps the skip alive on purpose."""
    reached = e2e_scope.e2e_reached_unit_tests(_REPO_ROOT)
    assert reached is not None
    assert "base/sessions/tests/poll_until.py" in reached


def test_the_real_e2e_imports_are_all_in_the_reached_set() -> None:
    reached = e2e_scope.e2e_reached_unit_tests(_REPO_ROOT)
    assert reached is not None
    for file in sorted((_REPO_ROOT / "tests" / "e2e").rglob("*.py")):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                if parts[0] in e2e_scope.PACKAGES and "tests" in parts[1:]:
                    path = Path(*parts).with_suffix(".py").as_posix()
                    if (_REPO_ROOT / path).is_file():
                        assert path in reached, f"{file.relative_to(_REPO_ROOT)}: {node.module}"


def test_every_package_with_unit_tests_is_covered_or_excluded_on_purpose() -> None:
    """A new code top that holds `tests` directories must be added to the rule or stay out of it."""
    excluded = {"tests", "ui", "schedules", "demos"}
    tops = {
        path.relative_to(_REPO_ROOT).parts[0]
        for path in _REPO_ROOT.glob("*/**/tests/test_*.py")
        if path.relative_to(_REPO_ROOT).parts[0] not in {"node_modules", ".venv", ".worktrees"}
    }
    assert tops - excluded <= set(e2e_scope.PACKAGES)


def test_every_e2e_job_and_the_aggregator_honor_the_flag() -> None:
    workflow = cast("dict[str, Any]", yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8")))
    jobs = workflow["jobs"]
    classify = jobs["classify"]
    assert classify["outputs"]["skip_e2e"] == "${{ steps.e2e.outputs.skip }}"
    step = next(item for item in classify["steps"] if item.get("id") == "e2e")
    assert step["if"] == (
        "${{ github.event_name == 'pull_request' && steps.decide.outputs.backend == 'true' }}"
    )
    assert "scripts/ci/e2e_scope.py" in step["run"]
    # After the selector, whose mode and decision it reads.
    ids = [item.get("id") for item in classify["steps"]]
    assert ids.index("selector") < ids.index("e2e")
    for name in ("e2e-shard", "e2e-hosted", "e2e-env-guard", "e2e"):
        assert "needs.classify.outputs.skip_e2e != 'true'" in jobs[name]["if"], name
    # Nothing else is gated by it: the backend jobs and every other required check still run.
    gated = {name for name, job in jobs.items() if "skip_e2e" in str(job.get("if", ""))}
    assert gated == {"e2e-shard", "e2e-hosted", "e2e-env-guard", "e2e"}
