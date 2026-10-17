"""PgBouncer probes whose contract is independent of bring-up sequencing."""

from __future__ import annotations

import pytest

from base.cluster import port_preflight
from base.cluster.dataplane import pooler as base_pooler
from base.cluster.machine import MachineIdentity


@pytest.fixture
def probe_machine() -> MachineIdentity:
    return MachineIdentity(
        name=lambda: "pooler-probe",
        role=lambda: frozenset({"gateway"}),
        host=lambda: "10.0.0.5",
        description=lambda: None,
    )


def _fail_admin_dial(
    _listen_port: int,
    _role: str,
    host: str = "127.0.0.1",
) -> bool:
    pytest.fail(f"public probe must not make a network dial to {host}")


def test_public_probe_is_a_noop_for_loopback_only_bind(
    monkeypatch: pytest.MonkeyPatch,
    probe_machine: MachineIdentity,
) -> None:
    """A loopback-only pooler has no public listener, so the fast path calls nothing."""

    def loopback(*, machine: MachineIdentity) -> list[str]:
        assert machine is probe_machine
        return ["127.0.0.1"]

    monkeypatch.setattr(port_preflight, "bind_addrs", loopback)

    def _fail_host_lookup() -> str:
        pytest.fail("loopback-only probe must not resolve the reachable host")

    def _fail_listener_scan(_port: int) -> set[str]:
        pytest.fail("loopback-only probe must not inspect listeners")

    monkeypatch.setattr(probe_machine, "host", _fail_host_lookup)
    monkeypatch.setattr(port_preflight, "listener_addrs", _fail_listener_scan)
    monkeypatch.setattr(base_pooler, "admin_reachable", _fail_admin_dial)

    assert (
        base_pooler.pgbouncer_public_listener_reachable(6433, "meiva_main", machine=probe_machine)
        is True
    )


def test_public_probe_accepts_the_exact_reachable_host_in_the_socket_table(
    monkeypatch: pytest.MonkeyPatch,
    probe_machine: MachineIdentity,
) -> None:
    """The configured address itself proves the public listener is bound."""
    scanned: list[int] = []

    def _listener_addrs(port: int) -> set[str]:
        scanned.append(port)
        return {"127.0.0.1", "10.0.0.5"}

    def private_bind(*, machine: MachineIdentity) -> list[str]:
        assert machine is probe_machine
        return ["127.0.0.1", "10.0.0.5"]

    monkeypatch.setattr(port_preflight, "bind_addrs", private_bind)
    monkeypatch.setattr(port_preflight, "listener_addrs", _listener_addrs)
    monkeypatch.setattr(base_pooler, "admin_reachable", _fail_admin_dial)

    assert (
        base_pooler.pgbouncer_public_listener_reachable(6433, "meiva_main", machine=probe_machine)
        is True
    )
    assert scanned == [6433]


@pytest.mark.parametrize(
    ("addrs", "expected"),
    [
        ({"127.0.0.1"}, False),
        ({"0.0.0.0"}, True),  # noqa: S104 — socket-table wildcard fixture
        ({"::"}, True),
        ({"*"}, True),
        (set[str](), False),
    ],
)
def test_public_probe_interprets_socket_table_bindings(
    monkeypatch: pytest.MonkeyPatch,
    addrs: set[str],
    expected: bool,
    probe_machine: MachineIdentity,
) -> None:
    """Only the reachable address or a wildcard covers the public front door;
    loopback-only and unknown (empty) results remain degraded."""

    def _listener_addrs(_port: int) -> set[str]:
        return addrs

    def private_bind(*, machine: MachineIdentity) -> list[str]:
        assert machine is probe_machine
        return ["127.0.0.1", "10.0.0.5"]

    monkeypatch.setattr(port_preflight, "bind_addrs", private_bind)
    monkeypatch.setattr(port_preflight, "listener_addrs", _listener_addrs)
    monkeypatch.setattr(base_pooler, "admin_reachable", _fail_admin_dial)

    assert (
        base_pooler.pgbouncer_public_listener_reachable(6433, "meiva_main", machine=probe_machine)
        is expected
    )
