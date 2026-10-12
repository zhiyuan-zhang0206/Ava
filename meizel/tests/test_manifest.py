"""Manifest validation: every way a `meizel.toml` is malformed or out of date."""

from __future__ import annotations

import pytest

from meizel.tests.synthetic import RepoFactory, diagnostics, verdict

BASE = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg import b\n",
    "pkg/b.py": "",
}
GOOD = '[uses]\n"a.py" = ["b.py"]\n'


def code_for(make_repo: RepoFactory, manifest: str, code: str, extra: dict[str, str] | None = None):
    root = make_repo({**BASE, **(extra or {}), "pkg/meizel.toml": manifest})
    return diagnostics(root, code)


@pytest.mark.parametrize(
    ("manifest", "code"),
    [
        ("mystery = 1\n" + GOOD, "manifest-unknown-key"),
        ('[uses]\n"a.py" = ["b.py", "ghost.py"]\n', "manifest-unknown-child"),
        ('[uses]\n"ghost.py" = ["b.py"]\n"a.py" = ["b.py"]\n', "manifest-unknown-child"),
        ('exports = ["ghost.py"]\n' + GOOD, "manifest-unknown-child"),
        ('[uses]\n"a.py" = ["b.py", "b.py"]\n', "manifest-duplicate"),
        ('exports = ["b.py", "b.py"]\n' + GOOD, "manifest-duplicate"),
        ('[uses]\n"a.py" = ["a.py", "b.py"]\n', "manifest-self-edge"),
        ('[uses]\n"a.py" = ["b.py"]\n"b.py" = ["a.py"]\n', "manifest-cycle"),
        ("exports = 3\n" + GOOD, "manifest-syntax"),
        ("uses = 3\n", "manifest-syntax"),
        ('[uses]\n"a.py" = "b.py"\n', "manifest-syntax"),
        ("this is not toml", "manifest-syntax"),
        ('[uses]\n"a.py" = ["meizel.toml"]\n', "manifest-unknown-child"),
    ],
)
def test_malformed_manifests_fail_with_a_located_diagnostic(
    make_repo: RepoFactory, manifest: str, code: str
) -> None:
    found = code_for(make_repo, manifest, code)
    assert found, f"{code} not reported for {manifest!r}"
    assert {d.path for d in found} == {"pkg/meizel.toml"}
    assert all(d.line >= 1 for d in found)


def test_an_underscore_child_cannot_be_exported(make_repo: RepoFactory) -> None:
    found = code_for(
        make_repo,
        'exports = ["_hidden.py"]\n' + GOOD,
        "manifest-export-private",
        {"pkg/_hidden.py": ""},
    )
    assert [d.line for d in found] == [1]


def test_a_declared_edge_no_fact_needs_is_stale(make_repo: RepoFactory) -> None:
    manifest = '[uses]\n"a.py" = ["b.py"]\n"b.py" = []\n"__init__.py" = ["a.py"]\n'
    [found] = code_for(make_repo, manifest, "manifest-stale-edge")
    assert "__init__.py -> a.py" in found.message
    assert found.line == 4


def test_a_well_formed_manifest_is_silent(make_repo: RepoFactory) -> None:
    root = make_repo({**BASE, "pkg/meizel.toml": GOOD})
    assert verdict(root) == set()


def test_unknown_keys_do_not_hide_other_errors(make_repo: RepoFactory) -> None:
    root = make_repo({**BASE, "pkg/meizel.toml": 'mystery = 1\n[uses]\n"a.py" = ["a.py"]\n'})
    assert {c for c, _ in verdict(root)} >= {"manifest-unknown-key", "manifest-self-edge"}
