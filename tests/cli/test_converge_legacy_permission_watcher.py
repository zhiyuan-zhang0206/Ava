"""Converge's one-shot cleanup of the removed macOS permission-prompt watcher."""

from __future__ import annotations

from pathlib import Path

import pytest

import cli.commands._converge as cv
import cli.commands._converge_legacy_permission_watcher as clpw

_LABEL = "com.ava.permission-watcher"


def _ctx(tmp_path: Path) -> cv.ConvergeCtx:
    return cv.ConvergeCtx(repo=Path("/repo"), ava_home=tmp_path, roles=cv.ALL_ROLES)


def _step() -> cv.ConvergeStep:
    return next(s for s in cv.CONVERGE_STEPS if s.name == "legacy macOS permission-watcher removal")


def _plist_path(tmp_path: Path) -> Path:
    return tmp_path / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _patch_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the macOS branch so these tests exercise the step on every host
    (CI is Linux; the real IS_MACOS would no-op the step there).

    The step module binds IS_MACOS at import time, so its own module global
    is patched — patching shared.platform would not reach the step.
    """
    monkeypatch.setattr(clpw, "IS_MACOS", True)


def _fake_launchd(
    monkeypatch: pytest.MonkeyPatch, loaded: bool, bootout_ok: bool = True
) -> dict[str, int]:
    """Stub launchd state on a macOS host; `loaded` stays true until a
    successful bootout."""
    _patch_macos(monkeypatch)
    calls = {"bootout": 0}

    def fake_job_loaded(label: str) -> bool:
        assert label == _LABEL
        return loaded and calls["bootout"] == 0

    def fake_bootout_and_wait(label: str) -> bool:
        assert label == _LABEL
        calls["bootout"] += 1
        return bootout_ok

    monkeypatch.setattr(clpw, "_job_loaded", fake_job_loaded)
    monkeypatch.setattr(clpw, "_bootout_and_wait", fake_bootout_and_wait)
    return calls


def _patch_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    def _home(_cls: type[Path]) -> Path:
        return home

    monkeypatch.setattr(Path, "home", classmethod(_home))


def test_step_is_registered_host_global() -> None:
    assert _step().host_global


def test_noop_on_non_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(clpw, "IS_MACOS", False)
    _patch_home(monkeypatch, tmp_path)
    _step().apply(_ctx(tmp_path))
    assert not _plist_path(tmp_path).exists()


def test_noop_when_nothing_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_macos(monkeypatch)
    calls = _fake_launchd(monkeypatch, loaded=False)
    _patch_home(monkeypatch, tmp_path)
    _step().apply(_ctx(tmp_path))
    assert calls["bootout"] == 0
    assert not _plist_path(tmp_path).exists()


def test_boots_out_and_removes_plist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _fake_launchd(monkeypatch, loaded=True)
    plist = _plist_path(tmp_path)
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    _patch_home(monkeypatch, tmp_path)
    _step().apply(_ctx(tmp_path))
    assert calls["bootout"] == 1
    assert not plist.exists()


def test_removes_stale_plist_without_loaded_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_launchd(monkeypatch, loaded=False)
    plist = _plist_path(tmp_path)
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    _patch_home(monkeypatch, tmp_path)
    _step().apply(_ctx(tmp_path))
    assert calls["bootout"] == 0
    assert not plist.exists()


def test_bootout_failure_keeps_plist_and_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_launchd(monkeypatch, loaded=True, bootout_ok=False)
    plist = _plist_path(tmp_path)
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    _patch_home(monkeypatch, tmp_path)
    _step().apply(_ctx(tmp_path))
    assert calls["bootout"] == 1
    assert plist.exists()  # kept so the operator can see what is still loaded
