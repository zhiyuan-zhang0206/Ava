"""The standard `/healthz` daemon entry: derivation, construction-time validation,
and the invariants every such roster entry must keep.

`healthz_daemon` is the one place a launch command, probe URL and identity probe are
derived from `(session, module)`. The invariants below are properties of the whole
roster, not of one service, so a new daemon added later is judged without a new test
line: its rendered unit, its names and its daemon-side literals either agree or one of
these names the entry that does not.
"""

from __future__ import annotations

import ast
import math
from functools import partial
from pathlib import Path
from typing import cast

import pytest

from base.cluster.machine import MachineRole
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.health import DEFAULT_PORTS, DaemonProbe
from base.daemon.tests.fakes import pin_endpoints
from ops import roster
from ops.roster.healthz import (
    daemon_identity,
    health_name_of,
    healthz_daemon,
    healthz_url,
)
from ops.roster.service_spec import ServiceSpec
from services.ava_root_glue import manifests as gen

_REPO = Path(__file__).resolve().parents[3]
_PYTHON_M = ".venv/bin/python -m "

_GATEWAY: frozenset[MachineRole] = frozenset({"gateway"})
_RUNNER: frozenset[MachineRole] = frozenset({"agent-runner"})
_BOTH: frozenset[MachineRole] = _GATEWAY | _RUNNER


def _healthz_specs() -> dict[str, ServiceSpec]:
    return {s.session: s for s in roster.build_services() if s.health_name is not None}


def _module(spec: ServiceSpec) -> str:
    assert spec.cmd.startswith(_PYTHON_M), spec.cmd
    return spec.cmd.removeprefix(_PYTHON_M)


def _daemon_probe(spec: ServiceSpec) -> partial[DaemonProbe]:
    """The daemon probe inside the root-ownership wrapper `build_services` adds."""
    outer = cast("partial[DaemonProbe]", spec.identity_probe)
    return cast("partial[DaemonProbe]", outer.args[2])


# ── the factory ──


def test_the_factory_derives_command_url_probe_and_health_name() -> None:
    spec = healthz_daemon(
        "delivery-watchdog", module="services.x.daemon", capabilities=_GATEWAY, requires_db=True
    )

    assert spec.session == "delivery-watchdog"
    assert spec.health_name == "delivery_watchdog"
    assert spec.cmd == ".venv/bin/python -m services.x.daemon"
    assert (
        spec.curl_url
        == f"http://localhost:{ServiceEndpoints.from_settings().of('delivery_watchdog').health_port}/healthz"
    )
    probe = cast("partial[DaemonProbe]", spec.identity_probe)
    assert probe.func.__name__ == "probe_daemon"
    assert probe.args == ("delivery_watchdog", spec.curl_url)
    assert probe.keywords == {
        "pidfile": ServiceEndpoints.from_settings().of("delivery_watchdog").pidfile
    }


def test_the_factory_passes_the_optional_declarations_through() -> None:
    def gate() -> str | None:
        return None

    spec = healthz_daemon(
        "labeler",
        module="services.labeler.daemon",
        capabilities=_GATEWAY,
        requires_db=False,
        gate=gate,
        profile="agent",
        no_profile_marker=True,
        db_access="gateway",
        stop_ceiling_s=12.5,
    )

    assert (spec.gate, spec.profile, spec.no_profile_marker) == (gate, "agent", True)
    assert (spec.db_access, spec.stop_ceiling_s, spec.requires_db) == ("gateway", 12.5, False)


def test_the_url_follows_the_endpoint_table_resolved_when_the_roster_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_health_port(_name: str) -> int:
        return 18111

    pin_endpoints(monkeypatch, port=_fake_health_port)
    assert healthz_url("im_bridge") == "http://localhost:18111/healthz"
    spec = healthz_daemon("im-bridge", module="a.b", capabilities=_GATEWAY, requires_db=True)
    assert spec.curl_url == "http://localhost:18111/healthz"


@pytest.mark.parametrize(
    "session", ["Heartbeat", "heart_beat", "heart beat", "-heartbeat", "", "1a"]
)
def test_a_session_that_is_not_lowercase_kebab_is_refused(session: str) -> None:
    with pytest.raises(ValueError, match="kebab"):
        health_name_of(session)


@pytest.mark.parametrize(
    "module", ["", "services/x/daemon", "services..x", "a b", "-m evil", "a.b;c"]
)
def test_a_module_that_is_not_a_dotted_path_is_refused(module: str) -> None:
    with pytest.raises(ValueError, match="dotted module path"):
        healthz_daemon("heartbeat", module=module, capabilities=_GATEWAY, requires_db=True)


def test_a_service_without_a_port_slot_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="no port slot") as raised:
        healthz_daemon("not-a-daemon", module="a.b", capabilities=_GATEWAY, requires_db=True)
    assert "not_a_daemon" in str(raised.value)


# ── construction-time validation of every ServiceSpec ──


def _spec(**fields: object) -> ServiceSpec:
    base: dict[str, object] = {
        "session": "x",
        "cmd": "x",
        "capabilities": _GATEWAY,
        "requires_db": False,
    }
    return ServiceSpec(**{**base, **fields})  # pyright: ignore[reportArgumentType]


def test_a_well_formed_spec_constructs() -> None:
    assert _spec().session == "x"
    assert _spec(curl_url="http://localhost:1/", stop_ceiling_s=0.5).stop_ceiling_s == 0.5
    assert _spec(capabilities=_BOTH, requires_db=True, db_access="gateway").db_access == "gateway"
    assert _spec(tcp_port=65535).tcp_port == 65535
    assert _spec(curl_url="http://localhost:1/__ava/healthz", home_healthz=True).home_healthz


_MALFORMED: list[tuple[dict[str, object], str]] = [
    ({"capabilities": frozenset()}, "no capabilities"),
    ({"capabilities": frozenset({"gateway", "mainframe"})}, "unknown capabilities"),
    ({"curl_url": "http://localhost:1/", "tcp_port": 1}, "both curl_url and tcp_port"),
    ({"tcp_port": 0}, "invalid tcp_port"),
    ({"tcp_port": 65536}, "invalid tcp_port"),
    ({"stop_ceiling_s": 0}, "positive finite"),
    ({"stop_ceiling_s": -1.0}, "positive finite"),
    ({"stop_ceiling_s": math.inf}, "positive finite"),
    ({"stop_ceiling_s": math.nan}, "positive finite"),
    ({"health_name": "no_such_daemon", "curl_url": "http://localhost:1/healthz"}, "no port"),
    (
        {"health_name": "heartbeat", "curl_url": "http://localhost:1/api/health"},
        "serves /healthz",
    ),
    ({"health_name": "heartbeat"}, "serves /healthz"),
    ({"home_healthz": True}, "declares home_healthz"),
    ({"home_healthz": True, "curl_url": "http://localhost:1/api/health"}, "declares home_healthz"),
    ({"capabilities": _BOTH, "requires_db": True}, "declares no db_access"),
]


@pytest.mark.parametrize(("fields", "message"), _MALFORMED)
def test_a_malformed_spec_fails_where_it_is_written(
    fields: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _spec(**fields)


# ── invariants of the whole roster ──


def test_the_roster_has_standard_daemons_and_each_one_has_a_port_slot() -> None:
    specs = _healthz_specs()
    assert len(specs) >= 11
    for spec in specs.values():
        assert spec.health_name in DEFAULT_PORTS, spec.session


def test_every_standard_daemon_renders_as_exec_python_dash_m_its_module() -> None:
    """I4: the unit pid stays a direct child of root, and a declaration cannot smuggle
    environment, arguments, a stop window or sealed inputs into the rendered unit."""
    for spec in _healthz_specs().values():
        (unit,) = gen.build_units([spec], capabilities=spec.capabilities, repo_root=_REPO)
        assert unit["exec"] == [
            "/bin/sh",
            "-c",
            f"cd {_REPO} && exec {_PYTHON_M}{_module(spec)}",
        ], spec.session
        assert unit["inputs"] == [], spec.session
        assert "stop_timeout_s" not in unit, spec.session
        assert (unit["id"], unit["restart"], unit["attach"]) == (spec.session, "always", "root")


def test_names_are_unique_across_the_whole_roster() -> None:
    """I5: two entries sharing a session, a health name, a URL or a pidfile would fight
    over one unit, one port or one pid record, and the second would never show up."""
    specs = roster.build_services()
    sessions = [s.session for s in specs]
    assert len(set(sessions)) == len(sessions), sorted(sessions)

    healthz = [s for s in specs if s.health_name is not None]
    for key, values in {
        "health_name": [s.health_name for s in healthz],
        "curl_url": [s.curl_url for s in healthz],
        "pidfile": [_daemon_probe(s).keywords["pidfile"] for s in healthz],
        "module": [_module(s) for s in healthz],
    }.items():
        assert len(set(values)) == len(values), f"duplicate {key}: {sorted(map(str, values))}"


def test_a_healthz_endpoint_is_declared_not_left_to_its_url() -> None:
    """Every roster entry whose readiness endpoint is a `/healthz` says what kind: a
    standard daemon (`health_name`) or an endpoint that names its home (`home_healthz`).
    `ava start` decides which ports it can check for a foreign occupant from that
    declaration, so a new `/healthz` entry cannot be silently in or out of the check."""
    for spec in roster.build_services():
        serves_healthz = spec.curl_url is not None and spec.curl_url.endswith("/healthz")
        declared = spec.health_name is not None or spec.home_healthz
        assert serves_healthz == declared, spec.session
    assert {s.session for s in roster.build_services() if s.home_healthz} == {"gate"}


def test_a_standard_daemons_session_derives_its_health_name() -> None:
    for session, spec in _healthz_specs().items():
        assert spec.health_name == health_name_of(session)
        assert spec.curl_url == healthz_url(health_name_of(session))


# ── the daemon side agrees with the roster ──

# The daemon-side calls that name the daemon. A mismatch is silent until boot (a name with
# no port slot) or forever (the probe reads another `name` out of /healthz and the port
# reads as taken), and a roster rename cannot see these literals.
_HEALTH_NAME_CALLS = ("start_health_server", "init_gateway_process", "install_graceful_shutdown")
_MODULE_CALLS = ("acquire_pidfile", "pidfile_holds_daemon")


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[node.targets[0].id] = node.value.value
    return found


def _literal(node: ast.expr, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _daemon_literals(source: str) -> dict[str, list[str | None]]:
    tree = ast.parse(source)
    constants = _module_constants(tree)
    found: dict[str, list[str | None]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _call_name(node)
        if callee in _HEALTH_NAME_CALLS:
            keyword = {k.arg: k.value for k in node.keywords}
            argument = node.args[0] if node.args else keyword.get("name") or keyword.get("label")
            found.setdefault(callee, []).append(_literal(argument, constants) if argument else None)
        elif callee in _MODULE_CALLS and len(node.args) >= 2:
            found.setdefault(callee, []).append(_literal(node.args[1], constants))
    return found


def test_every_standard_daemon_names_itself_as_the_roster_does() -> None:
    """The names the daemon passes to its health server, log init, shutdown label and
    pidfile guard are the roster's derived names, and the module it guards is the one
    `python -m` runs."""
    for spec in _healthz_specs().values():
        module = _module(spec)
        source = (_REPO / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8")
        found = _daemon_literals(source)
        for callee in _HEALTH_NAME_CALLS:
            assert found.get(callee), f"{module}: no {callee}(...) with a literal name"
            assert set(found[callee]) == {spec.health_name}, (module, callee, found[callee])
        for callee in _MODULE_CALLS:
            assert found.get(callee), f"{module}: no {callee}(path, module) with a literal"
            assert set(found[callee]) == {module}, (module, callee, found[callee])


def test_the_literal_finder_sees_a_mismatch() -> None:
    """The check above is only worth having if it can fail."""
    source = (
        "_M = 'pkg.daemon'\n"
        "async def run():\n"
        "    await start_health_server('beat')\n"
        "def main():\n"
        "    init_gateway_process(name='beat-x')\n"
        "    install_graceful_shutdown('b')\n"
        "    acquire_pidfile(P, _M)\n"
    )
    assert _daemon_literals(source) == {
        "start_health_server": ["beat"],
        "init_gateway_process": ["beat-x"],
        "install_graceful_shutdown": ["b"],
        "acquire_pidfile": ["pkg.daemon"],
    }


def test_the_public_probe_builder_is_still_the_roster_door() -> None:
    """Plugin services import `daemon_identity` from `ops.roster`; it must stay there."""
    assert roster.daemon_identity is daemon_identity
    assert roster.healthz_daemon is healthz_daemon
