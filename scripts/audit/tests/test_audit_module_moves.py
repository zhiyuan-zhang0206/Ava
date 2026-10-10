"""Module-move CLI audits use tracked scratch repositories and real imports."""

import sys
from pathlib import Path
from uuid import uuid4

import pytest

from base.host.proc import run_bounded
from scripts.audit import module_moves as gate


@pytest.fixture
def move_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    package = f"audit_move_{uuid4().hex}"
    source = tmp_path / package
    source.mkdir()
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "module.py").write_text("def present() -> None: ...\n", encoding="utf-8")
    run_bounded(
        ["git", "init", "--quiet"], timeout=10, capture_output=True, cwd=tmp_path
    ).check_returncode()
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    return tmp_path, package


def _audit(checkout: tuple[Path, str], files: dict[str, str], *, target: str | None = None) -> int:
    root, package = checkout
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    run_bounded(["git", "add", "."], timeout=10, capture_output=True, cwd=root).check_returncode()
    return gate.main([f"pkg_old.mod_name={target or package + '.module'}"], repo_root=root)


def test_reports_dotted_parent_import_and_slash_lines(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _audit(
            move_checkout,
            {
                "references.txt": (
                    "import pkg_old.mod_name\n"
                    "from pkg_old import mod_name\n"
                    "See pkg_old/mod_name.py for details.\n"
                )
            },
        )
        == 1
    )
    output = capsys.readouterr().out
    assert [row for row in output.splitlines() if ": old reference" in row] == [
        f"references.txt:{line}: old reference to pkg_old.mod_name" for line in (1, 2, 3)
    ]
    assert "3 old references" in output
    assert output.endswith("AUDIT: FAIL\n")


def test_history_reads_ignore_quoted_and_bare_operands(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _audit(
            move_checkout,
            {
                "history.sh": (
                    'git show "$SHA:pkg_old/mod_name.py" > moved/module.py\n'
                    "git show bd6b15ed0:pkg_old/mod_name.py > moved/module.py\n"
                )
            },
        )
        == 0
    )
    assert "0 old references" in capsys.readouterr().out


def test_history_read_does_not_hide_an_old_destination(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _audit(
            move_checkout,
            {"history.sh": 'git show "abc:pkg_old/mod_name.py" > pkg_old/mod_name.py\n'},
        )
        == 1
    )
    assert "history.sh:1: old reference to pkg_old.mod_name" in capsys.readouterr().out


@pytest.mark.parametrize(
    "text",
    [
        "pkg_old/sub/mod_name.py",
        "unrelated text",
        "mod_name.py",
        "pkg_old/mod_name.pyc",
        "pkg_old/mod_name.pyi",
        "other_pkg_old/mod_name.py",
        "other.pkg_old/mod_name.py",
    ],
)
def test_ignores_unrelated_or_inexact_paths(move_checkout: tuple[Path, str], text: str) -> None:
    assert _audit(move_checkout, {"references.txt": text}) == 0


def test_frozen_history_is_excluded_but_live_docs_fail(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _audit(
            move_checkout,
            {
                "docs/decisions/x.md": "pkg_old.mod_name\n",
                "docs/postmortems/y.md": "pkg_old.mod_name\n",
                "db/schema.sql": "-- pkg_old.mod_name\n",
                "base/packages/docs/notes.py": "# pkg_old.mod_name\n",
            },
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "docs/decisions/x.md:" not in output
    assert "docs/postmortems/y.md:" not in output
    assert "db/schema.sql:1: old reference" in output
    assert "base/packages/docs/notes.py:1: old reference" in output


def test_accepts_submodule_fallback_for_a_lean_package(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, package = move_checkout
    assert (
        _audit(move_checkout, {"consumer.py": f"from {package} import module\n"}, target=package)
        == 0
    )
    assert "1 referenced names, missing=[]" in capsys.readouterr().out


def test_reports_unknown_names_at_the_new_module(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, package = move_checkout
    assert (
        _audit(move_checkout, {"consumer.py": f"from {package}.module import no_such_name_xyz\n"})
        == 1
    )
    assert "missing=['no_such_name_xyz']" in capsys.readouterr().out


def test_existing_names_in_multiline_imports_and_quoted_targets_pass(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, package = move_checkout
    assert (
        _audit(
            move_checkout,
            {
                "consumer.py": f"from {package}.module import (\n    present,\n)\n",
                "target.txt": f'"{package}.module.present"\n',
            },
        )
        == 0
    )
    assert "1 referenced names, missing=[]" in capsys.readouterr().out


def test_wildcard_import_fails_completeness(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, package = move_checkout
    assert _audit(move_checkout, {"consumer.py": f"from {package}.module import *\n"}) == 1
    assert "missing=['*']" in capsys.readouterr().out


def test_import_failure_does_not_hide_remaining_pairs(
    move_checkout: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, package = move_checkout
    assert _audit(move_checkout, {}) == 0
    capsys.readouterr()
    assert (
        gate.main(
            [f"pkg_old.mod_name={package}.missing", f"pkg_old.other={package}.module"],
            repo_root=root,
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "import failed: ModuleNotFoundError" in output
    assert f"pkg_old.other -> {package}.module: 0 old references" in output


@pytest.mark.parametrize("pair", ["missing_separator", "old.mod=new-mod", "old.mod="])
def test_invalid_move_pair_fails_at_the_cli_boundary(pair: str, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        gate.main([pair], repo_root=tmp_path)
    assert exit_info.value.code == 2
