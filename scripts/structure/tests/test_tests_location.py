"""The tests-location lint (`scripts/structure/tests_location.py`): a top-level test must be
registered (nothing else is accepted), the registry cannot rot, and the checks stay path-only."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

import pytest

from scripts.structure import (
    locality,
    placement,
    tests_location_allowed,
    tests_location_suggest,
)
from scripts.structure import tests_location as tl

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_REGISTERED = {"tests/base/test_x.py": ("contract", "a registered test the fixtures start from")}
_PYPROJECT = """\
[tool.importlinter]
root_packages = ["agent", "ops", "base"]

[[tool.importlinter.contracts]]
name = "Layers"
type = "layers"
layers = ["agent | ops", "base"]
"""
_SOURCES = {
    "pyproject.toml": _PYPROJECT,
    "base/__init__.py": "",
    "base/net/__init__.py": "",
    "base/net/retry.py": "def backoff():\n    return 1\n",
    "agent/__init__.py": "",
    "agent/own/__init__.py": "",
    "agent/own/hosted.py": "def admit():\n    return 1\n",
    "ops/__init__.py": "",
    "ops/wake/__init__.py": "",
    "ops/wake/spawn.py": "def spawn():\n    return 1\n",
    "tests/base/test_x.py": "def test_x():\n    pass\n",
}


def _write(root: pathlib.Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — fixed test commands, never external input
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Tests location test",
            "-c",
            "user.email=tests-location@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _run(
    root: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str] | None = None,
    allowed: dict[str, tuple[str, str]] | None = None,
) -> tuple[int, str, str]:
    status = tl.main(argv or [], repo_root=root, allowed={**_REGISTERED, **(allowed or {})})
    out = capsys.readouterr()
    return status, out.out, out.err


def _make_repo(root: pathlib.Path) -> pathlib.Path:
    """A tracked repository at `root` with one registered top-level test and nothing else."""
    for rel, text in _SOURCES.items():
        _write(root, rel, text)
    _git(root, "init", "--quiet")
    _git(root, "add", "-A")
    return root


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    return _make_repo(tmp_path)


def _track(root: pathlib.Path, rel: str, text: str = "def test_it():\n    pass\n") -> None:
    _write(root, rel, text)
    _git(root, "add", rel)


# ------------------------------------------------------------------ the verdict (path only)


def test_a_registered_tree_is_clean(repo: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(repo, capsys) == (0, "", "")


def test_a_new_top_level_test_is_refused_with_the_fix(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _track(repo, "tests/base/test_new.py")
    status, out, err = _run(repo, capsys)
    assert status == 1
    assert out.splitlines()[0].startswith("tests/base/test_new.py:1: a top-level test that is not")
    assert "scripts/structure/tests_location.py --suggest tests/base/test_new.py" in out
    assert "git mv" in err
    assert "path_scopes.toml" in err  # the autouse fixtures do not follow a move
    assert "scripts/structure/tests_location_allowed.py" in err  # the way out
    assert "`contract`" in err
    assert "`integration`" in err


def test_the_same_test_inside_a_package_is_not_this_lints_business(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _track(repo, "base/net/tests/test_new.py")
    _track(repo, "tests/base/helpers.py")  # not a test_*.py
    _track(repo, "tests/base/conftest.py")
    assert _run(repo, capsys) == (0, "", "")
    assert _run(repo, capsys, ["base/net/tests/test_new.py"]) == (0, "", "")


def test_a_test_in_a_by_design_place_is_never_judged(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for rel in (
        "tests/e2e/test_flow.py",
        "tests/e2e/deep/test_more.py",
        "tests/ui/test_app.py",
        "tests/fixtures/test_plugin.py",
        "tests/factories/test_factory.py",
        "tests/integration/test_cluster_instance.py",
    ):
        _track(repo, rel)
    assert _run(repo, capsys) == (0, "", "")


def test_an_allowed_test_passes_and_a_file_that_is_gone_makes_its_entry_stale(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _track(repo, "tests/ci/test_workflow.py")
    allowed = {"tests/ci/test_workflow.py": ("contract", "reads .github/workflows/ci.yml")}
    assert _run(repo, capsys, allowed=allowed) == (0, "", "")
    _git(repo, "rm", "-q", "-f", "tests/ci/test_workflow.py")
    status, out, _ = _run(repo, capsys, allowed=allowed)
    assert status == 1
    assert "stale entry `tests/ci/test_workflow.py`: the file no longer exists" in out


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (("contract ", "x"), "has category 'contract '"),
        (("harness", "x"), "has category 'harness'"),
        (("contract", "  "), "has no reason"),
    ],
)
def test_an_allowed_entry_needs_a_known_category_and_a_reason(
    repo: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    entry: tuple[str, str],
    message: str,
) -> None:
    _track(repo, "tests/ci/test_workflow.py")
    status, out, _ = _run(repo, capsys, allowed={"tests/ci/test_workflow.py": entry})
    assert status == 1
    assert message in out


def test_an_entry_that_needs_none_is_refused(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _track(repo, "tests/e2e/test_flow.py")
    _track(repo, "base/net/tests/test_inside.py")
    allowed = {
        "tests/e2e/test_flow.py": ("contract", "no need: e2e"),
        "base/net/tests/test_inside.py": ("integration", "not a top-level test"),
    }
    status, out, _ = _run(repo, capsys, allowed=allowed)
    assert status == 1
    assert "`tests/e2e/test_flow.py` needs no entry: it stays at the top level by design" in out
    assert "`base/net/tests/test_inside.py` is not a top-level test file" in out


# ------------------------------------------------------------------ explicit paths and --only


def test_explicit_paths_judge_exactly_those_tests(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _track(repo, "tests/base/test_a.py")
    _track(repo, "tests/base/test_b.py")
    status, out, _ = _run(repo, capsys, ["tests/base/test_a.py"])
    assert status == 1
    assert _flagged(out) == ["tests/base/test_a.py"]
    assert _run(repo, capsys, ["tests/base/test_x.py"]) == (0, "", "")  # registered: fine
    assert _run(repo, capsys, [str(repo / "tests/base/test_a.py")])[0] == 1  # absolute path


def _flagged(out: str) -> list[str]:
    return [line.split(":")[0] for line in out.splitlines() if ":1: a top-level test" in line]


def test_only_judges_the_changed_top_level_tests_and_nothing_else(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _track(repo, "tests/base/test_a.py")
    _track(repo, "tests/base/test_b.py")
    _track(repo, "base/net/tests/test_p.py")
    status, out, _ = _run(
        repo, capsys, ["--only", "tests/base/test_a.py", "base/net/tests/test_p.py"]
    )
    assert status == 1
    assert _flagged(out) == ["tests/base/test_a.py"]  # test_b did not change
    assert _run(repo, capsys, ["--only", "tests/base/test_x.py"]) == (0, "", "")  # registered
    assert _run(repo, capsys, ["--only", str(repo / "tests/base/test_a.py")])[0] == 1


def test_only_with_nothing_changed_has_nothing_to_judge(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _track(repo, "tests/base/test_a.py")
    assert _run(repo, capsys, ["--only"]) == (0, "", "")


@pytest.mark.parametrize(
    "rule_input",
    [
        "scripts/structure/tests_location.py",
        "scripts/structure/tests_location_allowed.py",
        "scripts/structure/tests_location_suggest.py",
    ],
)
def test_a_changed_rule_input_widens_only_to_every_test(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str], rule_input: str
) -> None:
    _track(repo, "tests/base/test_a.py")
    _track(repo, rule_input, "# a rule input\n")
    status, out, _ = _run(repo, capsys, ["--only", rule_input])
    assert status == 1
    assert _flagged(out) == ["tests/base/test_a.py"]


def test_a_changed_test_of_the_lint_itself_does_not_widen(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _track(repo, "tests/base/test_a.py")
    _track(repo, "scripts/structure/tests/test_tests_location.py")
    assert _run(repo, capsys, ["--only", "scripts/structure/tests/test_tests_location.py"]) == (
        0,
        "",
        "",
    )


def test_a_registry_entry_gone_stale_is_found_whichever_file_the_commit_names(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _track(repo, "tests/base/test_other.py")
    _git(repo, "rm", "-q", "-f", "tests/base/test_x.py")
    _track(repo, "base/net/tests/test_p.py")
    for argv in (["tests/base/test_other.py"], ["--only", "base/net/tests/test_p.py"]):
        status, out, _ = _run(repo, capsys, argv)
        assert status == 1
        assert "stale entry `tests/base/test_x.py`" in out


def test_a_missing_explicit_path_is_an_error(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, err = _run(repo, capsys, ["tests/base/test_nope.py"])
    assert (status, out) == (1, "")
    assert "target path(s) not found: tests/base/test_nope.py" in err


def test_a_missing_only_path_is_an_error(repo: pathlib.Path) -> None:
    with pytest.raises(SystemExit, match=r"target path\(s\) not found: tests/base/test_nope.py"):
        tl.main(["--only", "tests/base/test_nope.py"], repo_root=repo, allowed={})


# ------------------------------------------------------------------ where the checkout sits


def test_the_verdicts_do_not_depend_on_where_the_checkout_sits(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every judgment is on the path below the repository root: a checkout under directories
    named `tests`, `e2e`, `tmp`, `build`, `ui` or `fixtures` is judged like one anywhere else."""
    verdicts: list[tuple[int, list[str]]] = []
    for root in (
        tmp_path / "plain",
        tmp_path / "tests" / "e2e" / "tmp" / "build" / "ui" / "fixtures" / "repo",
    ):
        _make_repo(root)
        _track(root, "tests/base/test_new.py")
        _track(root, "tests/e2e/test_flow.py")
        _track(root, "tests/ui/test_app.py")
        _track(root, "base/net/tests/test_p.py")
        for argv in ([], ["--only", "tests/base/test_new.py", "tests/e2e/test_flow.py"]):
            status, out, _ = _run(root, capsys, argv)
            verdicts.append((status, _flagged(out)))
        status, out, _ = _run(
            root, capsys, ["--only", "tests/e2e/test_flow.py", "tests/ui/test_app.py"]
        )
        verdicts.append((status, _flagged(out)))
    assert verdicts[:3] == verdicts[3:]
    assert verdicts[:3] == [(1, ["tests/base/test_new.py"])] * 2 + [(0, [])]


# ------------------------------------------------------------------ the checks never read the code


def test_the_checks_do_not_load_the_placement_rule(repo: pathlib.Path) -> None:
    """A hook run is a path lookup: no module index, no import graph, no `place()`."""
    _track(repo, "tests/base/test_new.py")
    code = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(_ROOT)!r})
        from scripts.structure import tests_location as tl
        status = tl.main([], repo_root=Path({str(repo)!r}), allowed={{}})
        assert status == 1
        loaded = [m for m in sys.modules if m.startswith("scripts.structure.placement")]
        assert not loaded, loaded
        assert "scripts.structure.tests_location_suggest" not in sys.modules
        """
    )
    done = subprocess.run(  # noqa: S603 — fixed interpreter, test-owned source
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


# ------------------------------------------------------------------ the shipped registry


def test_by_design_covers_the_placement_policy_and_keeps_to_one_line_reasons() -> None:
    by_design = tests_location_allowed.BY_DESIGN
    for prefix in placement.TOP_LEVEL_PREFIXES:
        assert prefix in by_design
    for rel in placement.TOP_LEVEL_FILES:
        if tl.is_top_level_test(rel):
            assert tl.stays_by_design(rel) is not None, rel
    assert all(reason.strip() and "\n" not in reason for reason in by_design.values())


def test_every_shipped_entry_has_a_known_category_and_a_reason() -> None:
    for rel, (category, reason) in tests_location_allowed.ALLOWED.items():
        assert tl.is_top_level_test(rel), rel
        assert category in {"contract", "integration"}, rel
        assert reason.strip() and "\n" not in reason, rel


# ------------------------------------------------------------------ --suggest (on demand, reads the code)


def _suggest_repo(root: pathlib.Path, text: str) -> str:
    _track(root, "tests/agent/test_probe.py", text)
    return tests_location_suggest.suggest("tests/agent/test_probe.py", root)


def test_suggest_names_the_lowest_package_that_may_hold_the_test(repo: pathlib.Path) -> None:
    text = "from agent.own import hosted\nfrom base.net import retry\n\ndef test_a():\n    hosted.admit()\n    retry.backoff()\n"
    message = _suggest_repo(repo, text)
    assert "put it in agent/own/tests/test_probe.py" in message
    assert "references agent.own.hosted (unit agent)" in message


def test_suggest_says_why_no_package_can_hold_a_test_across_peer_units(
    repo: pathlib.Path,
) -> None:
    text = "from agent.own import hosted\nfrom ops.wake import spawn\n\ndef test_a():\n    hosted.admit()\n    spawn.spawn()\n"
    message = _suggest_repo(repo, text)
    assert "no package can hold this test" in message
    assert "agent, ops" in message
    assert "`integration`" in message


def test_suggest_a_test_with_no_first_party_reference(repo: pathlib.Path) -> None:
    message = _suggest_repo(repo, "def test_a():\n    assert True\n")
    assert "references no first-party package" in message
    assert "`contract`" in message


def test_suggest_a_by_design_test_is_told_it_stays(repo: pathlib.Path) -> None:
    _track(repo, "tests/e2e/test_flow.py")
    message = tests_location_suggest.suggest("tests/e2e/test_flow.py", repo)
    assert "stays at the top level by design" in message


# ------------------------------------------------------------------ no baseline to add debt to


def test_no_frozen_section_exists_any_more() -> None:
    """A top-level test is registered or refused: the structure gate keeps no `tests_location`
    section, and no shipped baseline shard carries one."""
    assert "tests_location" not in locality.SECTIONS
    assert "tests_location" not in locality.EXTERNAL_SECTIONS
    assert not hasattr(tl, "read_baseline")
    for shard in (_ROOT / "scripts/structure/baseline").glob("*.json"):
        assert "tests_location" not in shard.read_text(encoding="utf-8"), shard.name
