"""`services.healthchecks.frontend` unit tests — `curl` health check + session restart branches."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from services.healthchecks import frontend as hc


class _FakeResult:
    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr


def test_is_alive_curl_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args, **kwargs):
        assert args[0] == "curl"
        return _FakeResult(returncode=0)

    monkeypatch.setattr(hc.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._is_alive() is True


def test_is_alive_curl_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args, **kwargs):
        return _FakeResult(returncode=7)  # curl exit 7 = "Failed to connect"

    monkeypatch.setattr(hc.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._is_alive() is False


def test_is_alive_curl_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args, **kwargs):
        raise FileNotFoundError("curl not in PATH")

    monkeypatch.setattr(hc.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._is_alive() is False


def test_is_alive_curl_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="curl", timeout=5)

    monkeypatch.setattr(hc.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._is_alive() is False


def test_restart_routes_through_respawn_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_restart routes through respawn_service — the shared service-respawn helper
    that kills any stale session on both backends and launches through the
    session backend (native on POSIX, winproc on Windows)."""
    frontend_dir = tmp_path / "ui" / "web"
    frontend_dir.mkdir(parents=True)
    from shared.config import settings

    monkeypatch.setattr(settings.services, "project_root", tmp_path)

    captured: list[tuple[str, str, object, object, object]] = []

    def fake_respawn(service, cmd, repo, *, checkout=None, extra_env=None) -> bool:
        captured.append((service, cmd, repo, checkout, extra_env))  # pyright: ignore[reportUnknownArgumentType]
        return True

    monkeypatch.setattr(hc, "respawn_service", fake_respawn)  # pyright: ignore[reportUnknownArgumentType]
    ok = hc._restart()
    assert ok is True
    assert len(captured) == 1
    service, cmd, repo, checkout, extra_env = captured[0]
    assert service == "frontend"
    assert repo == frontend_dir
    # npm runs in ui/web/, but the launch-site guard judges the checkout above
    # it — passing the subdirectory as both made the guard refuse every prod
    # restart, so the frontend could never self-heal.
    assert checkout == tmp_path
    assert extra_env == {"AVA_PROCESS_PROFILE": "gateway"}
    # build + start ride the respawn command, and the serve stage execs — the
    # session validator (shared.session_env.exec_into) rejects a compound
    # command whose final stage does not hand over its pid, so without this the
    # respawn could never launch and a dead frontend could never self-heal.
    assert "npm run build" in cmd
    assert "exec npm run start -- -p" in cmd
    from shared.session_env import exec_into

    assert exec_into(cmd) == cmd


def test_restart_injects_gateway_port_matching_servicespec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The respawn build must carry the same NEXT_PUBLIC_GATEWAY_PORT prefix as the
    canonical ServiceSpec — both derive from settings.gateway.gateway_port via the shared
    helper, so a watchdog restart can never bake a stale port (the 2026-06-23 prod
    outage). Asserting the exact prefix on the respawn command pins the two paths."""
    frontend_dir = tmp_path / "ui" / "web"
    frontend_dir.mkdir(parents=True)
    from shared.config import settings

    monkeypatch.setattr(settings.services, "project_root", tmp_path)
    monkeypatch.setattr(settings.gateway, "gateway_port", 8800)

    cmds: list[str] = []
    monkeypatch.setattr(
        hc,
        "respawn_service",
        lambda _s, cmd, _repo, **_kw: cmds.append(cmd) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert hc._restart() is True
    assert len(cmds) == 1
    # exact prefix-before-build, identical to cli/commands/_repo.py's ServiceSpec
    assert "NEXT_PUBLIC_GATEWAY_PORT=8800 npm run build" in cmds[0]

    # And it really is the SAME string the ServiceSpec uses (no drift).
    import cli.commands._repo as repo

    spec = next(s for s in repo.build_services() if s.session == "frontend")
    assert "NEXT_PUBLIC_GATEWAY_PORT=8800 npm run build" in spec.cmd


def test_respawn_command_is_the_spec_command_single_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The watchdog respawn and the canonical ServiceSpec build the SAME command
    (shared.cluster.frontend_service_cmd) — the single source that makes drift
    impossible. The 2026-08-27 prod outage was exactly this drift: the respawn
    lost its `exec`, the session validator rejected the command, and a dead
    frontend could never self-heal. Locking both call sites to the builder (and
    the builder to the validator) closes it for good."""
    frontend_dir = tmp_path / "ui" / "web"
    frontend_dir.mkdir(parents=True)
    from shared.config import settings

    monkeypatch.setattr(settings.services, "project_root", tmp_path)

    cmds: list[str] = []
    monkeypatch.setattr(
        hc,
        "respawn_service",
        lambda _s, cmd, _repo, **_kw: cmds.append(cmd) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert hc._restart() is True
    assert len(cmds) == 1
    cmd = cmds[0]

    from urllib.parse import urlsplit

    from shared.cluster import frontend_service_cmd

    # The respawn command is exactly the shared builder's output for this
    # frontend dir...
    port = urlsplit(hc._FRONTEND_URL).port or 3000
    assert cmd == frontend_service_cmd(port, frontend_dir)
    # ...and the spec's command is the same builder's output for the checkout
    # root, so the two launch paths differ only in the session's working
    # directory, never in the command shape.
    import cli.commands._repo as repo

    spec = next(s for s in repo.build_services() if s.session == "frontend")
    assert spec.cmd == frontend_service_cmd(port)
    # Same build env prefix in both (the port baked into the bundle).
    assert f"NEXT_PUBLIC_GATEWAY_PORT={settings.gateway.gateway_port}" in cmd
    assert f"NEXT_PUBLIC_GATEWAY_PORT={settings.gateway.gateway_port}" in spec.cmd


def test_main_skips_restart_when_session_exists_but_curl_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """session exists + curl fails = still building; main() should not call _restart.

    Trade-off: when a session hangs (process exists but is stuck) we also skip, requires manual cleanup.
    """
    monkeypatch.setattr(hc, "_is_alive", lambda: False)
    monkeypatch.setattr(hc, "_session_exists", lambda: True)

    restart_calls: list[bool] = []
    monkeypatch.setattr(hc, "_restart", lambda: restart_calls.append(True) or True)
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]

    hc.main()
    assert restart_calls == []  # _restart was not called


def test_main_restarts_when_session_gone_and_curl_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """session gone + curl fails = truly dead, calls _restart."""
    monkeypatch.setattr(hc, "_is_alive", lambda: False)
    monkeypatch.setattr(hc, "_session_exists", lambda: False)

    restart_calls: list[bool] = []
    monkeypatch.setattr(hc, "_restart", lambda: restart_calls.append(True) or True)
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]

    hc.main()
    assert restart_calls == [True]


def test_session_exists_asks_the_session_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The build-in-progress gate reads the session backend (native supervisor on
    POSIX, winproc on Windows) — the same name-keyed backend respawn launches
    into — so a session that is up (mid-build) is never kill-restarted."""

    class _FakeBackend:
        def __init__(self, answer: bool) -> None:
            self.answer = answer

        def has_session(self, name: str) -> bool:
            assert name == hc._session_name()
            return self.answer

    monkeypatch.setattr("shared.session_backend.get_backend", lambda: _FakeBackend(True))
    assert hc._session_exists() is True

    monkeypatch.setattr("shared.session_backend.get_backend", lambda: _FakeBackend(False))
    assert hc._session_exists() is False
