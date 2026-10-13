"""`debt` (the read-only summary) and `debt diff` (growth against a git ref)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, commit, run, write

TREE = {
    "base/__init__.py": "",
    "base/meizel.toml": (
        '[debt.private-name]\n"a.py -> b.py#_x" = 3\n"a.py -> b.py#_y" = 1\n'
        '\n[debt.restricted-python]\n"c.py#exec" = 2\n'
    ),
    "base/sub/__init__.py": "",
    "base/sub/meizel.toml": '[debt.private-module]\n"a.py -> b.py" = 4\n',
    "ava/__init__.py": "",
    "ava/meizel.toml": '[debt.cycle-edge]\n"x.py -> y.py" = 3\n',
    "clean/__init__.py": "",
    "meizel.toml": '[debt.undeclared-edge]\n"ava -> base" = 5\n',
    "broken/__init__.py": "",
    "broken/bad.py": "def f(:\n",
}


def test_summary_counts_by_code_top_level_directory_and_mode(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(TREE)
    assert run(root, "debt", "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total"] == 18
    assert report["by_code"] == {
        "cycle-edge": 3,
        "private-module": 4,
        "private-name": 4,
        "restricted-python": 2,
        "undeclared-edge": 5,
    }
    assert report["by_top_level"]["base"] == {
        "total": 10,
        "by_code": {"private-module": 4, "private-name": 4, "restricted-python": 2},
        "strict_to_be_directories": 2,
    }
    assert report["by_top_level"]["."]["total"] == 5
    assert report["directories"] == {"strict_to_be": 4, "strict": 2}


def test_summary_text_names_the_modes_and_the_counts(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(make_repo(TREE), "debt") == 0
    out = capsys.readouterr().out
    assert "18 recorded locality debts" in out
    assert "4 strict-to-be directories" in out
    assert "2 strict directories" in out
    assert "private-name" in out
    assert "base" in out


def test_summary_reads_manifests_only_and_runs_no_analysis(make_repo: RepoFactory) -> None:
    root = make_repo(TREE)
    # `broken/bad.py` cannot be parsed: any analysis stops the tool with exit code 2.
    assert run(root, "check") == 2
    assert run(root, "debt") == 0


def test_a_repository_without_debt_is_all_strict(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({"pkg/__init__.py": "", "pkg/a.py": ""})
    assert run(root, "debt", "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total"] == 0
    assert report["directories"] == {"strict_to_be": 0, "strict": 2}


def test_a_malformed_baseline_stops_the_summary(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({"pkg/__init__.py": "", "pkg/meizel.toml": '[debt.mystery]\n"x" = 1\n'})
    assert run(root, "debt") == 2
    assert "pkg/meizel.toml" in capsys.readouterr().err


def base_repo(make_repo: RepoFactory) -> Path:
    root = make_repo(
        {
            "a/__init__.py": "",
            "a/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 3\n',
            "b/__init__.py": "",
        }
    )
    commit(root)
    return root


def diff(root: Path, *flags: str) -> int:
    return run(root, "debt", "diff", "--against", "HEAD", *flags)


def test_growth_fails_or_warns_as_the_caller_chooses(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    write(root, {"a/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 4\n'})
    assert diff(root, "--on-growth", "fail") == 1
    out = capsys.readouterr()
    assert "private-name" in out.out
    assert "+1" in out.out
    assert "grew" in out.err
    assert diff(root, "--on-growth", "warn") == 0
    assert "warning" in capsys.readouterr().err


def test_the_tool_has_no_default_policy(make_repo: RepoFactory) -> None:
    root = base_repo(make_repo)
    with pytest.raises(SystemExit):
        run(root, "debt", "diff", "--against", "HEAD")


def test_moving_debt_between_directories_is_not_growth(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    write(
        root,
        {
            "a/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 1\n',
            "b/meizel.toml": '[debt.private-name]\n"z.py -> y.py#_n" = 2\n',
        },
    )
    assert diff(root, "--on-growth", "fail") == 0
    assert "no growth" in capsys.readouterr().out


def test_shrinking_and_clearing_debt_is_fine(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    write(root, {"a/meizel.toml": ""})
    assert diff(root, "--on-growth", "fail") == 0
    assert "-3" in capsys.readouterr().out


def test_a_new_code_counts_as_growth(make_repo: RepoFactory) -> None:
    root = base_repo(make_repo)
    write(
        root,
        {
            "a/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 3\n'
            '\n[debt.restricted-python]\n"x.py#exec" = 1\n'
        },
    )
    assert diff(root, "--on-growth", "fail") == 1


def test_a_ref_without_any_manifest_has_zero_debt(make_repo: RepoFactory) -> None:
    root = make_repo({"a/__init__.py": ""})
    commit(root)
    write(root, {"a/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 1\n'})
    assert diff(root, "--on-growth", "fail") == 1


def test_an_unknown_ref_stops_the_tool(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    assert run(root, "debt", "diff", "--against", "no-such-ref", "--on-growth", "fail") == 2
    assert "no-such-ref" in capsys.readouterr().err


WAIVE = (
    "--on-growth",
    "fail",
    "--waive-if-changed",
    ":(glob)rules/**/*.py",
    ":(exclude,glob)rules/**/tests/**",
    "--waive-only-within",
    ":(glob)rules/**",
    ":(glob)**/meizel.toml",
    "policy.toml",
)


def test_growth_is_only_a_warning_when_the_change_touches_the_rules(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    write(
        root,
        {
            "a/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 4\n',
            "rules/engine.py": "RULE = 1\n",
        },
    )
    assert diff(root, *WAIVE) == 0
    out = capsys.readouterr()
    assert "this change edits only the rules (rules/engine.py)" in out.out
    assert "warning" in out.err


def test_a_test_under_the_rules_does_not_waive_growth(make_repo: RepoFactory) -> None:
    root = base_repo(make_repo)
    write(
        root,
        {
            "a/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 4\n',
            "rules/tests/test_engine.py": "def test_a(): ...\n",
        },
    )
    assert diff(root, *WAIVE) == 1


def test_a_change_that_also_edits_product_code_is_not_waived(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = base_repo(make_repo)
    write(
        root,
        {
            "a/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 4\n',
            "rules/engine.py": "RULE = 1\n",
            "a/product.py": "VALUE = 1\n",
        },
    )
    assert diff(root, *WAIVE) == 1
    out = capsys.readouterr()
    assert "not waived" in out.out
    assert "a/product.py" in out.out
    assert "warning" not in out.err


def test_a_rule_change_may_carry_the_baselines_and_the_policy_it_moves(
    make_repo: RepoFactory,
) -> None:
    root = base_repo(make_repo)
    write(
        root,
        {
            "a/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 4\n',
            "rules/engine.py": "RULE = 1\n",
            "rules/tests/test_engine.py": "def test_a(): ...\n",
            "policy.toml": "[external]\n",
        },
    )
    assert diff(root, *WAIVE) == 0


def test_manifests_alone_are_no_rule_change(make_repo: RepoFactory) -> None:
    root = base_repo(make_repo)
    write(root, {"a/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 4\n'})
    assert diff(root, *WAIVE) == 1


def test_only_within_alone_waives_a_change_confined_to_those_paths(
    make_repo: RepoFactory,
) -> None:
    root = base_repo(make_repo)
    write(root, {"a/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 4\n'})
    args = ("--on-growth", "fail", "--waive-only-within", ":(glob)**/meizel.toml")
    assert diff(root, *args) == 0
    write(root, {"a/product.py": "VALUE = 1\n"})
    assert diff(root, *args) == 1


def test_a_ref_that_records_a_retired_code_still_counts(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(
        {
            "a/__init__.py": "",
            "a/meizel.toml": '[debt.manifest-cycle]\n"x.py -> y.py" = 2\n\n[debt.private-name]\n"x.py -> y.py#_n" = 3\n',
        }
    )
    commit(root)
    write(root, {"a/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 3\n'})
    assert diff(root, "--on-growth", "fail") == 0
    out = capsys.readouterr().out
    assert "manifest-cycle" in out
    assert "-2" in out
