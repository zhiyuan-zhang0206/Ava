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
from shared.platform import IS_WINDOWS


def _make_repo(tmp_path: Path, lock_body: str) -> Path:
    """A repo skeleton with ui/web/package-lock.json holding `lock_body`."""
    fe = tmp_path / "ui" / "web"
    fe.mkdir(parents=True)
    (fe / "package-lock.json").write_text(lock_body)
    return tmp_path


@pytest.fixture
def fake_npm_ci(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Replace `subprocess.run` with a recorder that mimics `npm ci` by creating
    node_modules (real `npm ci` wipes+recreates it). Returns the list of cwds it
    was invoked with — empty list == install was skipped."""
    calls: list[Path] = []

    def _run(
        cmd: list[str],
        cwd: str,
        check: bool,
        shell: bool = False,
        env: dict[str, str] | None = None,
    ):  # test stub mirrors subprocess.run signature (shell=IS_WINDOWS for npm.cmd)
        del check, shell
        assert cmd == ["npm", "ci"]
        assert env is not None
        (Path(cwd) / "node_modules").mkdir(exist_ok=True)
        calls.append(Path(cwd))

    monkeypatch.setattr(_repo.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    return calls


def test_installs_when_node_modules_missing(tmp_path: Path, fake_npm_ci: list[Path]) -> None:
    repo = _make_repo(tmp_path, '{"lock": 1}')
    _repo._ensure_frontend_deps(repo)
    assert fake_npm_ci == [repo / "ui" / "web"]
    # stamp written so the next call is a noop
    assert (repo / "ui" / "web" / "node_modules" / ".ava-lock-hash").is_file()


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX toolchain paths are injected only on POSIX")
def test_npm_ci_injects_toolchain_path_from_a_minimal_non_login_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ava start` must find npm without a shell profile adding Homebrew's bin."""
    from shared import session_env

    repo = _make_repo(tmp_path, '{"lock": 1}')
    captured: dict[str, str] = {}

    def _run(
        cmd: list[str],
        cwd: str,
        check: bool,
        shell: bool = False,
        env: dict[str, str] | None = None,
    ) -> None:
        del check, shell
        assert cmd == ["npm", "ci"]
        assert env is not None
        captured.update(env)
        (Path(cwd) / "node_modules").mkdir(exist_ok=True)

    monkeypatch.setattr(session_env.os, "environ", {"PATH": "/usr/bin:/bin"})
    monkeypatch.setattr(_repo.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]

    _repo._ensure_frontend_deps(repo)

    assert captured["PATH"].split(":")[:4] == [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]


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
    (repo / "ui" / "web" / "package-lock.json").write_text('{"lock": 2, "added": "dep"}')
    _repo._ensure_frontend_deps(repo)
    assert fake_npm_ci == [repo / "ui" / "web"]


def test_reinstalls_when_node_modules_exists_without_stamp(
    tmp_path: Path, fake_npm_ci: list[Path]
) -> None:
    """Legacy install (node_modules from before this stamp existed) → reinstall
    once so the stamp gets written; a bare node_modules is no longer trusted."""
    repo = _make_repo(tmp_path, '{"lock": 1}')
    (repo / "ui" / "web" / "node_modules").mkdir()  # exists, but no .ava-lock-hash
    _repo._ensure_frontend_deps(repo)
    assert fake_npm_ci == [repo / "ui" / "web"]
