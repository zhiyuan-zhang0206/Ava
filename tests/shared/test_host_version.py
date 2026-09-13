"""shared.host_version — the derived host version and its fallback chain.

The derivation reads the checkout HEAD commit's date (`YYYY.M.D`, no zero
padding — the gate-comparable form) and the short SHA for the display form; a
checkout-less directory falls back to `[project].version`, and neither source
raises `HostVersionError` instead of guessing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from shared import host_version as hv

_COMMIT_DATE = "2026-03-05T12:00:00+00:00"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"GIT_AUTHOR_DATE": _COMMIT_DATE, "GIT_COMMITTER_DATE": _COMMIT_DATE}
    return subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True
    )
    (repo / "file.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "one")
    return repo


def test_host_version_derives_from_commit_date(git_repo: Path) -> None:
    # Zero-padded date parts are stripped: 2026-03-05 -> "2026.3.5".
    assert hv.host_version(git_repo) == "2026.3.5"


def test_host_version_display_adds_short_sha(git_repo: Path) -> None:
    sha = _git(git_repo, "rev-parse", "--short", "HEAD").stdout.strip()
    assert hv.host_version_display(git_repo) == f"2026.3.5+g{sha}"


def test_commit_date_moves_with_head(git_repo: Path) -> None:
    (git_repo / "file.txt").write_text("y", encoding="utf-8")
    _git(git_repo, "add", "-A")
    env = {
        "GIT_AUTHOR_DATE": "2027-11-30T08:00:00+00:00",
        "GIT_COMMITTER_DATE": "2027-11-30T08:00:00+00:00",
    }
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        [
            "git",
            "-C",
            str(git_repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "two",
        ],
        check=True,
        capture_output=True,
        env=os.environ | env,
    )
    assert hv.host_version(git_repo) == "2027.11.30"


def test_fallback_to_pyproject_without_git(tmp_path: Path) -> None:
    d = tmp_path / "wheel"
    d.mkdir()
    (d / "pyproject.toml").write_text(
        '[project]\nname = "ava"\nversion = "0.1.5"\n', encoding="utf-8"
    )
    assert hv.host_version(d) == "0.1.5"
    assert hv.host_version_display(d) == "0.1.5"


def test_neither_source_raises(tmp_path: Path) -> None:
    d = tmp_path / "bare"
    d.mkdir()
    with pytest.raises(hv.HostVersionError):
        hv.host_version(d)
