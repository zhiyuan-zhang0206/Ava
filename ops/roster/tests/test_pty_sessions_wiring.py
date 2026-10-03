"""The pty-sessions roster entry: what makes shell persistence a service's job.

Every agent shell lives in one ordinary roster process per machine. The behavior
(a session outliving its clients, the closure, the sweep) is covered by
services/pty_sessions/tests; this file pins the declaration: its capabilities, that
it needs no database, its ownership probe, its gate, and the SIGTERM budget root
derives its stop window from.
"""

from __future__ import annotations

import pytest

from ops import roster, spec
from ops.roster import service_spec
from services.pty_sessions import shutdown_budget


def _spec() -> service_spec.ServiceSpec:
    return next(s for s in roster.build_services() if s.session == "pty-sessions")


def test_both_a_gateway_host_and_a_runner_host_run_the_service() -> None:
    declared = _spec()
    assert declared.capabilities == frozenset({"gateway", "agent-runner"})
    for role in ("gateway", "agent-runner"):
        started = {s.session for s in spec.services_for_capabilities(frozenset({role}))}
        assert "pty-sessions" in started, role


def test_the_service_needs_no_database_and_no_profile_marker() -> None:
    """A Unix socket and a ledger file are its whole data plane: a database outage must
    not hold it, and it carries no write generation."""
    declared = _spec()
    assert declared.requires_db is False
    assert service_spec.db_access(declared) is None
    assert service_spec.api_access(declared) is None
    assert service_spec.profile_marker(declared) is None


def test_the_ownership_probe_is_bound_to_the_roots_generation() -> None:
    declared = _spec()
    assert declared.identity_probe is not None
    assert declared.curl_url is None and declared.tcp_port is None, "a Unix endpoint binds its peer"
    assert declared.cmd == ".venv/bin/python -m services.pty_sessions.daemon"


def test_the_stop_window_covers_the_services_own_closure() -> None:
    assert _spec().stop_ceiling_s == shutdown_budget.SHUTDOWN_CEILING_S
    assert shutdown_budget.SHUTDOWN_CEILING_S > (
        shutdown_budget.SHUTDOWN_HANGUP_WAIT_S + shutdown_budget.SHUTDOWN_KILL_S
    )


@pytest.mark.parametrize(("sockets", "gated"), [(False, True), (True, False)])
def test_the_service_is_gated_out_where_there_are_no_unix_sockets(
    monkeypatch: pytest.MonkeyPatch, sockets: bool, gated: bool
) -> None:
    monkeypatch.setattr("ops.spec.unix_sockets_available", lambda: sockets)
    reason = {
        s.session: why
        for s, why in spec.services_for_capabilities_annotated(frozenset({"agent-runner"}))
    }["pty-sessions"]
    assert (reason is not None) is gated
    if gated:
        assert reason is not None and "POSIX-only" in reason
