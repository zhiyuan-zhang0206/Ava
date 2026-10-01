"""The pinned WAL-G binary: verified before it is installed, one stable path, Linux x86_64 only."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from base.cluster.dataplane import walg_binary

_FAKE_WALG = b'#!/bin/sh\necho "wal-g version v3.0.9 deadbeef 2026.08.20 PostgreSQL"\n'


def _serving(blob: bytes) -> Callable[..., bytes]:
    """A downloader stand-in that answers every URL with `blob`."""

    def download(_url: str, **_kwargs: object) -> bytes:
        return blob

    return download


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """A home under tmp, a Linux x86_64 platform, and a pin for the fake artifact.

    Returns the list of URLs the (stubbed) downloader was asked for.
    """
    monkeypatch.setenv("AVA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(walg_binary.platform, "system", lambda: "Linux")
    monkeypatch.setattr(walg_binary.platform, "machine", lambda: "x86_64")
    monkeypatch.setitem(
        walg_binary._WALG_ARTIFACTS,
        "linux-x86_64",
        ("wal-g-fake", hashlib.sha256(_FAKE_WALG).hexdigest()),
    )
    fetched: list[str] = []

    def download(url: str, **_kwargs: object) -> bytes:
        fetched.append(url)
        return _FAKE_WALG

    monkeypatch.setattr(walg_binary, "_download", download)
    return fetched


def test_only_linux_x86_64_is_pinned() -> None:
    assert set(walg_binary._WALG_ARTIFACTS) == {"linux-x86_64"}


@pytest.mark.parametrize(("system", "machine"), [("Darwin", "arm64"), ("Linux", "aarch64")])
def test_other_platforms_fail_fast(
    pinned: list[str], monkeypatch: pytest.MonkeyPatch, system: str, machine: str
) -> None:
    monkeypatch.setattr(walg_binary.platform, "system", lambda: system)
    monkeypatch.setattr(walg_binary.platform, "machine", lambda: machine)

    with pytest.raises(RuntimeError, match="no pinned wal-g artifact for"):
        walg_binary.ensure_walg_binary()

    assert pinned == []
    assert "no pinned wal-g artifact" in (walg_binary.installed_problem() or "")


def test_install_downloads_verifies_and_publishes_an_executable(pinned: list[str]) -> None:
    path = walg_binary.ensure_walg_binary()

    assert path == walg_binary.walg_path()
    assert path.name == "wal-g" and path.parent.name == "walg"
    assert pinned == [
        f"https://github.com/wal-g/wal-g/releases/download/{walg_binary.WALG_VERSION}/wal-g-fake"
    ]
    assert path.read_bytes() == _FAKE_WALG
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert walg_binary.installed_problem() is None
    assert [p.name for p in path.parent.iterdir()] == ["wal-g"], "no staging file is left behind"


def test_install_is_idempotent(pinned: list[str]) -> None:
    walg_binary.ensure_walg_binary()
    walg_binary.ensure_walg_binary()

    assert len(pinned) == 1


def test_checksum_mismatch_installs_nothing(
    pinned: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(walg_binary, "_download", _serving(_FAKE_WALG + b"tampered"))

    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        walg_binary.ensure_walg_binary()

    assert not walg_binary.walg_path().exists()


def test_wrong_version_never_displaces_the_installed_binary(
    pinned: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    walg_binary.ensure_walg_binary()
    stale = b'#!/bin/sh\necho "wal-g version v0.0.1"\n'
    monkeypatch.setitem(
        walg_binary._WALG_ARTIFACTS,
        "linux-x86_64",
        ("wal-g-fake", hashlib.sha256(stale).hexdigest()),
    )
    monkeypatch.setattr(walg_binary, "_download", _serving(stale))

    with pytest.raises(RuntimeError, match=r"does not report v3\.0\.9"):
        walg_binary.ensure_walg_binary()

    assert walg_binary.walg_path().read_bytes() == _FAKE_WALG
    assert [p.name for p in walg_binary.walg_path().parent.iterdir()] == ["wal-g"]


def test_installed_problem_names_each_defect(pinned: list[str]) -> None:
    assert "is not installed" in (walg_binary.installed_problem() or "")

    path = walg_binary.ensure_walg_binary()
    path.write_bytes(_FAKE_WALG + b"# edited by hand\n")
    assert "is not the pinned v3.0.9 build" in (walg_binary.installed_problem() or "")

    path.write_bytes(_FAKE_WALG)
    path.chmod(0o644)
    assert "is not executable" in (walg_binary.installed_problem() or "")

    path.chmod(0o755)
    assert walg_binary.installed_problem() is None
    assert os.access(path, os.X_OK)
