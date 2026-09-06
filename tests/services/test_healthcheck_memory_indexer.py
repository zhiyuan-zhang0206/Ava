"""`services.healthchecks.memory_indexer` unit tests — probe delegation + restart shape.

memory_indexer moved from pidfile to HTTP /healthz (#254 pattern) so the watchdog
does not misjudge death during a tens-of-seconds cold-start embed and fall into a
spawn race. The probe itself (identity verification) is covered in
`tests/shared/test_daemon_health.py`; here we pin that this healthcheck asks for
the right daemon name and pidfile, and that the restart path reports the probe's
verdict rather than the spawn's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.healthchecks import memory_indexer as hc
from shared.config import settings
from shared.daemon_health import DaemonProbe


def test_probe_asks_for_this_daemons_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe is scoped to name=memory_indexer + this unit's pidfile — a probe
    that got either wrong would accept a different daemon as healthy."""
    seen: dict[str, object] = {}

    def fake_probe_daemon(name, url, *, pidfile, **_kw) -> DaemonProbe:
        seen.update(name=name, url=url, pidfile=pidfile)  # pyright: ignore[reportUnknownArgumentType]
        return DaemonProbe.up("stub")

    monkeypatch.setattr(hc, "probe_daemon", fake_probe_daemon)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._probe().alive is True
    assert seen["name"] == "memory_indexer"
    assert seen["pidfile"] == settings.services.memory_indexer_pidfile


def test_restart_respawns_session_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """_restart_daemon respawns `memory-indexer` and returns the probe's verdict."""
    calls: list[tuple[str, str, Path]] = []

    def fake_respawn_and_verify(session, cmd, repo, *, verify, **_kw) -> DaemonProbe:
        calls.append((session, cmd, repo))  # pyright: ignore[reportUnknownArgumentType]
        return verify()

    monkeypatch.setattr(hc, "respawn_and_verify", fake_respawn_and_verify)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "probe_daemon", lambda *_a, **_kw: DaemonProbe.up("stub"))  # pyright: ignore[reportUnknownArgumentType]

    result = hc._restart_daemon()
    assert result.alive is True
    assert [(s, c) for s, c, _r in calls] == [
        ("memory-indexer", ".venv/bin/python -m services.memory_indexer.daemon")
    ]


def test_restart_reports_failure_when_daemon_never_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """the spawn accepted the command but the daemon never came up → NOT a success."""
    monkeypatch.setattr(
        hc,
        "respawn_and_verify",
        lambda *_a, verify, **_kw: verify(),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        hc,
        "probe_daemon",
        lambda *_a, **_kw: DaemonProbe.down("healthz unreachable"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert hc._restart_daemon().alive is False
