"""Generated package lists and the backstop that keeps other files from re-listing a role."""

from __future__ import annotations

from pathlib import Path

import pytest

from meizel.contracts import enumerations, generated
from meizel.contracts.cli import main
from meizel.graph.model import MeizelError
from meizel.tests.synthetic import RepoFactory

ROOT_MANIFEST = '[roles]\napplication = ["a", "b", "c"]\ntooling = ["tool"]\ntests = ["t"]\n'
BASE = {
    "meizel.toml": ROOT_MANIFEST,
    "a/__init__.py": "",
    "b/__init__.py": "",
    "c/__init__.py": "",
    "tool/x.py": "",
    "t/test_x.py": "",
}


def repo(make_repo: RepoFactory, extra: dict[str, str]) -> Path:
    return make_repo({**BASE, **extra})


def toml_with(body: str) -> str:
    return f"[tool.demo]\nitems = [\n    # meizel:roles BEGIN {body}\n    # meizel:roles END\n]\n"


def test_a_region_is_generated_and_then_fresh(make_repo: RepoFactory) -> None:
    root = repo(make_repo, {"conf.toml": toml_with("names application tooling")})
    assert generated.stale(root) == [
        "conf.toml:3: generated names region is stale; run `python -m meizel roles write`"
    ]
    assert generated.write(root) == ["conf.toml"]
    text = (root / "conf.toml").read_text(encoding="utf-8")
    assert '    "a",\n    "b",\n    "c",\n    "tool",\n    # meizel:roles END' in text
    assert generated.stale(root) == []
    assert generated.write(root) == []


def test_the_formats_render_names_test_hosts_and_a_regex_alternation(
    make_repo: RepoFactory,
) -> None:
    root = repo(
        make_repo,
        {
            "hosts.toml": toml_with("testpaths tests application"),
            "re.yaml": "files: |-\n  (?x)^(\n    # meizel:roles BEGIN regex-names application tooling\n"
            "    # meizel:roles END\n  )$\n",
        },
    )
    generated.write(root)
    hosts = (root / "hosts.toml").read_text(encoding="utf-8")
    assert '    "t",\n    "a/**/tests",\n    "b/**/tests",\n    "c/**/tests",\n' in hosts
    assert "    a|b|c|tool\n" in (root / "re.yaml").read_text(encoding="utf-8")


def test_the_dirs_format_renders_shell_words(make_repo: RepoFactory) -> None:
    root = repo(
        make_repo,
        {"scan.sh": "ROOTS=(\n  # meizel:roles BEGIN dirs application\n  # meizel:roles END\n)\n"},
    )
    generated.write(root)
    assert (root / "scan.sh").read_text(encoding="utf-8").splitlines()[2:5] == [
        "  a/",
        "  b/",
        "  c/",
    ]


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("# meizel:roles BEGIN names application\n", "BEGIN without END"),
        ("# meizel:roles END\n", "END without BEGIN"),
        (
            "# meizel:roles BEGIN names application\n# meizel:roles BEGIN names tests\n",
            "begins inside",
        ),
    ],
)
def test_unbalanced_markers_fail_fast(make_repo: RepoFactory, text: str, fragment: str) -> None:
    root = repo(make_repo, {"conf.toml": text})
    with pytest.raises(MeizelError, match=fragment):
        generated.regions(root)


def test_an_unknown_format_or_role_fails_fast(make_repo: RepoFactory) -> None:
    with pytest.raises(MeizelError, match="unknown generated format"):
        generated.stale(repo(make_repo, {"conf.toml": toml_with("mystery application")}))


def test_an_unknown_role_in_a_region_fails_fast(tmp_path: Path, make_repo: RepoFactory) -> None:
    root = repo(make_repo, {"conf.toml": toml_with("names mystery")})
    with pytest.raises(MeizelError, match="unknown role"):
        generated.stale(root)


@pytest.mark.parametrize(
    "literal",
    ['("a", "b", "c")', '["c/x", "b", "a", "z"]', '{"a", "b", "c"}'],
)
def test_a_literal_that_lists_a_whole_role_is_reported(
    make_repo: RepoFactory, literal: str
) -> None:
    root = repo(make_repo, {"tool/scan.py": f"DIRS = {literal}\n"})
    found = enumerations.violations(root)
    assert len(found) == 1
    assert found[0].startswith(
        "tool/scan.py:1: lists every top-level package of role 'application'"
    )


@pytest.mark.parametrize("literal", ['("a", "b")', '("a", "b", "tool")', '("a", "x", "y", "z")'])
def test_a_partial_list_is_not_a_copy(make_repo: RepoFactory, literal: str) -> None:
    assert enumerations.violations(repo(make_repo, {"tool/scan.py": f"DIRS = {literal}\n"})) == []


def test_tests_docs_and_meizel_are_outside_the_rule(make_repo: RepoFactory) -> None:
    listing = 'DIRS = ("a", "b", "c")\n'
    root = repo(
        make_repo,
        {
            "a/tests/test_x.py": listing,
            "docs/x.py": listing,
            "meizel/x.py": listing,
            "conftest.py": listing,
        },
    )
    assert enumerations.violations(root) == []


def test_a_toml_array_is_reported_unless_generated_or_pending_removal(
    make_repo: RepoFactory,
) -> None:
    root = repo(
        make_repo,
        {
            "copy.toml": 'items = ["a", "b", "c"]\n',
            "gen.toml": toml_with("names application"),
            "lint.toml": '[tool.importlinter]\nroot_packages = ["a", "b", "c"]\n',
        },
    )
    generated.write(root)
    found = enumerations.violations(root)
    assert [line.split(":")[0] for line in found] == ["copy.toml"]


def test_the_roles_command_lists_checks_and_writes(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo, {"conf.toml": toml_with("names application")})
    assert main(["--repo", str(root), "roles", "list", "application", "tooling"]) == 0
    assert capsys.readouterr().out.split() == ["a", "b", "c", "tool"]
    assert main(["--repo", str(root), "roles", "check"]) == 1
    assert "stale" in capsys.readouterr().out
    assert main(["--repo", str(root), "roles", "write"]) == 0
    assert main(["--repo", str(root), "roles", "check"]) == 0
    assert main(["--repo", str(root), "roles", "list", "mystery"]) == 2
