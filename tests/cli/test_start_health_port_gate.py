"""`ava start` probes its daemon health ports before it binds them.

The defect these cover (issue #977): a health port was treated as a per-CLUSTER
fact, but the collision domain is one MACHINE's localhost namespace. Two
agent-runners of the SAME cluster on one machine — a WSL2 distro plus the native
Windows install, whose loopback WSL2 republishes into — were therefore handed
identical ports by construction. The Windows watchdog probed its own port, was
answered by the Linux unit's daemon, and logged 402 identity mismatches in an
afternoon, each ending "manual intervention needed".

No arithmetic scheme closes that: a later WSL2 install can bind anything.
Detection is the mechanism that does not require everyone to have agreed in
advance, so the start asks each port who is there while it is still cheap to stop.

What is asserted here is the discrimination, because a gate that refuses too
eagerly is worse than none:

- a port answered by ANOTHER unit's daemon stops the start, before anything is
  launched, naming the occupant and the remedy;
- this unit's own daemon (an idempotent restart), a stray of its own home, and a
  cold port all pass;
- only the ports `--health-port-base` can move are gated — the gateway, the
  browser and the frontend are somebody else's problem, and the browser
  deliberately tolerates another unit's Chrome.
"""

from __future__ import annotations

import pytest

import cli.commands._probe as _probe_commands
import cli.commands._root_driver as _root_driver_commands
import cli.commands.start as _start_commands
from cli.commands import start as start_mod
from cli.commands._probe import ReadinessWait
from cli.commands._root_driver import LaunchOutcome
from ops.service_spec import _AGENT_RUNNER, _GATEWAY, ServiceSpec
from shared.daemon_health import DaemonProbe
from tests.cli.test_start_readiness_gate import (
    _hermetic_start as _base_start,  # noqa: F401 — shared fixture  # pyright: ignore[reportUnusedImport] — pytest fixture import
)

# The gate IS the subject here, so stand the global autouse net down for this
# module (tests/conftest.py:_guard_health_port_gate reports every port free).
pytestmark = pytest.mark.real_health_port_gate


def _healthz_spec(service: str, port: int) -> ServiceSpec:
    """A daemon whose probe target is an Ava `/healthz` — i.e. one of the ports
    `--health-port-base` moves."""
    return ServiceSpec(
        session=service,
        cmd="x",
        capabilities=_AGENT_RUNNER,
        requires_db=True,
        curl_url=f"http://localhost:{port}/healthz",
    )


def _other_endpoint_spec(service: str, url: str) -> ServiceSpec:
    """A service whose port is NOT in the health-port block (the gateway's
    `/api/health`, the browser's CDP `/json/version`, the frontend)."""
    return ServiceSpec(
        session=service,
        cmd="x",
        capabilities=_GATEWAY,
        requires_db=False,
        curl_url=url,
    )


_FOREIGN = (
    "identity mismatch on http://localhost:8102/healthz: home='/home/ava/.ava' != "
    "'C:\\\\Users\\\\ava\\\\.ava' — another unit's daemon holds this port"
)


def _verdicts(monkeypatch: pytest.MonkeyPatch, by_session: dict[str, DaemonProbe]) -> None:
    """Pin `_probe_service` from a per-session `DaemonProbe`, through the same
    translation the real one performs — so what these tests exercise is the
    gate's reading of a verdict, not a hand-built `ServiceProbe`."""

    def _probe(spec: ServiceSpec) -> _probe_commands.ServiceProbe:
        probe = by_session[spec.session]
        return _probe_commands.ServiceProbe(
            probe.alive, "identity", "" if probe.alive else probe.detail, probe.terminal
        )

    monkeypatch.setattr(_probe_commands, "_probe_service", _probe)


# ─── which verdicts are conflicts ────────────────────────────────────────────


def test_a_foreign_units_daemon_on_a_health_port_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restarter = _healthz_spec("restarter", 8102)
    _verdicts(monkeypatch, {"restarter": DaemonProbe.port_taken(_FOREIGN)})

    occupied = _probe_commands._occupied_health_ports((restarter,))

    assert [o.spec.session for o in occupied] == ["restarter"]
    assert occupied[0].detail == _FOREIGN


def test_our_own_running_daemon_is_not_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The idempotent case. `ava start` over a healthy host re-probes every port
    it is about to use and finds its own daemons — a start that refused here
    would make restart impossible on exactly the hosts that are working."""
    restarter = _healthz_spec("restarter", 8102)
    _verdicts(monkeypatch, {"restarter": DaemonProbe.up("pid 4242")})

    assert _probe_commands._occupied_health_ports((restarter,)) == ()


def test_a_dead_or_cold_port_is_not_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOWN covers both a port nothing answers on and a stray of our OWN home
    (`probe_daemon` reaches the pid arm only after name and home matched). Both
    are cleared by the launch that follows — the kill-session a respawn does
    first is exactly the fix — so neither may stop the start."""
    restarter = _healthz_spec("restarter", 8102)
    ops = _healthz_spec("ops", 8106)
    _verdicts(
        monkeypatch,
        {
            "restarter": DaemonProbe.down("healthz unreachable: URLError: refused"),
            "ops": DaemonProbe.down("healthz pid=9 != pidfile pid=8 — a stray process"),
        },
    )

    assert _probe_commands._occupied_health_ports((restarter, ops)) == ()


def test_only_the_ports_a_health_port_base_can_move_are_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal verdict on the gateway or the browser is not this gate's
    business, and folding them in would be a regression: the browser healthcheck
    deliberately tolerates another unit's Chrome on the CDP port, so a start that
    refused would leave a headed box unable to come up at all. Neither is a port
    `--health-port-base` moves either, so the remedy this gate prints would be
    wrong advice."""
    gateway = _other_endpoint_spec("gateway", "http://localhost:8000/api/health")
    browser = _other_endpoint_spec("browser", "http://localhost:9222/json/version")
    restarter = _healthz_spec("restarter", 8102)
    _verdicts(
        monkeypatch,
        {
            "gateway": DaemonProbe.port_taken("another unit's gateway"),
            "browser": DaemonProbe.port_taken("another unit's Chrome"),
            "restarter": DaemonProbe.up("pid 1"),
        },
    )

    assert _probe_commands._occupied_health_ports((gateway, browser, restarter)) == ()


def test_every_conflicting_port_is_reported_not_just_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator moving a unit needs the whole overlap in one pass: reporting
    only the first port turns one fix into a sequence of restarts, each revealing
    the next collision."""
    specs = (_healthz_spec("restarter", 8102), _healthz_spec("ops", 8106))
    _verdicts(
        monkeypatch,
        {"restarter": DaemonProbe.port_taken("a"), "ops": DaemonProbe.port_taken("b")},
    )

    assert [o.spec.session for o in _probe_commands._occupied_health_ports(specs)] == [
        "restarter",
        "ops",
    ]


# ─── what the start does with a conflict ─────────────────────────────────────


_REAL_GATE = start_mod._refuse_occupied_health_ports


@pytest.fixture
def _hermetic_start(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    request.getfixturevalue("_base_start")
    monkeypatch.setattr(start_mod, "_refuse_occupied_health_ports", _REAL_GATE)
    monkeypatch.setattr(
        _root_driver_commands,
        "_wait_for_service_tree",
        lambda *_a, **_kw: ReadinessWait((), 0.0, sessions_gone=False),  # pyright: ignore[reportUnknownArgumentType] — variadic readiness test double
    )


def _roster(monkeypatch: pytest.MonkeyPatch, specs: tuple[ServiceSpec, ...]) -> list[str]:
    launched: list[str] = []
    monkeypatch.setattr(
        "cli.commands._repo._services_for_roles_annotated",
        lambda _r: tuple((s, None) for s in specs),  # pyright: ignore[reportUnknownArgumentType] — roster test double
    )
    monkeypatch.setattr(
        _root_driver_commands,
        "_start_roster",
        lambda _roles, skip: tuple(s for s in specs if s.session not in skip),  # pyright: ignore[reportUnknownArgumentType] — roster test double
    )

    def launch(roster: tuple[ServiceSpec, ...], *_a: object, **_kw: object) -> LaunchOutcome:
        launched.extend(s.session for s in roster)
        return LaunchOutcome(roster, ())

    monkeypatch.setattr(_root_driver_commands, "_launch_service_tree", launch)
    return launched


def test_start_refuses_and_launches_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _hermetic_start
) -> None:
    launched = _roster(monkeypatch, (_healthz_spec("ops", 8106),))
    _verdicts(monkeypatch, {"ops": DaemonProbe.port_taken(_FOREIGN)})
    assert _start_commands.cmd_start() == 1
    assert launched == []
    message = "".join(capsys.readouterr())
    assert "/home/ava/.ava" in message
    assert "--health-port-base" in message
    assert "--disable-service ops" in message


def test_a_clear_roster_starts_normally(monkeypatch: pytest.MonkeyPatch, _hermetic_start) -> None:
    launched = _roster(monkeypatch, (_healthz_spec("ops", 8106),))
    _verdicts(monkeypatch, {"ops": DaemonProbe.down("cold")})
    assert _start_commands.cmd_start() == 0
    assert launched == ["ops"]


def test_a_disabled_service_cannot_block_start(
    monkeypatch: pytest.MonkeyPatch, _hermetic_start
) -> None:
    launched = _roster(monkeypatch, (_healthz_spec("labeler", 8103), _healthz_spec("ops", 8106)))
    _verdicts(
        monkeypatch, {"labeler": DaemonProbe.port_taken(_FOREIGN), "ops": DaemonProbe.down("cold")}
    )
    assert _start_commands.cmd_start(disabled_services=("labeler",)) == 0
    assert launched == ["ops"]
