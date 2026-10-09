"""What counts as a direct entry for the 20-entry directory budget."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any, cast

import pytest
import yaml

from scripts.lint import code_structure as lcs
from scripts.structure import baseline_shards


@pytest.fixture(autouse=True)
def _isolated_repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every main() call scans only its own temporary root, with an empty baseline:
    the shard directory must exist (read_worktree() fails fast otherwise), so it
    gets its README.md and no shard files — a legitimate empty baseline."""
    monkeypatch.setattr(lcs, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("LINT_STRUCTURE_BASELINE_BASE", "HEAD")
    directory = tmp_path / baseline_shards.SHARD_DIR
    directory.mkdir(parents=True)
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    for args in (
        ("init", "--quiet"),
        ("config", "user.name", "Structure gate test"),
        ("config", "user.email", "structure-test@example.invalid"),
        ("add", baseline_shards.SHARD_DIR),
        ("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Empty baseline"),
    ):
        result = lcs._git(*args)
        assert result.returncode == 0, result.stderr


def _module(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")


def test_subdirectories_ci_never_checks_out_do_not_count(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A package renamed or removed locally can leave a directory holding only
    `__pycache__` (or nothing, or only hidden files); a fresh CI checkout never
    has it, so counting it would fail a commit locally that passes in CI."""
    package = tmp_path / "tests/package"
    for index in range(20):
        _module(package / f"entry_{index}.py")
    _track(package)
    (package / "removed_pkg" / "__pycache__").mkdir(parents=True)
    (package / "removed_pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"")
    (package / "empty").mkdir()
    (package / "hidden_only").mkdir()
    (package / "hidden_only" / ".DS_Store").write_bytes(b"")
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""

    _module(package / "real_pkg" / "module.py")
    _track(package / "real_pkg")
    assert lcs.main([]) == 1
    assert "tests/package: directory has 21 direct entries" in capsys.readouterr().out


def test_docs_layer_without_init_counts(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Component documentation takes the same parent slot as any other directory."""
    package = tmp_path / "tests/package"
    for index in range(20):
        _module(package / f"entry_{index}.py")
    (package / "docs").mkdir()
    (package / "docs" / "package.ava.okf.md").write_text("# doc\n", encoding="utf-8")
    _track(package)
    assert lcs.main([]) == 1
    assert "tests/package: directory has 21 direct entries" in capsys.readouterr().out


def test_docs_package_with_init_counts(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A directory named `docs` that has `__init__.py` is a real Python package
    (`base/packages/docs`) and takes a slot like any other subdirectory."""
    package = tmp_path / "tests/package"
    for index in range(20):
        _module(package / f"entry_{index}.py")
    _module(package / "docs" / "__init__.py")
    _track(package)
    assert lcs.main([]) == 1
    assert "tests/package: directory has 21 direct entries" in capsys.readouterr().out


def _track(*paths: pathlib.Path) -> None:
    result = lcs._git("add", "--", *(str(path) for path in paths))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "directory", ["docs", "ui/src", ".hidden", "migrations", "tests", "base/pkg/tests"]
)
@pytest.mark.parametrize("count", [20, 21])
def test_every_tracked_directory_has_the_same_cap(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    directory: str,
    count: int,
) -> None:
    folder = tmp_path / directory
    folder.mkdir(parents=True)
    for index in range(count):
        (folder / f"entry_{index}.md").write_text("Documentation.\n", encoding="utf-8")
    _track(folder)

    assert lcs.main([]) == int(count > 20)
    output = capsys.readouterr().out
    assert (f"{directory}: directory has 21 direct entries" in output) == (count > 20)


@pytest.mark.parametrize("extension", [".py", ".pyi", ".ts", ".md", ".sql", ".png"])
def test_changed_files_of_every_suffix_check_their_directory(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], extension: str
) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    for index in range(20):
        (folder / f"entry_{index}.md").write_text("Documentation.\n", encoding="utf-8")
    changed = folder / f"extra{extension}"
    changed.write_bytes(b"x = 1\n")
    _track(folder)

    assert lcs.main(["--only", str(changed)]) == 1
    assert "docs: directory has 21 direct entries" in capsys.readouterr().out


def test_tracked_hidden_members_and_docs_tests_migrations_take_parent_slots(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "base/package"
    folder.mkdir(parents=True)
    for index in range(17):
        _module(folder / f"entry_{index}.py")
    for name in ("docs", "tests", "migrations", ".hidden"):
        member = folder / name / "data.json"
        member.parent.mkdir()
        member.write_text("{}\n", encoding="utf-8")
    _track(folder)

    assert lcs.main([]) == 1
    assert "base/package: directory has 21 direct entries" in capsys.readouterr().out


def test_root_can_exceed_the_cap_but_its_child_cannot(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for index in range(21):
        (tmp_path / f"root_{index}.md").write_text("Root.\n", encoding="utf-8")
    _track(*(tmp_path.glob("root_*.md")))
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""
    folder = tmp_path / "docs"
    folder.mkdir()
    for index in range(21):
        (folder / f"entry_{index}.md").write_text("Child.\n", encoding="utf-8")
    _track(*(tmp_path.glob("root_*.md")), folder)

    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "docs: directory has 21 direct entries" in output
    assert "root_" not in output


def test_untracked_local_artifacts_do_not_add_slots(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    for index in range(20):
        (folder / f"entry_{index}.md").write_text("Tracked.\n", encoding="utf-8")
    _track(folder)
    for name in (".venv", "node_modules", "__pycache__"):
        (folder / name).mkdir()
        for index in range(30):
            (folder / name / f"local_{index}.bin").write_bytes(b"\x00")

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "name", ["line\nbreak.md", "tab\tvalue.md", 'quote"file.md', "space name.md"]
)
def test_git_filename_quoting_does_not_change_the_count(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    for index in range(20):
        (folder / f"entry_{index}.md").write_text("Tracked.\n", encoding="utf-8")
    changed = folder / name
    changed.write_text("Changed.\n", encoding="utf-8")
    _track(folder)

    assert lcs.main(["--only", str(changed)]) == 1
    assert "docs: directory has 21 direct entries" in capsys.readouterr().out


@pytest.mark.parametrize("target_kind", ["file", "directory", "missing"])
def test_a_tracked_symlink_takes_one_slot_without_traversal(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], target_kind: str
) -> None:
    external = tmp_path.parent / f"{tmp_path.name}-external"
    external.mkdir()
    for index in range(30):
        (external / f"entry_{index}.md").write_text("Outside.\n", encoding="utf-8")
    target = external if target_kind == "directory" else external / f"{target_kind}.md"
    if target_kind == "file":
        target.write_text("Outside.\n", encoding="utf-8")
    folder = tmp_path / "docs"
    folder.mkdir()
    for index in range(19):
        (folder / f"entry_{index}.md").write_text("Tracked.\n", encoding="utf-8")
    link = folder / "linked"
    link.symlink_to(target, target_is_directory=target_kind == "directory")
    _track(folder)

    assert lcs.main(["--only", str(link)]) == 0
    assert capsys.readouterr().out == ""
    (folder / "extra.md").write_text("Extra.\n", encoding="utf-8")
    _track(folder / "extra.md")
    assert lcs.main(["--only", str(link)]) == 1
    assert "docs: directory has 21 direct entries" in capsys.readouterr().out


def test_a_gitlink_takes_one_slot_without_scanning_local_contents(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    for index in range(19):
        (folder / f"entry_{index}.md").write_text("Tracked.\n", encoding="utf-8")
    _track(folder)
    vendor = folder / "vendor"
    vendor.mkdir()
    for index in range(30):
        (vendor / f"local_{index}.md").write_text("Uninitialized gitlink.\n", encoding="utf-8")
    revision = lcs._git("rev-parse", "HEAD").stdout.strip()
    result = lcs._git("update-index", "--add", "--cacheinfo", f"160000,{revision},docs/vendor")
    assert result.returncode == 0, result.stderr

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""
    (folder / "extra.md").write_text("Extra.\n", encoding="utf-8")
    _track(folder / "extra.md")
    assert lcs.main([]) == 1
    assert "docs: directory has 21 direct entries" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["exit", "truncated", "empty_path", "dot_path"])
def test_real_cli_fails_when_the_tracked_query_cannot_establish_structure(
    tmp_path: pathlib.Path, mode: str
) -> None:
    git = shutil.which("git")
    assert git is not None
    binaries = tmp_path / "bin"
    binaries.mkdir()
    shim = binaries / "git"
    output = {
        "exit": "raise SystemExit(73)",
        "truncated": "sys.stdout.write('docs/a.md')",
        "empty_path": "sys.stdout.write('\\0')",
        "dot_path": "sys.stdout.write('.\\0')",
    }[mode]
    shim.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        f"if 'ls-files' in sys.argv:\n    {output}\n    raise SystemExit(0)\n"
        f"os.execv({git!r}, [{git!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    shim.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
        "LINT_STRUCTURE_BASELINE_BASE": "HEAD",
    }
    result = subprocess.run(
        [".venv/bin/python", "scripts/lint/code_structure.py"],
        cwd=pathlib.Path(lcs.__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot read tracked directory structure" in result.stderr
    assert "directory has" not in result.stdout


def test_the_real_hook_selects_docs_hidden_links_and_gitlinks(
    tmp_path: pathlib.Path,
) -> None:
    """Run real pre-commit selection with its normal defaults and non-file tags."""
    root = pathlib.Path(lcs.__file__).parents[2]
    config = cast(dict[str, Any], yaml.safe_load((root / ".pre-commit-config.yaml").read_text()))
    local = next(repo for repo in config["repos"] if repo["repo"] == "local")
    selected = next(hook for hook in local["hooks"] if hook["id"] == "lint-code-structure")
    # The public entry seam reveals exactly the filenames pre-commit delivers.
    config["repos"] = [{**local, "hooks": [{**selected, "entry": "/bin/echo"}]}]
    (tmp_path / ".pre-commit-config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    runner = tmp_path / "pre-commit"
    runner.write_text(
        f"#!{sys.executable}\nfrom pre_commit.main import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    runner.chmod(0o700)
    names = ["docs/guide.md", "ui/component.ts", "db/migrations/001.sql", ".hidden/data.bin"]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Test.\n")
    (tmp_path / "docs/linked").symlink_to(tmp_path / "missing")
    names.append("docs/linked")
    (tmp_path / "docs/vendor").mkdir()
    names.append("docs/vendor")
    _track(*(tmp_path / name for name in names[:-1]))
    revision = lcs._git("rev-parse", "HEAD").stdout.strip()
    result = lcs._git("update-index", "--add", "--cacheinfo", f"160000,{revision},docs/vendor")
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [
            "./pre-commit",
            "run",
            "lint-code-structure",
            "--verbose",
            "--files",
            "docs/guide.md",
            "ui/component.ts",
            "db/migrations/001.sql",
            ".hidden/data.bin",
            "docs/linked",
            "docs/vendor",
        ],
        cwd=tmp_path,
        env={**os.environ, "PRE_COMMIT_HOME": str(tmp_path / "pre-commit-cache")},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "hook id: lint-code-structure" in result.stdout
    assert all(name in result.stdout for name in names)


def test_explicit_deep_target_checks_all_ancestors_without_an_unrelated_sibling(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    for index in range(20):
        (folder / f"entry_{index}.md").write_text("Tracked.\n", encoding="utf-8")
    changed = folder.joinpath(*(f"layer_{index}" for index in range(10)), "changed.md")
    changed.parent.mkdir(parents=True)
    changed.write_text("Deep.\n", encoding="utf-8")
    unrelated = tmp_path / "ui"
    unrelated.mkdir()
    for index in range(21):
        (unrelated / f"entry_{index}.ts").write_text("export {};\n", encoding="utf-8")
    _track(folder, unrelated)

    assert lcs.main(["--only", str(changed)]) == 1
    output = capsys.readouterr().out
    assert "docs: directory has 21 direct entries" in output
    assert "ui: directory" not in output
    assert lcs.main([]) == 1
    assert "ui: directory has 21 direct entries" in capsys.readouterr().out
