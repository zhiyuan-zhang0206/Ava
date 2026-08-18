"""Process-commit capture — the properties that make it process state.

Two of these tests are the whole point of the module and are worth stating
plainly, because a plausible-looking implementation passes everything else and
still reproduces the bug it exists to prevent:

- `get()` never reads git. An implementation that lazily resolves HEAD on first
  read answers with whatever the checkout became, so a daemon that outlived a
  rollout would report the *new* commit and look aligned.
- `freeze()` keeps its first answer. The capture is meant to describe the code
  the process loaded, which cannot change without a restart; a re-reading
  `freeze()` would let a later caller overwrite that with a newer commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shared import process_sha


@pytest.fixture(autouse=True)
def _fresh_capture():
    """Each test starts from an unfrozen process and leaves one behind."""
    process_sha._reset_for_tests()
    yield
    process_sha._reset_for_tests()


def test_get_is_none_before_freeze() -> None:
    """An unfrozen process reports unknown rather than resolving HEAD on demand.

    This is the guard against the original bug: any read path that can reach git
    is a read path that answers for the *current* checkout, not for the code the
    process is executing."""
    assert process_sha.get() is None


def test_get_never_shells_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with git available and a moved checkout, `get()` stays silent until
    someone froze — it has no git call to make."""

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("get() must not resolve git")

    monkeypatch.setattr(subprocess, "run", _explode)
    assert process_sha.get() is None


def test_freeze_captures_this_trees_head() -> None:
    """The capture is the commit of the tree the module was loaded from — the
    checkout under test, resolved from `__file__` rather than the cwd."""
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(process_sha.__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert process_sha.freeze() == expected
    assert process_sha.get() == expected


def test_freeze_keeps_the_first_answer_when_the_checkout_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second freeze after the checkout advanced re-uses the first capture.

    Stand-in for the real sequence: a daemon boots on commit A, a rollout moves
    the checkout to B, and something in-process calls freeze() again. The daemon
    is still executing A, so A is the only honest answer."""
    shas = iter(["aaaaaaa1111", "bbbbbbb2222"])

    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=next(shas) + "\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert process_sha.freeze() == "aaaaaaa1111"
    assert process_sha.freeze() == "aaaaaaa1111"
    assert process_sha.get() == "aaaaaaa1111"


def test_freeze_is_none_outside_a_git_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tarball / installed-package deploy has no commit; that is unknown, not
    a crash, and not a guess."""

    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="not a repo")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert process_sha.freeze() is None
    assert process_sha.get() is None


def test_freeze_survives_a_missing_git_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Capturing a commit is bookkeeping; it must never take a daemon down."""

    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert process_sha.freeze() is None


def test_freeze_survives_a_hung_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """The capture is bounded — a wedged git cannot stall a daemon's boot."""

    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert process_sha.freeze() is None
