"""`cli.commands._installed_sha` — the shell-chain seam for the install bookmark.

The cmd.exe update chain hand-builds the ladder the POSIX side runs in-process, and
it never recorded `shared.source_integrity.set_installed`. So every Windows
self-update handed its own trailing `ava start` a HEAD that did not match
`installed_sha`, and the source-integrity guard reported the rollout as tampering
(win, 2026-08-12: `HEAD c5f0539 / installed 902af72`) and re-ran `uv sync` to heal
something that was never broken.

These pin the seam's contract: it resolves HEAD itself (a watchdog self-heal checks
out `origin/<track>` and only git knows what that landed on), it writes the bookmark,
and it reports a failure by exit code rather than raising — the chain calls it
fail-soft (`|| ver>nul`) and an unwritable bookmark must never abort an update.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        },
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A one-commit checkout, with `$AVA_HOME` pointed somewhere writable."""
    checkout = tmp_path / "source"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    (checkout / "f").write_text("x")
    _git(checkout, "add", "f")
    _git(checkout, "commit", "-qm", "one")
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr("cli.commands._repo._repo_root", lambda: checkout)
    return checkout


def _head(repo: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def test_it_records_the_checkouts_current_head(repo: Path) -> None:
    """The sha is resolved here, not passed in: the chain does not always know it as
    a literal — a watchdog self-heal checks out a ref, not a commit."""
    from cli.commands import _installed_sha
    from shared import source_integrity

    assert _installed_sha._main() == 0
    assert source_integrity.get() == _head(repo)


def test_a_second_run_moves_the_bookmark_to_the_new_head(repo: Path) -> None:
    """Idempotent in the way that matters: every update writes the bookmark again,
    and the value tracks HEAD rather than sticking at the first one recorded."""
    from cli.commands import _installed_sha
    from shared import source_integrity

    _installed_sha._main()
    (repo / "f").write_text("y")
    _git(repo, "commit", "-aqm", "two")

    assert _installed_sha._main() == 0
    assert source_integrity.get() == _head(repo)


def test_a_checkout_git_cannot_read_exits_nonzero_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-soft contract the chain depends on. `|| ver>nul` turns a non-zero
    exit into "carry on"; an exception would escape cmd.exe's `||` entirely and take
    down an update over a bookmark."""
    from cli.commands import _installed_sha

    not_a_repo = tmp_path / "bare"
    not_a_repo.mkdir()
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr("cli.commands._repo._repo_root", lambda: not_a_repo)

    assert _installed_sha._main() == 1


def test_it_never_records_an_empty_bookmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty stdout from a git that "succeeded" must not be written: `get()` reads
    an empty file back as None, so the guard would silently re-seed on the next start
    and the drift it exists to catch would go unreported."""
    from cli.commands import _installed_sha
    from shared import source_integrity

    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr("cli.commands._repo._repo_root", lambda: tmp_path)

    def _blank_head(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="\n", stderr="")

    monkeypatch.setattr(_installed_sha.subprocess, "run", _blank_head)

    assert _installed_sha._main() == 1
    assert source_integrity.get() is None
