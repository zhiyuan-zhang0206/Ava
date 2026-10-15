from __future__ import annotations

import pytest

from meizel import roles
from meizel.graph.model import MeizelError
from meizel.tests.synthetic import RepoFactory, diagnostics

ROOT = {"a/__init__.py": "", "b/__init__.py": "", "t/test_x.py": "", ".hidden/x.py": ""}
GOOD = '[roles]\napplication = ["a", "b"]\ntests = ["t"]\n'


def problems(make_repo: RepoFactory, manifest: str) -> list[str]:
    root = make_repo({**ROOT, "meizel.toml": manifest})
    return [d.message for d in diagnostics(root, "manifest-roles")]


def test_a_complete_declaration_is_clean(make_repo: RepoFactory) -> None:
    assert problems(make_repo, GOOD) == []


def test_a_top_level_directory_with_python_and_no_role_is_reported(make_repo: RepoFactory) -> None:
    found = problems(make_repo, '[roles]\napplication = ["a"]\ntests = ["t"]\n')
    assert found == [
        "top-level directory 'b' holds Python but has no role; declare it under [roles]"
    ]


def test_a_hidden_directory_is_not_a_package(make_repo: RepoFactory) -> None:
    assert not any(".hidden" in message for message in problems(make_repo, GOOD))


@pytest.mark.parametrize(
    ("manifest", "fragment"),
    [
        ('[roles]\nmystery = ["a"]\n', "unknown role 'mystery'"),
        ('[roles]\napplication = ["a", "b"]\ntests = ["t", "a"]\n', "'a' is in role 'application'"),
        ('[roles]\napplication = ["a", "b", "ghost"]\ntests = ["t"]\n', "'ghost' is not a tracked"),
        ('[roles]\napplication = "a"\n', "must be a list of strings"),
        ("roles = 3\n", "[roles] must be a table"),
    ],
)
def test_a_malformed_declaration_is_reported(
    make_repo: RepoFactory, manifest: str, fragment: str
) -> None:
    assert any(fragment in message for message in problems(make_repo, manifest))


def test_roles_belong_in_the_root_manifest_only(make_repo: RepoFactory) -> None:
    root = make_repo({**ROOT, "a/meizel.toml": GOOD})
    messages = [d.message for d in diagnostics(root, "manifest-roles")]
    assert messages == ["[roles] belongs in the root manifest only"]


def test_the_api_reads_roles_in_declared_order(make_repo: RepoFactory) -> None:
    root = make_repo({**ROOT, "meizel.toml": GOOD})
    assert dict(roles.roles(root)) == {"application": ("a", "b"), "tests": ("t",)}
    assert roles.packages("tests", "application", root=root) == ("t", "a", "b")
    assert roles.packages("tooling", root=root) == ()
    assert roles.all_packages(root) == ("a", "b", "t")


def test_the_api_refuses_an_unknown_role(make_repo: RepoFactory) -> None:
    root = make_repo({**ROOT, "meizel.toml": GOOD})
    with pytest.raises(MeizelError, match="unknown role"):
        roles.packages("mystery", root=root)


@pytest.mark.parametrize("manifest", ["[uses]\n", "[roles]\nmystery = []\n", "not toml ["])
def test_the_api_fails_fast_on_an_invalid_declaration(
    make_repo: RepoFactory, manifest: str
) -> None:
    root = make_repo({**ROOT, "meizel.toml": manifest})
    with pytest.raises(MeizelError):
        roles.roles(root)
