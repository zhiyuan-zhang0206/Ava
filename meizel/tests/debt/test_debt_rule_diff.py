"""`debt diff` for a change that edits the rules: the growth the rules cause is reported, the content's is judged."""

from __future__ import annotations

from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, commit, run, write

RULES = ":(glob)rules/**"
# `app` imports through an underscore module: a violation the base's old rules did not record, and one
# that no declaration can fix, so a rule change must record it.
BASE = {
    "lib/__init__.py": "",
    "lib/_hidden.py": "",
    "app/__init__.py": "",
    "app/x.py": "import lib._hidden\n",
    "rules/engine.py": "RULE = 1\n",
}


def base_repo(make_repo: RepoFactory, extra: dict[str, str] | None = None) -> Path:
    root = make_repo({**BASE, **(extra or {})})
    commit(root)
    return root


def diff(root: Path) -> int:
    return run(
        root,
        "debt",
        "diff",
        "--against",
        "HEAD",
        "--on-growth",
        "fail",
        "--waive-if-changed",
        RULES,
    )


def record(root: Path) -> None:
    """What a rule change's baseline commit does: declare the legal edges, record the rest, stage."""
    run(root, "sync", "--declare", ".")  # exits 1 while an error is left for `record`
    write(root, {})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})


def rule_change(root: Path) -> None:
    write(root, {"rules/engine.py": "RULE = 2\n"})


def test_a_pure_rule_change_may_record_what_the_new_rules_find(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    rule_change(root)
    record(root)
    capsys.readouterr()
    assert diff(root) == 0
    out = capsys.readouterr().out
    assert "private-module" in out
    assert "+1" in out
    assert "no content growth" in out


def test_a_rule_change_that_also_adds_a_violation_fails(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    rule_change(root)
    write(root, {"app/y.py": "import lib._hidden\n"})
    record(root)
    capsys.readouterr()
    assert diff(root) == 1
    out = capsys.readouterr()
    assert "app/y.py" in out.out
    assert "grew" in out.err


def test_a_content_change_that_moves_a_file_is_not_growth(make_repo: RepoFactory) -> None:
    root = base_repo(make_repo)
    record(root)
    commit(root)
    write(root, {"app/z.py": "import lib._hidden\n"})
    (root / "app" / "x.py").unlink()
    record(root)
    assert run(root, "debt", "prune") == 0
    write(root, {})
    assert diff(root) == 0


def test_a_content_change_behind_a_rule_change_passes_with_it(make_repo: RepoFactory) -> None:
    root = base_repo(make_repo)
    rule_change(root)
    write(root, {"app/x.py": "import lib._hidden\n\nVALUE = 1\n", "app/notes.md": "text\n"})
    record(root)
    assert diff(root) == 0


def test_a_content_change_with_no_rule_change_is_judged_by_total(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    write(root, {"app/y.py": "import lib._hidden\n"})
    record(root)
    capsys.readouterr()
    assert diff(root) == 1
    assert "grew" in capsys.readouterr().err


def test_manifests_the_checker_cannot_read_stop_the_comparison(make_repo: RepoFactory) -> None:
    root = base_repo(make_repo, {"app/meizel.toml": 'not_a_key = ["x"]\n'})
    rule_change(root)
    write(root, {"app/meizel.toml": ""})
    assert diff(root) == 2
