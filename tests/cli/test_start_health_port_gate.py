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

import subprocess
from pathlib import Path

import pytest

import cli.commands as _cli
from cli.commands._repo import ServiceSpec
from ops.spec import _AGENT_RUNNER, _GATEWAY
from shared.daemon_health import DaemonProbe

# The gate IS the subject here, so stand the global autouse net down for this
# module (tests/conftest.py:_guard_health_port_gate reports every port free).
pytestmark = pytest.mark.real_health_port_gate


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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

    def _probe(spec: ServiceSpec) -> _cli.ServiceProbe:
        probe = by_session[spec.session]
        return _cli.ServiceProbe(
            probe.alive, "identity", "" if probe.alive else probe.detail, probe.terminal
        )

    monkeypatch.setattr(_cli, "_probe_service", _probe)


# ─── which verdicts are conflicts ────────────────────────────────────────────


def test_a_foreign_units_daemon_on_a_health_port_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restarter = _healthz_spec("restarter", 8102)
    _verdicts(monkeypatch, {"restarter": DaemonProbe.port_taken(_FOREIGN)})

    occupied = _cli._occupied_health_ports((restarter,))

    assert [o.spec.session for o in occupied] == ["restarter"]
    assert occupied[0].detail == _FOREIGN


def test_our_own_running_daemon_is_not_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The idempotent case. `ava start` over a healthy host re-probes every port
    it is about to use and finds its own daemons — a start that refused here
    would make restart impossible on exactly the hosts that are working."""
    restarter = _healthz_spec("restarter", 8102)
    _verdicts(monkeypatch, {"restarter": DaemonProbe.up("pid 4242")})

    assert _cli._occupied_health_ports((restarter,)) == ()


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

    assert _cli._occupied_health_ports((restarter, ops)) == ()


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

    assert _cli._occupied_health_ports((gateway, browser, restarter)) == ()


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

    assert [o.spec.session for o in _cli._occupied_health_ports(specs)] == ["restarter", "ops"]


# ─── what the start does with a conflict ─────────────────────────────────────


@pytest.fixture
def _hermetic_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reduce `_cmd_start_body` to the steps before the launch.

    Every precondition (setup collection, converge, the cluster's pg/redis,
    migrations, machine registration, the schema assertion) is stubbed to
    success, so what a non-zero rc can mean here is the gate and nothing else.
    """
    monkeypatch.setattr(
        _cli,
        "_collect_setup_values",
        lambda _a: (  # pyright: ignore[reportUnknownArgumentType]
            {
                "machine_name": "win",
                "machine_role": "agent-runner",
                "memory_remote": "git@github.com:test/AvaMemory.git",
                "gateway_url": "http://test-gateway:8000",
            },
            [],
        ),
    )
    monkeypatch.setattr(_cli, "converge_host", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_register_machine_or_die", lambda _r, _role: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_probe_gateway_or_die", lambda _url: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_assert_schema_current_or_die", lambda: 0)
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")

    from cli.commands import start as _start_mod

    monkeypatch.setattr(_start_mod, "cmd_migrations_apply", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _FakeResult(returncode=0))  # pyright: ignore[reportUnknownArgumentType]


def _roster(monkeypatch: pytest.MonkeyPatch, specs: tuple[ServiceSpec, ...]) -> None:
    from cli.commands import _session_lifecycle as _session_mod

    annotated = tuple((spec, None) for spec in specs)
    monkeypatch.setattr(_session_mod, "_services_for_roles_annotated", lambda _roles: annotated)  # pyright: ignore[reportUnknownArgumentType]


def test_start_refuses_and_launches_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys, _hermetic_start
) -> None:
    """The point of probing BEFORE binding: not one session is spawned.

    Launching first and reporting after is what the readiness gate already does,
    and it is the wrong shape here — the daemons would either die on 'address
    already in use' or, under a loopback relay, come up while the watchdog probes
    the other unit and reads green."""
    launched: list[str] = []
    monkeypatch.setattr(_cli, "_new_session", lambda s, *_a, **_kw: launched.append(s) is None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    _roster(monkeypatch, (_healthz_spec("restarter", 8102),))
    _verdicts(monkeypatch, {"restarter": DaemonProbe.port_taken(_FOREIGN)})

    rc = _cli.cmd_start()

    assert rc == 1
    assert launched == [], "the gate must run before any session is spawned"


def test_the_refusal_names_the_occupant_and_the_remedy(
    monkeypatch: pytest.MonkeyPatch, capsys, _hermetic_start
) -> None:
    """A refusal an operator cannot act on is a worse outage than the collision.

    The occupant's `$AVA_HOME` says WHICH unit to move, and `--health-port-base`
    is the only supported way to move it — the fix the incident needed and could
    not find, because the message it did get blamed the wrong layer ('another
    cluster's daemon' on a same-cluster neighbour).

    The refusal also has to name `--disable-service`. One occupied port stops the
    WHOLE start, gateway and frontend included, and there is no flag that waives
    the gate — so an operator who needs the rest of the unit up now has exactly
    one move, and leaving them to find it is how a refusal becomes the outage."""
    monkeypatch.setattr(_cli, "_new_session", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    _roster(monkeypatch, (_healthz_spec("restarter", 8102),))
    _verdicts(monkeypatch, {"restarter": DaemonProbe.port_taken(_FOREIGN)})

    _cli.cmd_start()

    combined = "".join(capsys.readouterr())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "ava-restarter" in combined
    assert "/home/ava/.ava" in combined, "the occupant's home is which unit to move"
    assert "--health-port-base" in combined
    assert "--disable-service restarter" in combined, "the stopgap, ready to paste"


def test_a_clear_roster_starts_normally(monkeypatch: pytest.MonkeyPatch, _hermetic_start) -> None:
    """The gate is invisible when nothing is in the way — no port this unit is
    about to bind is answered by anyone else, so the launch proceeds."""
    launched: list[str] = []
    monkeypatch.setattr(_cli, "_new_session", lambda s, *_a, **_kw: launched.append(s) is None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "cmd_status", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    _roster(monkeypatch, (_healthz_spec("restarter", 8102),))
    _verdicts(monkeypatch, {"restarter": DaemonProbe.down("nothing there")})

    rc = _cli.cmd_start()

    assert rc == 0
    assert launched == ["ava-restarter"]


def test_a_disabled_service_cannot_block_the_start(
    monkeypatch: pytest.MonkeyPatch, _hermetic_start
) -> None:
    """`--disable-service` removes a daemon from the roster, so its port is not one
    this unit is about to bind — and an occupant there is simply not this start's
    business. The gate reads the same roster the launch does for exactly this
    reason; a second, wider list would refuse over a port nobody was going to use."""
    monkeypatch.setattr(_cli, "_new_session", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "cmd_status", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    _roster(monkeypatch, (_healthz_spec("restarter", 8102), _healthz_spec("ops", 8106)))
    _verdicts(
        monkeypatch,
        {"restarter": DaemonProbe.port_taken(_FOREIGN), "ops": DaemonProbe.down("cold")},
    )

    assert _cli.cmd_start(disabled_services=("restarter",)) == 0


def test_disable_service_start_leaves_no_marker_in_the_session_home(
    monkeypatch: pytest.MonkeyPatch, _hermetic_start
) -> None:
    """`--disable-service` is durable operator intent (persist=True), so this
    start really writes the marker — the isolation must keep it OUT of the
    worker's shared session home, where it would silently disable the restarter
    for every later test that reads the marker (the `TestUnpauseLocalCluster`
    respawn tests — CI #1172/#1173, task #2177)."""
    from shared.config import settings

    marker = Path(settings.general.ava_home) / "disabled_services"
    marker.unlink(missing_ok=True)  # the suite home is freshly provisioned per worker
    monkeypatch.setattr(_cli, "_new_session", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "cmd_status", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    _roster(monkeypatch, (_healthz_spec("restarter", 8102),))
    _verdicts(monkeypatch, {"restarter": DaemonProbe.down("cold")})

    assert _cli.cmd_start(disabled_services=("restarter",)) == 0

    assert not marker.exists(), (
        "a start test must not write the durable --disable-service marker into the "
        "shared session home: with 'restarter' there, a later test calling the real "
        "unpause_local_cluster sees it durably disabled and neither respawns nor raises"
    )
