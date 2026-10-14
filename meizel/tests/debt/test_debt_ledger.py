"""Recorded debt: strict and strict-to-be directories, and what a baseline may and may not cover."""

from __future__ import annotations

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of, failures

PRIVATE_USE = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg.b import _secret\n",
    "pkg/b.py": "_secret = 1\n",
}
DECLARED = '[uses]\n"a.py" = ["b.py"]\n'
BASELINE = '\n[debt.private-name]\n"a.py -> b.py#_secret" = 1\n'


def verdict_of(make_repo: RepoFactory, manifest: str, extra: dict[str, str] | None = None):
    root = make_repo({**PRIVATE_USE, **(extra or {}), "pkg/meizel.toml": manifest})
    return root, analysis_of(root).verdict


def test_a_directory_without_debt_is_strict(make_repo: RepoFactory) -> None:
    _, found = verdict_of(make_repo, DECLARED)
    [private] = found
    assert private.code.value == "private-name"
    assert "strict-to-be" not in private.render()


def test_a_recorded_debt_is_allowed(make_repo: RepoFactory) -> None:
    root, found = verdict_of(make_repo, DECLARED + BASELINE)
    assert found == []
    assert analysis_of(root).diagnostics, "the violation is still found; only the verdict allows it"


def test_a_new_violation_in_a_strict_to_be_directory_fails_and_says_why(
    make_repo: RepoFactory,
) -> None:
    extra = {"pkg/c.py": "from pkg.b import _other\n"}
    manifest = '[uses]\n"a.py" = ["b.py"]\n"c.py" = ["b.py"]\n' + BASELINE
    _, found = verdict_of(make_repo, manifest, {**extra, "pkg/b.py": "_secret = 1\n_other = 2\n"})
    [new] = found
    assert (new.path, new.code.value) == ("pkg/c.py", "private-name")
    text = new.render()
    assert "pkg is strict-to-be: it carries 1 recorded locality debts that must only shrink" in text
    assert "Fix this violation rather than recording it" in text
    assert "debt record" in text


def test_more_occurrences_than_recorded_fail_with_both_counts(make_repo: RepoFactory) -> None:
    source = "from pkg.b import _secret\nfrom pkg.b import _secret as again\n"
    _, found = verdict_of(make_repo, DECLARED + BASELINE, {"pkg/a.py": source})
    assert {d.line for d in found} == {1, 2}
    assert all("1 recorded, 2 found" in d.render() for d in found)


def test_a_recorded_debt_that_no_longer_occurs_fails_and_asks_for_a_prune(
    make_repo: RepoFactory,
) -> None:
    manifest = DECLARED + BASELINE + '"a.py -> b.py#_gone" = 2\n'
    _, found = verdict_of(make_repo, manifest)
    [paid] = found
    assert (paid.path, paid.code.value) == ("pkg/meizel.toml", "debt-paid")
    assert paid.line == 6
    assert "_gone" in paid.message
    assert "debt prune" in paid.render()


def test_a_count_above_what_remains_is_paid_in_part(make_repo: RepoFactory) -> None:
    manifest = DECLARED + '\n[debt.private-name]\n"a.py -> b.py#_secret" = 3\n'
    _, found = verdict_of(make_repo, manifest)
    [paid] = found
    assert paid.code.value == "debt-paid"
    assert "3 recorded but 1 remain" in paid.message


def test_debt_of_a_deleted_file_is_paid(make_repo: RepoFactory) -> None:
    manifest = DECLARED + BASELINE + '"ghost.py -> b.py#_secret" = 1\n'
    _, found = verdict_of(make_repo, manifest)
    assert [d.code.value for d in found] == ["debt-paid"]


def test_a_cross_package_violation_is_owned_by_the_common_directory(
    make_repo: RepoFactory,
) -> None:
    files = {
        "top/__init__.py": "",
        "top/x/__init__.py": "",
        "top/x/m.py": "from top.y.n import _hidden\n",
        "top/y/__init__.py": "",
        "top/y/n.py": "_hidden = 1\n",
        "top/y/meizel.toml": 'exports = ["n.py"]\n',
        "top/meizel.toml": '[uses]\n"x" = ["y"]\n\n[debt.private-name]\n"x/m.py -> y/n.py#_hidden" = 1\n',
    }
    root = make_repo(files)
    assert analysis_of(root).verdict == []


def test_debt_recorded_in_the_wrong_directory_covers_nothing(make_repo: RepoFactory) -> None:
    files = {
        "top/__init__.py": "",
        "top/x/__init__.py": "",
        "top/x/m.py": "from top.y.n import _hidden\n",
        "top/y/__init__.py": "",
        "top/y/n.py": "_hidden = 1\n",
        "top/y/meizel.toml": 'exports = ["n.py"]\n',
        "top/x/meizel.toml": '[debt.private-name]\n"m.py -> ../y/n.py#_hidden" = 1\n',
        "top/meizel.toml": '[uses]\n"x" = ["y"]\n',
    }
    found = {(d.code.value, d.path) for d in analysis_of(make_repo(files)).verdict}
    assert ("private-name", "top/x/m.py") in found
    assert ("debt-paid", "top/x/meizel.toml") in found


def test_the_key_carries_no_line_number(make_repo: RepoFactory) -> None:
    shifted = "# a comment\n# another\n\nfrom pkg.b import _secret\n"
    _, found = verdict_of(make_repo, DECLARED + BASELINE, {"pkg/a.py": shifted})
    assert found == []


@pytest.mark.parametrize(
    ("code", "key"),
    [
        ("undeclared-edge", "a.py -> b.py"),
        ("not-exported", "a.py -> b.py"),
        ("private-module", "a.py -> b.py"),
        ("restricted-python", "a.py#exec"),
    ],
)
def test_every_debtable_code_is_accepted(make_repo: RepoFactory, code: str, key: str) -> None:
    manifest = f'[debt.{code}]\n"{key}" = 1\n'
    root = make_repo({**PRIVATE_USE, "pkg/meizel.toml": manifest})
    assert not [d for d in analysis_of(root).diagnostics if d.code.value == "manifest-debt"]


@pytest.mark.parametrize(
    "debt",
    [
        "debt = 3\n",
        "[debt]\nprivate-name = 3\n",
        '[debt.mystery]\n"a.py -> b.py" = 1\n',
        '[debt.stale-load]\n"a.py -> b.py" = 1\n',
        '[debt.private-name]\n"a.py -> b.py#_x" = 0\n',
        '[debt.private-name]\n"a.py -> b.py#_x" = -2\n',
        '[debt.private-name]\n"a.py -> b.py#_x" = "many"\n',
        '[debt.private-name]\n"a.py -> b.py#_x" = true\n',
        '[debt.private-name]\n"a.py -> b.py" = 1\n',
        '[debt.undeclared-edge]\n"a.py" = 1\n',
        '[debt.restricted-python]\n"a.py" = 1\n',
        '[debt.manifest-cycle]\n"b.py -> a.py" = 1\n',
        '[debt.cycle-edge]\n"b.py -> a.py" = 0\n',
        '[debt.cycle-edge]\n"b.py" = 1\n',
        '[debt.cycle-edge]\n"b.py -> a.py#_x" = 1\n',
    ],
)
def test_a_malformed_debt_table_fails_with_a_located_diagnostic(
    make_repo: RepoFactory, debt: str
) -> None:
    manifest = debt + DECLARED if debt.startswith("debt =") else DECLARED + debt
    root = make_repo({**PRIVATE_USE, "pkg/meizel.toml": manifest})
    found = [d for d in analysis_of(root).verdict if d.code.value == "manifest-debt"]
    assert found, debt
    assert {d.path for d in found} == {"pkg/meizel.toml"}
    assert all(d.line >= 1 for d in found)


CYCLE = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg import b\n",
    "pkg/b.py": "from pkg import a\n",
}
CYCLIC = '[uses]\n"a.py" = ["b.py"]\n"b.py" = ["a.py"]\n'


def test_recorded_facts_on_a_back_edge_let_a_cyclic_graph_through(make_repo: RepoFactory) -> None:
    manifest = CYCLIC + '\n[debt.cycle-edge]\n"b.py -> a.py" = 1\n'
    root = make_repo({**CYCLE, "pkg/meizel.toml": manifest})
    assert failures(root) == set()
    assert any(d.code.value == "cycle-edge" for d in analysis_of(root).diagnostics)


def test_a_new_import_on_a_recorded_back_edge_fails_with_both_counts(
    make_repo: RepoFactory,
) -> None:
    manifest = CYCLIC + '\n[debt.cycle-edge]\n"b.py -> a.py" = 1\n'
    source = "from pkg import a\nfrom pkg import a as again\n"
    root = make_repo({**CYCLE, "pkg/b.py": source, "pkg/meizel.toml": manifest})
    found = [d for d in analysis_of(root).verdict if d.code.value == "cycle-edge"]
    assert {d.line for d in found} == {1, 2}
    assert all("1 recorded, 2 found" in d.render() for d in found)
    assert all("pkg is strict-to-be" in d.render() for d in found)


def test_a_new_file_on_a_recorded_back_edge_is_a_new_violation(make_repo: RepoFactory) -> None:
    files = {
        "top/__init__.py": "",
        "top/x/__init__.py": "",
        "top/x/m.py": "from top.y import n\n",
        "top/x/extra.py": "from top.y import n\n",
        "top/y/__init__.py": "",
        "top/y/n.py": "from top.x import m\n",
        "top/x/meizel.toml": 'exports = ["m.py"]\n',
        "top/y/meizel.toml": 'exports = ["n.py"]\n',
        "top/meizel.toml": (
            '[uses]\n"x" = ["y"]\n"y" = ["x"]\n\n[debt.cycle-edge]\n"x/m.py -> y/n.py" = 1\n'
        ),
    }
    found = [d for d in analysis_of(make_repo(files)).verdict if d.code.value == "cycle-edge"]
    assert [d.path for d in found] == ["top/x/extra.py"]
    assert "does not close" not in found[0].message
    assert "closes a cycle" in found[0].message


def test_a_cycle_the_back_edges_do_not_cover_still_fails_and_says_so(
    make_repo: RepoFactory,
) -> None:
    files = {
        **CYCLE,
        "pkg/c.py": "from pkg import a\n",
        "pkg/a.py": "from pkg import b\nfrom pkg import c\n",
        "pkg/b.py": "from pkg import a\n",
    }
    manifest = (
        '[uses]\n"a.py" = ["b.py", "c.py"]\n"b.py" = ["a.py"]\n"c.py" = ["a.py"]\n'
        '\n[debt.cycle-edge]\n"b.py -> a.py" = 1\n'
    )
    root = make_repo({**files, "pkg/meizel.toml": manifest})
    [found] = [d for d in analysis_of(root).verdict if d.code.value == "manifest-cycle"]
    assert "pkg is strict-to-be" in found.render()


def test_facts_on_an_edge_that_no_longer_closes_a_cycle_are_paid(make_repo: RepoFactory) -> None:
    manifest = (
        '[uses]\n"a.py" = ["b.py"]\n"b.py" = ["a.py"]\n'
        '\n[debt.cycle-edge]\n"b.py -> a.py" = 1\n"a.py -> b.py" = 1\n'
    )
    root = make_repo({**CYCLE, "pkg/meizel.toml": manifest})
    [paid] = [d for d in analysis_of(root).verdict if d.code.value == "debt-paid"]
    assert "a.py -> b.py" in paid.message
    assert "closes no cycle" in paid.message


def test_facts_on_an_edge_that_is_no_longer_declared_are_paid(make_repo: RepoFactory) -> None:
    manifest = '[uses]\n"a.py" = ["b.py"]\n\n[debt.cycle-edge]\n"b.py -> a.py" = 1\n'
    root = make_repo({**CYCLE, "pkg/meizel.toml": manifest})
    found = analysis_of(root).verdict
    codes = {d.code.value for d in found}
    assert "debt-paid" in codes
    assert "undeclared-edge" in codes
    assert "no longer declared" in " ".join(d.message for d in found)


def test_a_fact_on_a_back_edge_is_still_judged_by_the_other_rules(make_repo: RepoFactory) -> None:
    files = {
        **CYCLE,
        "pkg/b.py": "from pkg.a import _hidden\n",
        "pkg/a.py": "from pkg import b\n_hidden = 1\n",
    }
    manifest = CYCLIC + '\n[debt.cycle-edge]\n"b.py -> a.py" = 1\n'
    root = make_repo({**files, "pkg/meizel.toml": manifest})
    assert {(d.code.value, d.path) for d in analysis_of(root).verdict} == {
        ("private-name", "pkg/b.py")
    }


def test_the_debt_of_one_directory_makes_only_that_directory_strict_to_be(
    make_repo: RepoFactory,
) -> None:
    files = {
        **PRIVATE_USE,
        "other/__init__.py": "",
        "other/a.py": "from other.b import _secret\n",
        "other/b.py": "_secret = 1\n",
        "other/meizel.toml": DECLARED,
        "pkg/meizel.toml": DECLARED + BASELINE,
    }
    root = make_repo(files)
    [found] = analysis_of(root).verdict
    assert found.path == "other/a.py"
    assert "strict-to-be" not in found.render()
