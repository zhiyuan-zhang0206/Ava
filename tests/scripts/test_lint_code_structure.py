"""Structure budgets, shrink-only baseline, and the existing AST-rule scope."""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from scripts import lint_code_structure as lcs


def _write(root: pathlib.Path, name: str, n_lines: int) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * n_lines, encoding="utf-8")
    return path


def _baseline(
    root: pathlib.Path,
    *,
    files: dict[str, int] | None = None,
    directories: dict[str, int] | None = None,
) -> pathlib.Path:
    path = root / "scripts/structure/baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"directories": directories or {}, "files": files or {}}, indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )
    return path


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
    _baseline(tmp_path)


@pytest.mark.parametrize("lines", [600, 601, 700, 800, 801])
@pytest.mark.parametrize("scope", ["shared", "tests", "scripts"])
def test_line_budget_boundary(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], scope: str, lines: int
) -> None:
    _write(tmp_path, f"{scope}/example.py", lines)

    assert lcs.main([]) == (1 if lines > 800 else 0)

    output = capsys.readouterr().out
    if lines > 800:
        assert f"{scope}/example.py:801:" in output
        assert "new violation, not in the baseline" in output
        assert "hard ceiling" in output
    else:
        assert output == ""


def test_directory_cap_counts_py_pyi_and_subdirectories(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _entries(tmp_path, "tests/package", 18)
    _write(directory, "types.pyi", 1)
    (directory / "child").mkdir()
    _write(directory, "README.md", 1)
    _write(directory, "config.json", 1)
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""

    _write(directory, "extra.pyi", 1)
    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "tests/package: directory has 21 direct entries" in output
    assert "new violation, not in the baseline" in output


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


@pytest.mark.parametrize("kind", ["files", "directories"])
@pytest.mark.parametrize("change", ["unlisted", "equal", "smaller", "larger"])
def test_baseline_contains_current_violations(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    change: str,
) -> None:
    if kind == "files":
        name, frozen = "tests/oversized.py", 805
        actual = frozen + {"unlisted": 0, "equal": 0, "smaller": -1, "larger": 1}[change]
        _write(tmp_path, name, actual)
    else:
        name, frozen = "tests/package", 25
        actual = frozen + {"unlisted": 0, "equal": 0, "smaller": -1, "larger": 1}[change]
        _entries(tmp_path, name, actual)
    baseline: dict[str, dict[str, int]] = {"files": {}, "directories": {}}
    if change != "unlisted":
        baseline[kind][name] = frozen
    _baseline(tmp_path, **baseline)

    assert lcs.main([]) == (1 if change in {"unlisted", "larger"} else 0)
    output = capsys.readouterr().out
    if change == "unlisted":
        assert name in output
        assert "new violation, not in the baseline" in output
    elif change == "larger":
        assert name in output
        assert "grew above its frozen baseline value" in output
        assert str(frozen) in output
    else:
        assert output == ""


@pytest.mark.parametrize("kind", ["files", "directories"])
@pytest.mark.parametrize("change", ["added", "raised", "removed", "lowered", "equal"])
@pytest.mark.parametrize("explicit", [False, True])
def test_baseline_guard_against_real_git_head(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    change: str,
    explicit: bool,
) -> None:
    name, frozen = ("tests/legacy.py", 805) if kind == "files" else ("tests/legacy", 25)
    baseline: dict[str, dict[str, int]] = {"files": {}, "directories": {}}
    baseline[kind][name] = frozen
    _baseline(tmp_path, **baseline)
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", "scripts/structure/baseline.json")
    _git(tmp_path, "commit", "--quiet", "-m", "Freeze baseline")
    if change == "added":
        extra = "tests/new.py" if kind == "files" else "tests/new"
        baseline[kind][extra] = frozen
    elif change == "removed":
        del baseline[kind][name]
    elif change in {"raised", "lowered"}:
        baseline[kind][name] += 1 if change == "raised" else -1
    _baseline(tmp_path, **baseline)
    target = _write(tmp_path, "tests/selected.py", 1)

    assert lcs.main([str(target)] if explicit else []) == (
        1 if change in {"added", "raised"} else 0
    )
    captured = capsys.readouterr()
    assert "guard skipped" not in captured.err
    if change in {"added", "raised"}:
        assert f"{change} {kind} entry" in captured.out
        assert "shrink-only" in captured.out
    else:
        assert captured.out == ""
        assert captured.err == ""


def test_baseline_introduction_skips_guard_when_absent_from_head(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "--quiet")
    _write(tmp_path, "README.md", 1)
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "--quiet", "-m", "Before baseline introduction")
    _write(tmp_path, "tests/oversized.py", 801)
    _baseline(tmp_path, files={"tests/oversized.py": 801})

    assert lcs.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "baseline guard skipped" in captured.err


def test_non_git_checkout_skips_guard(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lcs.main([]) == 0
    assert "baseline guard skipped" in capsys.readouterr().err


@pytest.mark.parametrize(
    "data",
    [
        "not JSON",
        "[]",
        "{}",
        '{"files": {}}',
        '{"files": {}, "directories": {}, "extra": {}}',
        '{"files": [], "directories": {}}',
        '{"files": {}, "directories": []}',
        '{"files": {"tests/big.py": true}, "directories": {}}',
        '{"files": {"tests/big.py": "801"}, "directories": {}}',
        '{"files": {"tests/big.py": 801.5}, "directories": {}}',
        '{"files": {}, "directories": {"tests/package": "21"}}',
        '{"files": {"../big.py": 801}, "directories": {}}',
        '{"files": {"/tests/big.py": 801}, "directories": {}}',
        '{"files": {"tests/big.txt": 801}, "directories": {}}',
    ],
)
def test_invalid_baseline_is_an_actionable_error(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], data: str
) -> None:
    (tmp_path / "scripts/structure/baseline.json").write_text(data, encoding="utf-8")
    assert lcs.main([]) == 1
    assert "scripts/structure/baseline.json: invalid baseline" in capsys.readouterr().err


def test_missing_baseline_is_an_actionable_error(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "scripts/structure/baseline.json").unlink()
    assert lcs.main([]) == 1
    assert "scripts/structure/baseline.json: invalid baseline" in capsys.readouterr().err


def test_directory_with_unreadable_member_is_skipped(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = _entries(tmp_path, "shared", 1)
    (shared / "bad_utf8.py").write_bytes(b"\xff\xfe\x00bad")
    (shared / "dangling.py").symlink_to(shared / "missing.py")
    assert lcs.main([str(shared / "bad_utf8.py")]) == 0
    assert lcs.main([str(shared)]) == 0
    assert lcs.main([]) == 0
    _write(shared, "big.py", 901)
    assert lcs.main([str(shared)]) == 1
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
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", "scripts/structure/baseline.json")
    _git(tmp_path, "commit", "--quiet", "-m", "Freeze empty baseline")
    directory = _entries(tmp_path, "docs", 21)
    path = _write(directory, "oversized.py", 801)
    args = [str(directory if target_is_directory else path)]
    assert lcs.main(args) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""

    _baseline(tmp_path, files={"tests/unrelated.py": 801})
    assert lcs.main(args) == 1
    assert "added files entry tests/unrelated.py" in capsys.readouterr().out


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
        "shared",
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
    path = _write(tmp_path, "shared/example.py", 0)
    path.write_text(
        "if typing.TYPE_CHECKING:\n    import example\nmachine_role()\n", encoding="utf-8"
    )
    monkeypatch.setattr(lcs, "_TYPE_CHECKING_ALLOWED", frozenset({"shared/example.py"}))
    monkeypatch.setattr(lcs, "_MACHINE_ROLE_ALLOWED", {"shared/example.py": "Test host capability"})
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""

    path.write_text("value = 1\n", encoding="utf-8")
    assert lcs.main([]) == 1
    assert "shared/example.py:1: stale machine_role() allowlist entry" in capsys.readouterr().out


@pytest.mark.parametrize("destination_scope", ["shared", "docs"])
def test_explicit_alias_preserves_resolved_ast_scope(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], destination_scope: str
) -> None:
    destination = _write(tmp_path, f"{destination_scope}/original.py", 801)
    with destination.open("a", encoding="utf-8") as stream:
        stream.write("if TYPE_CHECKING:\n    import example\n")
    alias_scope = "docs" if destination_scope == "shared" else "shared"
    alias = tmp_path / alias_scope / "alias.py"
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(destination)

    assert lcs.main([str(alias)]) == (1 if destination_scope == "shared" else 0)
    output = capsys.readouterr().out
    assert "hard ceiling" not in output
    if destination_scope == "shared":
        assert "shared/original.py:802:" in output
        assert "TYPE_CHECKING" in output
    else:
        assert output == ""
