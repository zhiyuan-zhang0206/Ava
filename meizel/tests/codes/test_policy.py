from __future__ import annotations

from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of, commit, diagnostics, git, run, write

LOADER = {
    "lib/__init__.py": "",
    "app/__init__.py": "",
    "app/loader.py": "import importlib\nimportlib.import_module('lib')\n",
    "app/runner.py": "exec('1')\n",
}
MANIFEST = '[loads]\n"loader.py" = ["lib"]\n\n[external]\n"runner.py" = "runs the user\'s code"\n'
ROOT = '[uses]\n"app" = ["lib"]\n'
POLICY = (
    '[external]\n"app/runner.py" = "runs the user\'s code"\n\n[loads]\n"app/loader.py" = ["lib"]\n'
)


def repo(make_repo: RepoFactory, policy: str | None, manifest: str = MANIFEST) -> Path:
    files = {**LOADER, "app/meizel.toml": manifest, "meizel.toml": ROOT}
    if policy is not None:
        files["meizel.policy.toml"] = policy
    return make_repo(files)


def unlisted(root: Path) -> list[str]:
    return sorted(d.message for d in diagnostics(root, "unlisted-allowance"))


def test_listed_sites_are_allowed(make_repo: RepoFactory) -> None:
    root = repo(make_repo, POLICY)
    assert unlisted(root) == []


def test_a_site_the_policy_does_not_list_is_an_error_whatever_the_manifest_says(
    make_repo: RepoFactory,
) -> None:
    root = repo(make_repo, '[external]\n"app/other.py" = "x"\n\n[loads]\n')
    messages = unlisted(root)
    assert messages == [
        "[external] app/runner.py is not listed in meizel.policy.toml",
        "[loads] app/loader.py -> lib is not listed in meizel.policy.toml",
    ]
    [found] = [d for d in diagnostics(root, "unlisted-allowance") if "external" in d.message]
    assert found.path == "app/meizel.toml"
    assert "a reason written in the manifest does not count" in found.render()


def test_a_missing_policy_allows_nothing(make_repo: RepoFactory) -> None:
    files = {**LOADER, "app/meizel.toml": MANIFEST, "meizel.toml": ROOT, "meizel.policy.toml": ""}
    root = make_repo(files)
    assert len(diagnostics(root, "unlisted-allowance")) == 2


def test_a_loads_target_outside_the_listed_ones_is_an_error(make_repo: RepoFactory) -> None:
    manifest = '[loads]\n"loader.py" = ["lib", "app"]\n'
    root = repo(make_repo, POLICY, manifest)
    assert unlisted(root) == ["[loads] app/loader.py -> app is not listed in meizel.policy.toml"]


def test_policy_entries_nothing_uses_do_no_harm(make_repo: RepoFactory) -> None:
    root = repo(make_repo, POLICY + '"app/gone.py" = ["lib"]\n')
    assert unlisted(root) == []


@pytest.mark.parametrize(
    "policy",
    [
        "this is not toml",
        "[mystery]\n",
        "external = 3\n",
        '[external]\n"a.py" = ""\n',
        '[external]\n"a.py" = 3\n',
        '[loads]\n"a.py" = "lib"\n',
        "[ambient-state]\nno-such-section = []\n",
        '[ambient-state.sink-facades]\n"a.py" = ""\n',
        "[ambient-state]\npure-callees = 3\n",
        '[ambient-state]\npure-callees = ["ok", 3]\n',
        '[type-checking]\nfiles = ["a.py"]\n',
        '[clock-lattice.family-modules]\n"a.py" = "a list is expected"\n',
        '[single-owner]\npostgres-dial = ["a.py"]\n',
        "[mystery.files]\n",
    ],
)
def test_a_malformed_policy_fails_at_its_own_path(make_repo: RepoFactory, policy: str) -> None:
    root = repo(make_repo, policy)
    found = diagnostics(root, "policy-syntax")
    assert found, policy
    assert {d.path for d in found} == {"meizel.policy.toml"}


LINTS = (
    '[ambient-state]\npure-callees = ["re.compile"]\n\n'
    '[ambient-state.sink-facades]\n"a.py" = "a write-only sink"\n\n'
    '[clock-lattice]\nfamily-modules = ["a.py"]\n\n'
    '[single-owner.postgres-dial]\n"b.py" = "cannot go through the owner"\n\n'
    '[type-checking.files]\n"c.py" = "a heavy import"\n'
)


def test_the_tables_of_the_lint_rules_are_part_of_the_policy(make_repo: RepoFactory) -> None:
    root = repo(make_repo, POLICY + "\n" + LINTS)
    assert diagnostics(root, "policy-syntax") == []
    assert unlisted(root) == []


def test_policy_diff_lists_the_lint_tables_too(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo, POLICY + "\n" + LINTS)
    commit(root)
    changed = (
        POLICY
        + '\n[ambient-state]\npure-callees = ["re.compile", "typing.TypeVar"]\n\n'
        + '[ambient-state.sink-facades]\n"a.py" = "another reason"\n\n'
        + '[single-owner.postgres-dial]\n"d.py" = "added"\n'
    )
    write(root, {"meizel.policy.toml": changed})
    assert run(root, "policy", "diff", "--against", "HEAD") == 0
    out = capsys.readouterr().out.splitlines()
    assert "+ [ambient-state.pure-callees] typing.TypeVar" in out
    assert "~ [ambient-state.sink-facades] a.py: a write-only sink -> another reason" in out
    assert "+ [single-owner.postgres-dial] d.py = added" in out
    assert "- [single-owner.postgres-dial] b.py = cannot go through the owner" in out
    assert "- [clock-lattice.family-modules] a.py" in out
    assert "- [type-checking.files] c.py = a heavy import" in out


def test_default_arguments_have_a_table_and_it_may_be_empty(make_repo: RepoFactory) -> None:
    root = repo(make_repo, POLICY + "\n[default-arguments]\n")
    assert diagnostics(root, "policy-syntax") == []


def test_policy_diff_lists_added_removed_and_changed_entries(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo, POLICY)
    commit(root)
    changed = '[external]\n"app/runner.py" = "a new reason"\n"app/extra.py" = "added"\n\n[loads]\n'
    write(root, {"meizel.policy.toml": changed})
    assert run(root, "policy", "diff", "--against", "HEAD") == 0
    out = capsys.readouterr().out.splitlines()
    assert "+ [external] app/extra.py = added" in out
    assert "~ [external] app/runner.py: runs the user's code -> a new reason" in out
    assert "- [loads] app/loader.py = [lib]" in out


def test_policy_diff_is_silent_when_nothing_changed(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo, POLICY)
    commit(root)
    assert run(root, "policy", "diff", "--against", "HEAD") == 0
    assert capsys.readouterr().out == ""


def test_policy_diff_treats_an_unborn_policy_as_empty(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({**LOADER, "app/meizel.toml": MANIFEST, "meizel.toml": ROOT})
    git(root, "rm", "-q", "--cached", "meizel.policy.toml")
    commit(root, "no policy")
    write(root, {"meizel.policy.toml": POLICY})
    assert run(root, "policy", "diff", "--against", "HEAD") == 0
    assert "+ [external] app/runner.py" in capsys.readouterr().out
    assert analysis_of(root).verdict is not None
