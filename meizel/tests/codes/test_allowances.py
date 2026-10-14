"""`meizel.allowances`: the one reader of the policy file, and rule code that holds no exemption."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from meizel import allowances
from meizel.tests.synthetic import RepoFactory

ROOT = Path(__file__).resolve().parents[3]
POLICY = (
    '[ambient-state]\npure-callees = ["re.compile", "typing.TypeVar"]\n\n'
    '[ambient-state.sites]\n"a.py::hidden-cache:f" = "a pure derivation"\n\n'
    '[single-owner.postgres-dial]\n"b.py" = "cannot go through the owner"\n'
)


def test_a_rule_reads_its_sections_from_the_policy(make_repo: RepoFactory) -> None:
    policy = allowances.load(make_repo({"meizel.policy.toml": POLICY}))
    assert policy.names("ambient-state", "pure-callees") == ("re.compile", "typing.TypeVar")
    assert policy.reasons("ambient-state", "sites") == {"a.py::hidden-cache:f": "a pure derivation"}
    assert policy.reasons("single-owner", "postgres-dial") == {
        "b.py": "cannot go through the owner"
    }


def test_a_section_the_file_does_not_have_lists_nothing(make_repo: RepoFactory) -> None:
    policy = allowances.load(make_repo({"meizel.policy.toml": POLICY}))
    assert policy.names("ambient-state", "wiring-callees") == ()
    assert policy.reasons("type-checking", "files") == {}


def test_an_absent_policy_lists_nothing(make_repo: RepoFactory) -> None:
    root = make_repo({"a.py": ""})
    (root / allowances.POLICY).unlink(missing_ok=True)
    assert allowances.load(root).reasons("type-checking", "files") == {}


@pytest.mark.parametrize(
    ("rule", "section", "shape"),
    [
        ("ambient-state", "no-such-section", "reasons"),
        ("no-such-rule", "files", "reasons"),
        ("ambient-state", "pure-callees", "reasons"),
        ("type-checking", "files", "names"),
        ("single-owner", "postgres-dial", "names"),
    ],
)
def test_a_rule_cannot_ask_for_a_section_that_does_not_exist(
    make_repo: RepoFactory, rule: str, section: str, shape: str
) -> None:
    policy = allowances.load(make_repo({"meizel.policy.toml": POLICY}))
    with pytest.raises(KeyError):
        (policy.reasons if shape == "reasons" else policy.names)(rule, section)


def test_a_malformed_policy_is_an_error_for_the_rule_that_reads_it(make_repo: RepoFactory) -> None:
    root = make_repo({"meizel.policy.toml": '[type-checking.files]\n"a.py" = ""\n'})
    with pytest.raises(
        ValueError, match=r"\[type-checking.files\] 'a.py' needs a non-empty reason"
    ):
        allowances.load(root)


def test_the_repository_policy_is_well_formed() -> None:
    _, problems = allowances.parse((ROOT / allowances.POLICY).read_text(encoding="utf-8"))
    assert problems == []


def test_no_rule_code_holds_an_exemption_of_its_own() -> None:
    """Every file or site the policy exempts is a string in the policy and in no rule module."""
    policy = allowances.load(ROOT)
    exempt = {
        name
        for entries in policy.listed.values()
        for name in entries
        if "/" in name  # a path or a `path::site`; the callee vocabularies are not repo paths
    }
    assert len(exempt) > 50
    literals = {
        node.value: path.relative_to(ROOT).as_posix()
        for path in (ROOT / "meizel").rglob("*.py")
        if "tests" not in path.relative_to(ROOT).parts
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {name: literals[name] for name in exempt & literals.keys()} == {}
