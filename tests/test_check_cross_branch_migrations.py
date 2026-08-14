"""Tests for scripts/check_cross_branch_migrations.py — the single-branch tripwire.

The previous version of this file asserted `main() == 0` unconditionally and that
the script "never touches git". That was an accurate test of a pass-through that
could not fail — which is precisely why the CI step was worthless. These tests
assert the opposite property: that it fires when its premise breaks.
"""

from __future__ import annotations

import importlib

import pytest

_MOD = "scripts.check_cross_branch_migrations"


def _mod():
    return importlib.import_module(_MOD)


@pytest.mark.parametrize(
    "branch",
    ["develop", "development", "staging", "next", "trunk", "release/1.2", "hotfix/x", "support/2"],
)
def test_breach_detected(branch: str) -> None:
    """Every name/prefix denoting a parallel integration line is a breach."""
    assert _mod().breaches(["main", branch]) == [branch]


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "ava-2201-lint-gate",  # ordinary feature branch — numerous, short-lived
        "ava-2203-migration-gates",
        "developer-notes",  # contains 'develop', is not that branch
        "release-notes",  # 'release' without the '/' — not a release line
    ],
)
def test_ordinary_branches_are_not_breaches(branch: str) -> None:
    """No heuristic: feature branches and lookalike names must never fire.

    `developer-notes` / `release-notes` guard the exact-match and prefix
    boundaries — a substring test or `startswith("release")` would false-positive
    on both, and a tripwire that cries wolf gets disabled.
    """
    assert _mod().breaches([branch]) == []


def test_passes_on_single_branch_model(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The premise holding is reported, not silently assumed."""
    mod = _mod()
    monkeypatch.setattr(mod, "remote_branches", lambda: ["main", "ava-1-x"])
    assert mod.main() == 0
    assert "single-branch premise holds" in capsys.readouterr().out


def test_fails_when_premise_breaks(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A long-lived second branch fails the step and names the remedy."""
    mod = _mod()
    monkeypatch.setattr(mod, "remote_branches", lambda: ["main", "develop"])
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "premise BROKEN" in err
    assert "develop" in err
    assert "Reinstate" in err


def test_unqueryable_remote_fails_rather_than_passing(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """An unverifiable premise must not report success.

    Returning 0 here would rebuild the silent pass-through this script replaced:
    a gate that cannot answer has to say so, not say 'fine'.
    """
    mod = _mod()

    def _boom() -> list[str]:
        raise RuntimeError("git ls-remote failed (rc=128): no such remote")

    monkeypatch.setattr(mod, "remote_branches", _boom)
    assert mod.main() == 1
    assert "cannot verify the single-branch premise" in capsys.readouterr().err


def test_remote_branches_parses_ls_remote_output(monkeypatch: pytest.MonkeyPatch) -> None:
    r"""Tab-separated `<sha>\trefs/heads/<name>` lines; non-head refs ignored."""
    mod = _mod()

    class _Proc:
        returncode = 0
        stdout = "abc123\trefs/heads/main\ndef456\trefs/heads/release/1.2\n999\trefs/tags/v1\n"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: _Proc())
    assert mod.remote_branches() == ["main", "release/1.2"]


def test_remote_branches_raises_on_git_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero git exit propagates as RuntimeError, never an empty list."""
    mod = _mod()

    class _Proc:
        returncode = 128
        stdout = ""
        stderr = "fatal: 'origin' does not appear to be a git repository"

    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: _Proc())
    with pytest.raises(RuntimeError, match="ls-remote failed"):
        mod.remote_branches()
