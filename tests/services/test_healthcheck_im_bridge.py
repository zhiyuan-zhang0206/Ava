"""`services.healthchecks.im_bridge` respawn-session regression guard.

Task #1291: the respawn session name must match ``ServiceSpec.session``
("im-bridge", kebab-case) — the module name ("im_bridge") differs, and a
respawn under the module name writes a session record the CLI (`ava status`
/ `ava stop` / `ava restart`) cannot see or kill.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import services.healthchecks.im_bridge as hc
from ops.spec import build_services
from shared.daemon_health import DaemonProbe


def _spec_session() -> str:
    specs = build_services()
    return next(s.session for s in specs if s.session == "im-bridge")


def test_restart_respawns_the_spec_session_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """_restart_daemon respawns under the ServiceSpec.session name, not the module name."""
    calls: list[tuple[str, str, Path]] = []

    def fake_respawn_and_verify(  # pyright: ignore[reportUnknownParameterType]
        session,
        cmd,
        repo,
        *,
        verify,
        **_kw,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    ) -> DaemonProbe:
        calls.append((session, cmd, repo))  # pyright: ignore[reportUnknownArgumentType]
        return verify()  # pyright: ignore[reportUnknownVariableType]

    monkeypatch.setattr(hc, "respawn_and_verify", fake_respawn_and_verify)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "probe_daemon", lambda *_a, **_kw: DaemonProbe.up("stub"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    result = hc._restart_daemon()
    assert result.alive is True
    expected_session = _spec_session()
    assert [(s, c) for s, c, _r in calls] == [
        (expected_session, ".venv/bin/python -m services.im_bridge.daemon")
    ]
    assert expected_session == "im-bridge"  # the CLI-facing session name


def test_spec_session_uses_kebab_case() -> None:
    """Guard the spec itself: im_bridge is the only service whose module name
    differs from its session name, so the respawn-name contract is explicit."""
    specs = build_services()
    assert any(s.session == "im-bridge" for s in specs)
