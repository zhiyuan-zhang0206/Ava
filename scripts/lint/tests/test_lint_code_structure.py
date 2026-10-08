"""Structure budgets, shrink-only baseline, and the existing AST-rule scope."""

from __future__ import annotations

import ast
import json
import pathlib
import subprocess
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.lint import code_structure as lcs
from scripts.structure import baseline_shards
from scripts.structure.budgets import quality_budget as quality


def _write(root: pathlib.Path, name: str, n_lines: int) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * n_lines, encoding="utf-8")
    return path


def _clear_baseline_dir(root: pathlib.Path) -> pathlib.Path:
    """The shard directory, emptied of any shard files already there (created if
    absent) and carrying its README.md — read_worktree() requires the directory
    to exist, and the README is what keeps git tracking it even with zero shards."""
    directory = root / baseline_shards.SHARD_DIR
    if directory.is_dir():
        for path in directory.rglob("*.json"):
            path.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    return directory


def _baseline(
    root: pathlib.Path,
    *,
    files: dict[str, int] | None = None,
    directories: dict[str, int] | None = None,
    complexity: dict[str, int] | None = None,
    nesting: dict[str, int] | None = None,
    patch_targets: dict[str, int] | None = None,
) -> pathlib.Path:
    """Write the baseline as shards under scripts/structure/baseline/."""
    directory = _clear_baseline_dir(root)
    data = {
        "directories": directories or {},
        "files": files or {},
        "complexity": complexity or {},
        "nesting": nesting or {},
        "patch_targets": patch_targets or {},
        **{kind: {} for kind in ("private_imports", "owner_bypasses", "path_imports")},
    }
    for name, shard in baseline_shards.split(data).items():
        # String concat, not `/`: a test-only key can produce a shard name
        # starting with "/", which `directory / name` would treat as absolute.
        pathlib.Path(f"{directory}/{name}.json").parent.mkdir(parents=True, exist_ok=True)
        (pathlib.Path(f"{directory}/{name}.json")).write_text(
            baseline_shards.render(shard), encoding="utf-8"
        )
    return directory


def _entries(root: pathlib.Path, name: str, count: int) -> pathlib.Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        _write(directory, f"entry_{index}.py", 1)
    return directory


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — arguments are fixed test commands, never external input.
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Structure gate test",
            "-c",
            "user.email=structure-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(autouse=True)
def _isolated_repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every main() call scans and invokes Git only in its own temporary root."""
    monkeypatch.setattr(lcs, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("LINT_STRUCTURE_BASELINE_BASE", raising=False)
    _baseline(tmp_path)


@pytest.mark.parametrize("lines", [600, 601, 700, 800, 801])
@pytest.mark.parametrize("scope", ["base", "tests", "scripts"])
def test_line_budget_boundary(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], scope: str, lines: int
) -> None:
    _write(tmp_path, f"{scope}/example.py", lines)

    assert lcs.main([]) == (1 if lines > 800 else 0)

    output = capsys.readouterr().out
    if lines > 800:
        assert f"{scope}/example.py:801:" in output
        assert "split it" in output
        assert "hard ceiling" in output
    else:
        assert output == ""


def test_directory_cap_counts_py_pyi_and_subdirectories(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _entries(tmp_path, "tests/package", 18)
    _write(directory, "types.pyi", 1)
    _write(directory / "child", "module.py", 1)
    _write(directory, "README.md", 1)
    _write(directory, "config.json", 1)
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""

    _write(directory, "extra.pyi", 1)
    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "tests/package: directory has 21 direct entries" in output
    assert "split it" in output


def test_directory_budgets_are_recursive_and_independent(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = _entries(tmp_path, "tests/package", 19)
    child = _entries(parent, "child", 20)
    assert lcs.main([str(parent)]) == 0
    assert capsys.readouterr().out == ""

    _write(child, "extra.py", 1)
    assert lcs.main([str(parent)]) == 1
    output = capsys.readouterr().out
    assert "tests/package/child: directory has 21 direct entries" in output
    assert "tests/package: directory" not in output


def test_hidden_cache_migrations_and_symlink_entries_are_exempt(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _entries(tmp_path, "tests/package", 20)
    _write(directory, ".hidden.py", 801)
    for name in (".hidden", "__pycache__", "migrations"):
        excluded = _entries(directory, name, 21)
        _write(excluded, "oversized.py", 801)
        _write(excluded, "nested/oversized.py", 801)
    external = _entries(tmp_path, "docs/linked", 21)
    external_file = _write(external, "oversized.py", 801)
    (directory / "linked.py").symlink_to(external_file)
    (directory / "linked_dir").symlink_to(external, target_is_directory=True)
    (directory / "dangling.py").symlink_to(directory / "missing.py")

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("name", [".hidden", "__pycache__", "migrations"])
@pytest.mark.parametrize("target_file", [False, True])
def test_explicit_excluded_targets_stay_exempt(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], name: str, target_file: bool
) -> None:
    directory = _entries(tmp_path, f"tests/{name}", 21)
    path = _write(directory, "oversized.py", 801)
    assert lcs.main([str(path if target_file else directory)]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("scope", ["docs", "ui"])
def test_docs_and_frontend_are_out_of_scope(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], scope: str
) -> None:
    directory = _entries(tmp_path, scope, 21)
    _write(directory, "oversized.py", 801)
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


def test_baseline_introduction_skips_guard_when_absent_from_head(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "--quiet")
    _write(tmp_path, "README.md", 1)
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "--quiet", "-m", "Before baseline introduction")
    _write(tmp_path, "tests/oversized.py", 801)
    _baseline(tmp_path)

    assert lcs.main([]) == 1
    captured = capsys.readouterr()
    assert "hard ceiling" in captured.out
    assert "baseline guard skipped" in captured.err


def test_non_git_checkout_skips_guard(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lcs.main([]) == 0
    assert "baseline guard skipped" in capsys.readouterr().err


# Malformed/misfiled/missing baseline shards: test_baseline_shard_validity_gate.py.


def test_directory_with_unreadable_member_is_skipped(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = _entries(tmp_path, "base", 1)
    (package / "bad_utf8.py").write_bytes(b"\xff\xfe\x00bad")
    (package / "dangling.py").symlink_to(package / "missing.py")
    assert lcs.main([str(package / "bad_utf8.py")]) == 0
    assert lcs.main([str(package)]) == 0
    assert lcs.main([]) == 0
    _write(package, "big.py", 901)
    assert lcs.main([str(package)]) == 1
    assert "hard ceiling" in capsys.readouterr().out


def test_explicit_missing_target_is_an_error(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _write(tmp_path, "tests/ok.py", 1)
    missing = tmp_path / "typo.py"
    for args in ([str(missing)], [str(good), str(missing)]):
        assert lcs.main(args) == 1
        assert f"error: target path(s) not found: {missing}" in capsys.readouterr().err


@pytest.mark.parametrize("target_is_directory", [False, True])
def test_explicit_out_of_scope_target_is_silent_but_guard_still_runs(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], target_is_directory: bool
) -> None:
    _baseline(tmp_path)
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", baseline_shards.SHARD_DIR)
    _git(tmp_path, "commit", "--quiet", "-m", "Freeze baseline")
    directory = _entries(tmp_path, "docs", 21)
    path = _write(directory, "oversized.py", 801)
    args = [str(directory if target_is_directory else path)]
    assert lcs.main(args) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""

    _baseline(tmp_path, files={"tests/unrelated.py": 801})
    assert lcs.main(args) == 1
    assert "unknown section 'files'" in capsys.readouterr().err


def test_explicit_file_checks_parent_count_without_scanning_siblings(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _entries(tmp_path, "tests/package", 19)
    target = _write(directory, "selected.py", 1)
    _write(directory, "entry_0.py", 801)
    assert lcs.main([str(target)]) == 0
    assert capsys.readouterr().out == ""

    _write(directory, "extra.py", 1)
    assert lcs.main([str(target)]) == 1
    output = capsys.readouterr().out
    assert "tests/package: directory has 21 direct entries" in output
    assert "entry_0.py" not in output


def test_explicit_directory_checks_itself_and_descendants_only(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    selected = _entries(tmp_path, "tests/selected", 20)
    _entries(tmp_path, "tests/unrelated", 21)
    _write(tmp_path, "tests/unrelated/oversized.py", 801)
    _write(selected, "nested/oversized.py", 801)

    assert lcs.main([str(selected)]) == 1
    output = capsys.readouterr().out
    assert "tests/selected: directory has 21 direct entries" in output
    assert "tests/selected/nested/oversized.py:801:" in output
    assert "unrelated" not in output


def test_explicit_repository_root_reaches_budget_scope(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "scripts/oversized.py", 801)
    assert lcs.main([str(tmp_path)]) == 1
    assert "scripts/oversized.py:801:" in capsys.readouterr().out


@pytest.mark.parametrize(
    "scope",
    [
        "agent",
        "ava",
        "ava_builtins",
        "gateway",
        "base",
        "services",
        "ops",
        "cli",
        "tests",
        "scripts",
    ],
)
def test_ast_rules_retain_the_eight_package_scope(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], scope: str
) -> None:
    path = _write(tmp_path, f"{scope}/example.py", 0)
    path.write_text("if TYPE_CHECKING:\n    import example\nmachine_role()\n", encoding="utf-8")

    governed = scope not in {"tests", "scripts"}
    assert lcs.main([]) == (1 if governed else 0)
    output = capsys.readouterr().out
    if governed:
        assert f"{scope}/example.py:1:" in output
        assert "`if TYPE_CHECKING:` is banned" in output
        assert f"{scope}/example.py:3:" in output
        assert "machine_role() may only be called" in output
    else:
        assert output == ""


def test_ast_allowlists_and_stale_role_entry_are_preserved(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "base/example.py", 0)
    path.write_text(
        "if typing.TYPE_CHECKING:\n    import example\ndef ask():\n    machine_role()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(lcs, "_TYPE_CHECKING_ALLOWED", frozenset({"base/example.py"}))
    monkeypatch.setattr(lcs, "_MACHINE_ROLE_ALLOWED", {"base/example.py": "Test host capability"})
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""

    path.write_text("value = 1\n", encoding="utf-8")
    assert lcs.main([]) == 1
    assert "base/example.py:1: stale machine_role() allowlist entry" in capsys.readouterr().out


@pytest.mark.parametrize("destination_scope", ["base", "docs"])
def test_explicit_alias_preserves_resolved_ast_scope(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], destination_scope: str
) -> None:
    destination = _write(tmp_path, f"{destination_scope}/original.py", 801)
    with destination.open("a", encoding="utf-8") as stream:
        stream.write("if TYPE_CHECKING:\n    import example\n")
    alias_scope = "docs" if destination_scope == "base" else "base"
    alias = tmp_path / alias_scope / "alias.py"
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(destination)

    assert lcs.main([str(alias)]) == (1 if destination_scope == "base" else 0)
    output = capsys.readouterr().out
    assert "hard ceiling" not in output
    if destination_scope == "base":
        assert "base/original.py:802:" in output
        assert "TYPE_CHECKING" in output
    else:
        assert output == ""


def _source(root: pathlib.Path, source: str, name: str = "tests/q.py") -> pathlib.Path:
    path = _write(root, name, 0)
    path.write_text(source, encoding="utf-8")
    return path


def _branches(cc: int) -> str:
    return "def f(x):\n" + "    if x: pass\n" * (cc - 1) + "    return x\n"


def _nested(depth: int) -> str:
    body = "".join("    " * i + "if x:\n" for i in range(1, depth + 1))
    return f"def f(x):\n{body}{'    ' * (depth + 1)}pass\n"


def _commit_baseline(root: pathlib.Path) -> None:
    _git(root, "add", baseline_shards.SHARD_DIR)
    _git(root, "commit", "--quiet", "-m", "Baseline snapshot")


@pytest.mark.parametrize(
    "kind,value,rc",
    [("complexity", 15, 1), ("complexity", 14, 0), ("nesting", 5, 0), ("nesting", 6, 1)],
)
def test_quality_boundaries(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], kind: str, value: int, rc: int
) -> None:
    _source(tmp_path, _branches(value) if kind == "complexity" else _nested(value))
    assert lcs.main([]) == rc
    captured = capsys.readouterr()
    assert (f"tests/q.py::f: {kind} {value}" in captured.out) == bool(rc)
    assert (
        "complexity warnings (cc 10-14, non-blocking): 1 functions in 1 files" in captured.err
    ) == (kind == "complexity" and value == 14)


def test_function_qualnames_duplicates_and_lambda_exclusion() -> None:
    tree = ast.parse(
        "class Outer:\n    class Inner:\n        def method(self): pass\ndef p():\n    def c(): pass\n    def c(): pass\n    return lambda: 1\nasync def p(): pass\n"
        "try:\n    pass\nexcept Exception:\n    def recovered(): pass\n"
        "match value:\n    case 1:\n        def matched(): pass\n"
    )
    measured = quality.measure_quality(tree, "tests/q.py")
    expected = {
        "tests/q.py::Outer.Inner.method",
        "tests/q.py::p",
        "tests/q.py::p.<locals>.c",
        "tests/q.py::p.<locals>.c#2",
        "tests/q.py::p#2",
        "tests/q.py::recovered",
        "tests/q.py::matched",
    }
    assert set(measured["complexity"]) == set(measured["nesting"]) == expected
    assert set(measured["complexity"].values()) == {1}


def test_function_local_class_method_and_closure_are_measured() -> None:
    tree = ast.parse(
        "def outer():\n    class Local:\n        def method(self):\n            def closure(x):\n                if x: return 1\n            return closure\n"
    )
    assert quality.measure_quality(tree, "tests/q.py") == {
        "complexity": {
            "tests/q.py::outer": 1,
            "tests/q.py::outer.<locals>.Local.method": 1,
            "tests/q.py::outer.<locals>.Local.method.<locals>.closure": 2,
        },
        "nesting": {
            "tests/q.py::outer": 0,
            "tests/q.py::outer.<locals>.Local.method": 0,
            "tests/q.py::outer.<locals>.Local.method.<locals>.closure": 1,
        },
    }


@pytest.mark.parametrize(
    "body,depth",
    [
        ("if x:\n    pass\nelif y:\n    pass\nelif z:\n    pass\n", 1),
        ("if x:\n    pass\nelse:\n    if y: pass\n", 2),
        ("try:\n    pass\nexcept Exception:\n    pass\nelse:\n    pass\nfinally:\n    pass\n", 1),
        ("try:\n    pass\nexcept Exception:\n    if x: pass\n", 2),
        ("try:\n    pass\nexcept Exception:\n    pass\nelse:\n    if x: pass\n", 2),
        ("try:\n    pass\nfinally:\n    if x: pass\n", 2),
        ("with a, b:\n    value = [x for x in xs if x]\n    fn = lambda: 1 if x else 0\n", 1),
        (
            "async with a, b:\n    async for x in xs:\n        while x:\n            for y in ys: pass\n",
            4,
        ),
        ("match x:\n    case 1:\n        if y: pass\n", 2),
        (
            "@decorate([x for x in xs if x])\ndef nested():\n    if x:\n        if y: pass\nclass Local:\n    if x:\n        if y: pass\n",
            0,
        ),
    ],
)
def test_nesting_semantics(body: str, depth: int) -> None:
    assert quality.max_depth(ast.parse(body).body) == depth


@pytest.mark.parametrize("count", [0, 1, 30, 32])
@pytest.mark.parametrize("full", [False, True])
def test_complexity_warning_summary_order_and_folding(
    capsys: pytest.CaptureFixture[str], count: int, full: bool
) -> None:
    scores = {f"tests/{i:02}.py::f": 10 for i in reversed(range(count))}
    if count:
        scores["tests/00.py::g"] = 14
    scores.update({"tests/silent.py::low": 9, "tests/silent.py::high": 15})
    quality.render_warnings(scores, full=full)
    lines = capsys.readouterr().err.splitlines()
    if not count:
        assert lines == []
        return
    assert (
        lines[0]
        == f"complexity warnings (cc 10-14, non-blocking): {count + 1} functions in {count} files"
    )
    visible = count if full else min(count, 30)
    assert lines[1 : visible + 1] == [
        f"tests/{i:02}.py: {2 if i == 0 else 1}" for i in range(visible)
    ]
    assert lines[visible + 1 :] == (
        [f"rest: {count - 30} files / {count - 30} functions"] if count > 30 and not full else []
    )


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("flag_first", [False, True])
def test_explicit_quality_targets_and_full_flag(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], directory: bool, flag_first: bool
) -> None:
    selected = _source(tmp_path, _branches(10), "tests/chosen/a.py")
    _source(tmp_path, _branches(15), "tests/other/hard.py")
    _source(tmp_path, _branches(14), "tests/other/warn.py")
    args = [str(selected.parent if directory else selected)]
    args.insert(0 if flag_first else len(args), "--complexity-warnings-full")
    assert lcs.main(args) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "1 functions in 1 files\ntests/chosen/a.py: 1\n" in captured.err
    assert "tests/other" not in captured.err
    assert lcs.main([]) == 1
    captured = capsys.readouterr()
    assert "tests/other/hard.py::f" in captured.out
    assert "2 functions in 2 files" in captured.err


@pytest.mark.parametrize(
    "kind,key,value",
    [
        ("patch_targets", "base/q.py::base.db._pool", 2),
    ],
)
@pytest.mark.parametrize("delta", [-1, 1])
@pytest.mark.parametrize("base", ["explicit", "origin"])
def test_committed_baseline_change_uses_base_revision(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    key: str,
    value: int,
    delta: int,
    base: str,
) -> None:
    _baseline(tmp_path, **{kind: {key: value}})
    _git(tmp_path, "init", "--quiet")
    _commit_baseline(tmp_path)
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _baseline(tmp_path, **{kind: {key: value + delta}})
    _commit_baseline(tmp_path)
    if base == "explicit":
        monkeypatch.setenv("LINT_STRUCTURE_BASELINE_BASE", "HEAD~1")
        _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert lcs.main([]) == (1 if delta > 0 else 0)
    captured = capsys.readouterr()
    assert (f"raised {kind} entry {key}" in captured.out) == (delta > 0)
    assert "guard skipped" not in captured.err


@pytest.mark.parametrize("ref", ["missing-structure-base", "--octopus", "--independent", ""])
def test_explicit_unresolvable_base_fails(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ref: str,
) -> None:
    _git(tmp_path, "init", "--quiet")
    _commit_baseline(tmp_path)
    monkeypatch.setenv("LINT_STRUCTURE_BASELINE_BASE", ref)
    assert lcs.main([]) == 1
    assert f"LINT_STRUCTURE_BASELINE_BASE={ref!r} cannot resolve" in capsys.readouterr().out


@pytest.mark.parametrize("previous,rc", [("valid", 0), ("raised", 1), ("malformed", 1)])
def test_guard_rejects_an_invalid_base_baseline(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], previous: str, rc: int
) -> None:
    # A malformed base shard: "invalid base baseline", not "invalid baseline".
    if previous == "malformed":
        directory = _clear_baseline_dir(tmp_path)
        (directory / "tests.json").write_text("not JSON", encoding="utf-8")
    else:
        _baseline(tmp_path, patch_targets={"base/q.py::base.db._pool": 2})
    _git(tmp_path, "init", "--quiet")
    _commit_baseline(tmp_path)
    _baseline(
        tmp_path, patch_targets={"base/q.py::base.db._pool": 3 if previous == "raised" else 2}
    )

    assert lcs.main([]) == rc
    captured = capsys.readouterr()
    if previous == "malformed":
        assert "invalid base baseline" in captured.out
    elif previous == "raised":
        assert "raised patch_targets entry base/q.py::base.db._pool" in captured.out
    else:
        assert captured.out == ""


def test_ast_and_radon_share_one_parse(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source(tmp_path, _branches(2), "base/q.py")
    parse = Mock(wraps=ast.parse)
    measure = Mock(wraps=quality.measure_quality)
    visitor = Mock(wraps=cast(Any, quality.ComplexityVisitor).from_ast)
    monkeypatch.setattr(ast, "parse", parse)
    monkeypatch.setattr(quality, "measure_quality", measure)
    monkeypatch.setattr(quality.ComplexityVisitor, "from_ast", visitor)
    assert lcs.main([]) == 0
    assert parse.call_count == 1
    assert visitor.call_count == 1
    assert visitor.call_args.args[0] is measure.call_args.args[0]


@pytest.mark.parametrize("kind", ["files", "directories", "complexity", "nesting"])
@pytest.mark.parametrize("entries", [{}, {"tests/q.py::f": 999}])
def test_retired_budget_sections_cannot_be_reintroduced(kind: str, entries: dict[str, int]) -> None:
    with pytest.raises(ValueError, match=f"unknown section '{kind}'"):
        lcs._parse_baseline({"tests": json.dumps({kind: entries})})


def test_zeroed_historical_budgets_do_not_restore_exemptions() -> None:
    retired: dict[str, dict[str, int]] = {
        kind: {} for kind in ("files", "directories", "complexity", "nesting")
    }
    result = lcs._parse_baseline({"tests": json.dumps(retired)}, historical=True)
    assert set(result) == set(lcs._SITE_SECTIONS)
    retired["files"] = {"tests/big.py": 900}
    with pytest.raises(ValueError, match="retired files baseline must be empty"):
        lcs._parse_baseline({"tests": json.dumps(retired)}, historical=True)
