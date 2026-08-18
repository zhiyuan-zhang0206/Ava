"""Infra wiring for per-session pty hosts — what makes shell persistence
structural: no pty service exists for any stop/update/watchdog path to kill,
and the one-time reap of the retired supervisor daemon is registered.

The behavioral end (a real session surviving its creators, host reparented to
init, crash sweep) is tests/shared/test_pty_sessions_cli.py; this file pins
the ABSENCE half — the roster, the stop scope, and the converge migration.
"""

from __future__ import annotations

import ops.spec as spec_mod
from cli.commands._converge import _RENAMED_AWAY_SERVICES


def test_roster_carries_no_pty_service() -> None:
    """No ServiceSpec hosts agent shells: sessions live in per-session
    detached hosts (shared/pty_sessions), so the roster — the single source
    every stop/start/watchdog scope derives from — must not name one. This is
    the structural fix for rollouts killing every shell (2026-08-12): a
    service that does not exist cannot be stopped, respawned, or force-killed
    with the shells as its children."""
    sessions = {s.session for s in spec_mod.build_services()}
    assert "pty-supervisor" not in sessions
    assert not any("pty" in s for s in sessions), sessions


def test_retired_supervisor_is_reaped_by_converge() -> None:
    """The one-way transition: converge's renamed-away reap must name the
    retired `pty-supervisor` service session so an updated host kills the old
    daemon (and with it the final pre-host-era shells — the one accepted
    loss) instead of stranding it as an unmanaged orphan."""
    assert "pty-supervisor" in _RENAMED_AWAY_SERVICES


def test_no_healthcheck_module_references_pty() -> None:
    """No spec points the watchdog at a pty healthcheck — nothing probes,
    nothing respawns, no respawn channel can take shells down."""
    for s in spec_mod.build_services():
        assert s.healthcheck_module is None or "pty" not in s.healthcheck_module, s.session
