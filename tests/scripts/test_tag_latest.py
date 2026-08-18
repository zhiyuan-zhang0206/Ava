"""scripts/tag_latest.py: pure logic (reachability guard, move semantics).

The git-touching paths are exercised by real deployments, not here
(same convention as test_release_cut).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts.tag_latest import move_latest, sha_reachable_from_main


def test_reachable_from_main_accepts_ancestor(fake_git) -> None:
    with patch("scripts.tag_latest.git", side_effect=fake_git):
        assert sha_reachable_from_main("abc123") is True


def test_unreachable_sha_fetches_then_retries(fake_git) -> None:
    # first merge-base fails, fetch succeeds, second merge-base still fails
    def flaky(*args, **kwargs):
        if args[:2] == ("merge-base", "--is-ancestor"):
            return subprocess_result(1)
        if args[:2] == ("fetch", "origin"):
            return subprocess_result(0)
        raise AssertionError(f"unexpected git call: {args}")

    with patch("scripts.tag_latest.git", side_effect=flaky):
        assert sha_reachable_from_main("badsha") is False


def test_move_latest_creates_only_with_flag(fake_git) -> None:
    with patch("scripts.tag_latest.git", side_effect=fake_git) as m:
        old, new = move_latest("sha1", create=False)
        assert (old, new) == (None, "sha1")
        # no git calls happened: missing tag + no create = no-op
        assert m.call_count == 1  # current_latest() only


def subprocess_result(rc: int):
    import subprocess

    return subprocess.CompletedProcess([], rc, stdout="", stderr="")


@pytest.fixture
def fake_git():
    import subprocess

    def _fake(*args, **kwargs):
        if args[:2] == ("merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")
        if args[:2] == ("rev-parse", "-q"):
            return subprocess.CompletedProcess([], 1, stdout="", stderr="")
        if args[:2] == ("fetch", "origin"):
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    return _fake
