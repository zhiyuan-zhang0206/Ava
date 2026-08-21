"""`ava cluster update`'s first line names the cluster HOME, not just the checkout.

Task #983 (audit 01-update-sequence #5): the 2026-08-07 01:13 rollout ran from a
worktree checkout, and the log's first line showed only `cwd = <repo>`. Cluster
identity IS the home path (AGENTS.md), so an operator or tool reading that line
could not tell which cluster the rollout was acting on — a worktree cluster and
the prod checkout were indistinguishable. The identity line must carry the
resolved home and its registry record (or an explicit unregistered marker).

Issue #216 (2026-08-21): `cmd_update` no longer dispatches by machine_role —
every host POSTs the gateway, and `--local` runs the in-process gateway
orchestration (`_run_gateway_orchestration`), which prints the identity line
before any repo work. These tests stop the dispatch at that seam.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import commands as _cli
from cli.commands import update as _update

_REPO = Path("/repo")
_HOME = Path("/home/ava-prod")


@pytest.fixture
def runner_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pure `ava cluster update --local` that stops at the identity line.

    `_repo_root` / `ava_home` / `get_record` are update.py module globals
    (the `cli.commands` package re-exports only `_repo_root`/dispatch names —
    patching the package attr would not reach cmd_update's global lookup).
    `_run_gateway_orchestration` is looked up on the `cli.commands` package at
    call time inside `_update_dispatch._run_in_process`, so patching the
    package attr stops the dispatch before any repo/git work.
    """
    monkeypatch.setattr(_update, "_repo_root", lambda: _REPO)
    monkeypatch.setattr(_update, "ava_home", lambda: _HOME)
    monkeypatch.setattr(_cli, "_run_gateway_orchestration", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]


def test_identity_line_prints_home_and_registry_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runner_env: None,
) -> None:
    """A registered home is named on the first line: cwd + home + registry record."""
    monkeypatch.setattr(_update, "get_record", lambda _h: SimpleNamespace(name="ava-prod"))  # pyright: ignore[reportUnknownArgumentType]

    assert _cli.cmd_update(local=True) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert str(_REPO) in first
    assert str(_HOME) in first
    assert "ava-prod" in first


def test_identity_line_marks_unregistered_home(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runner_env: None,
) -> None:
    """An unregistered home says so explicitly — the 01:13 worktree case."""
    monkeypatch.setattr(_update, "get_record", lambda _h: None)  # pyright: ignore[reportUnknownArgumentType]

    assert _cli.cmd_update(local=True) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert str(_REPO) in first
    assert str(_HOME) in first
    assert "(unregistered)" in first
