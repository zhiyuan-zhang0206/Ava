"""Which roster entries `ava start` checks for a foreign occupant before it launches.

The pre-bind occupancy check only works where the occupant answers an Ava `/healthz`
whose body names its home. That set used to be read off the URL (anything ending in
`/healthz`), which happened to include the gate; it is now read off declarations:
`health_name` for a standard daemon, `home_healthz` for the gate. These tests pin
that the set did not move, and that the gate is in it because it says so.
"""

from __future__ import annotations

from dataclasses import replace

from base.daemon.health import DaemonProbe
from cli.commands._probe import _binds_a_daemon_health_port, _occupied_health_ports
from ops import roster
from ops.roster.service_spec import ServiceSpec

_NOT_DAEMON_PORTS = ("gateway", "browser", "milvus", "frontend", "memory-search", "otel-collector")


def _by_session() -> dict[str, ServiceSpec]:
    return {s.session: s for s in roster.build_services()}


def _url_suffix_rule(spec: ServiceSpec) -> bool:
    """The predicate as it was: the URL says so."""
    return spec.curl_url is not None and spec.curl_url.endswith("/healthz")


def test_the_preflight_covers_exactly_the_entries_the_url_suffix_covered() -> None:
    for spec in roster.build_services():
        assert _binds_a_daemon_health_port(spec) == _url_suffix_rule(spec), spec.session


def test_the_covered_entries_are_the_standard_daemons_and_the_gate() -> None:
    specs = _by_session()
    covered = {s for s, spec in specs.items() if _binds_a_daemon_health_port(spec)}

    assert covered == {s for s, spec in specs.items() if spec.health_name is not None} | {"gate"}
    for session in _NOT_DAEMON_PORTS:
        assert not _binds_a_daemon_health_port(specs[session]), session


def test_the_gate_is_covered_because_it_declares_a_home_healthz() -> None:
    gate = _by_session()["gate"]
    assert gate.curl_url is not None and gate.curl_url.endswith("/__ava/healthz")
    assert gate.health_name is None
    assert gate.home_healthz is True

    # Withdraw the declaration and the URL alone no longer counts.
    assert not _binds_a_daemon_health_port(replace(gate, home_healthz=False))


def test_an_endpoint_that_only_looks_like_a_healthz_is_not_covered() -> None:
    lookalike = ServiceSpec(
        session="lookalike",
        cmd="x",
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        curl_url="http://localhost:1/healthz",
    )
    assert not _binds_a_daemon_health_port(lookalike)


def test_a_foreign_gate_on_the_entry_port_is_reported_before_the_launch() -> None:
    gate = _by_session()["gate"]
    foreign = replace(
        gate,
        identity_probe=lambda: DaemonProbe.port_taken("another unit's gate holds the entry port"),
    )

    (occupied,) = _occupied_health_ports((foreign,))

    assert occupied.spec.session == "gate"
    assert "another unit's gate" in occupied.detail


def test_a_gate_that_is_merely_down_does_not_stop_the_start() -> None:
    gate = _by_session()["gate"]
    down = replace(gate, identity_probe=lambda: DaemonProbe.down("nothing listening"))

    assert _occupied_health_ports((down,)) == ()
