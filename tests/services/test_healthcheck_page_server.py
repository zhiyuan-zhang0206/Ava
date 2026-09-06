"""`services.healthchecks.page_server` respawn-session regression guard.

Task #1291: the respawn session name must match ``ServiceSpec.session``
("page-server", kebab-case) — the module name ("page_server") differs, and a
respawn under the module name writes a session record the CLI cannot see or kill.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import services.healthchecks.page_server as hc
from ops.spec import build_services
from shared.daemon_health import DaemonProbe


def _spec_session() -> str:
    specs = build_services()
    return next(s.session for s in specs if s.session == "page-server")


def test_restart_respawns_the_spec_session_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """_restart_daemon respawns under the ServiceSpec.session name, not the module name."""
    calls: list[tuple[str, str, Path]] = []

    def fake_respawn_and_verify(
        session,
        cmd,
        repo,
        *,
        verify,
        **_kw,
    ) -> DaemonProbe:
        calls.append((session, cmd, repo))  # pyright: ignore[reportUnknownArgumentType]
        return verify()

    monkeypatch.setattr(hc, "respawn_and_verify", fake_respawn_and_verify)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "probe_daemon", lambda *_a, **_kw: DaemonProbe.up("stub"))  # pyright: ignore[reportUnknownArgumentType]

    result = hc._restart_daemon()
    assert result.alive is True
    expected_session = _spec_session()
    assert [(s, c) for s, c, _r in calls] == [
        (expected_session, ".venv/bin/python -m services.page_server.daemon")
    ]
    assert expected_session == "page-server"  # the CLI-facing session name


def test_spec_session_uses_kebab_case() -> None:
    specs = build_services()
    assert any(s.session == "page-server" for s in specs)
