"""`_ensure_frontend_deps` unit tests -- lockfile-drift reinstall logic.

Regression guard: after `ava cluster update` pulls a package-lock.json with new dependencies,
it must re-run `npm ci`; otherwise `npm run build` fails immediately due to missing
deps, the frontend session exits, and port 3000 refuses connections (root cause
of the 2026-06-07 prod outage).

All `npm ci` calls are intercepted -- no real install, only verifying "when to install
/ when to skip".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands import _repo


def _make_repo(tmp_path: Path, lock_body: str) -> Path:
    """A repo skeleton with frontend/package-lock.json holding `lock_body`."""
    fe = tmp_path / "frontend"
    fe.mkdir()
    (fe / "package-lock.json").write_text(lock_body)
    return tmp_path


@pytest.fixture
def fake_npm_ci(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Replace `subprocess.run` with a recorder that mimics `npm ci` by creating
    node_modules (real `npm ci` wipes+recreates it). Returns the list of cwds it
    was invoked with — empty list == install was skipped."""
    calls: list[Path] = []

    def _run(
        cmd, cwd, check, shell=False
    ):  # test stub mirrors subprocess.run signature (shell=IS_WINDOWS for npm.cmd)
        assert cmd == ["npm", "ci"]
        (Path(cwd) / "node_modules").mkdir(exist_ok=True)  # pyright: ignore[reportUnknownArgumentType]
        calls.append(Path(cwd))  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(_repo.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    return calls


def test_installs_when_node_modules_missing(tmp_path: Path, fake_npm_ci: list[Path]) -> None:
    repo = _make_repo(tmp_path, '{"lock": 1}')
    _repo._ensure_frontend_deps(repo)
    assert fake_npm_ci == [repo / "frontend"]
    # stamp written so the next call is a noop
    assert (repo / "frontend" / "node_modules" / ".ava-lock-hash").is_file()


def test_skips_when_stamp_matches_lockfile(tmp_path: Path, fake_npm_ci: list[Path]) -> None:
    repo = _make_repo(tmp_path, '{"lock": 1}')
    _repo._ensure_frontend_deps(repo)  # first install writes the stamp
    fake_npm_ci.clear()
    _repo._ensure_frontend_deps(repo)  # lockfile unchanged → skip
    assert fake_npm_ci == []


def test_reinstalls_when_lockfile_changed(tmp_path: Path, fake_npm_ci: list[Path]) -> None:
    repo = _make_repo(tmp_path, '{"lock": 1}')
    _repo._ensure_frontend_deps(repo)  # installs against lock v1
    fake_npm_ci.clear()
    # `ava cluster update` pulls a lockfile that added a dependency
    (repo / "frontend" / "package-lock.json").write_text('{"lock": 2, "added": "dep"}')
    _repo._ensure_frontend_deps(repo)
    assert fake_npm_ci == [repo / "frontend"]


def test_reinstalls_when_node_modules_exists_without_stamp(
    tmp_path: Path, fake_npm_ci: list[Path]
) -> None:
    """Legacy install (node_modules from before this stamp existed) → reinstall
    once so the stamp gets written; a bare node_modules is no longer trusted."""
    repo = _make_repo(tmp_path, '{"lock": 1}')
    (repo / "frontend" / "node_modules").mkdir()  # exists, but no .ava-lock-hash
    _repo._ensure_frontend_deps(repo)
    assert fake_npm_ci == [repo / "frontend"]
