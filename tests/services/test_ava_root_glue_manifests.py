"""services.ava_root_glue.manifests: roster -> K2 manifest generation."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from base.cluster.machine import MachineRole
from base.config import settings

# The capability constants are typed frozenset[MachineRole]; capability values
# are irrelevant to the roster-driven assertions here (same precedent as
# cli/commands/cluster/tests/test_cluster_health.py).
from ops.roster.service_spec import _AGENT_RUNNER, _BOTH, _GATEWAY, ServiceSpec
from services.ava_root.manifest import ManifestError, load_manifests
from services.ava_root.supervisor import SupervisorConfig
from services.ava_root_glue import manifests as gen

_REPO = Path("/checkout/repo")


@pytest.fixture(autouse=True)
def _declared_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the real roster private configuration, without a native backend install."""
    from base.telemetry.lgtm_local import BACKENDS, service_input_paths
    from ops import roster

    monkeypatch.setattr(roster, "ava_home", lambda: tmp_path)
    collector = tmp_path / "collector.yaml"
    collector.write_text("receivers: {}")
    monkeypatch.setattr(roster, "otel_collector_config", lambda: collector)
    for name in BACKENDS:
        for path in service_input_paths(tmp_path, name):
            if path.name == "config":
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test")


def _spec(session: str, capabilities: frozenset[MachineRole], cmd: str) -> ServiceSpec:
    return ServiceSpec(
        session=session,
        cmd=cmd,
        capabilities=capabilities,
        requires_db=True,
    )


def test_real_roster_gateway_subset() -> None:
    units = gen.build_units(capabilities=["gateway"], repo_root=_REPO)
    ids = [u["id"] for u in units]
    assert "gateway" in ids
    assert "gate" in ids
    assert "frontend" in ids
    assert "agent-host" not in ids
    # The two watchdogs are absorbed into the root's built-in health path.
    assert "gateway-watchdog" not in ids
    assert "agent-runner-watchdog" not in ids
    for unit in units:
        assert unit["restart"] == "always"
        assert unit["attach"] == "root"
        exec_argv = cast("list[str]", unit["exec"])
        assert exec_argv[0] == "/bin/sh"
        assert exec_argv[1] == "-c"
        assert exec_argv[2].startswith("cd /checkout/repo && ")


def test_agent_runner_subset_and_otel() -> None:
    runner = {u["id"] for u in gen.build_units(capabilities=["agent-runner"], repo_root=_REPO)}
    assert {"agent-host", "page-server", "ops"} <= runner
    assert "gateway" not in runner
    assert "otel-collector" in runner  # intersects on either side of the pair
    assert {
        u["id"] for u in gen.build_units(capabilities=["observability-station"], repo_root=_REPO)
    } == {"loki", "prometheus", "grafana"}


def test_key_units_exec_restart_attach() -> None:
    units = {
        u["id"]: u
        for u in gen.build_units(capabilities=["gateway", "agent-runner"], repo_root=_REPO)
    }
    assert units["gateway"]["exec"] == [
        "/bin/sh",
        "-c",
        "cd /checkout/repo && exec .venv/bin/python -m gateway",
    ]
    assert units["agent-host"]["exec"] == [
        "/bin/sh",
        "-c",
        "cd /checkout/repo && exec .venv/bin/python -m services.agent_host.daemon",
    ]
    assert units["page-server"]["restart"] == "always"
    assert units["page-server"]["attach"] == "root"
    frontend_cmd = cast("list[str]", units["frontend"]["exec"])[2]
    assert "&&" in frontend_cmd  # a compound command keeps its own shape
    assert frontend_cmd.count("exec ") == 1  # only its own final exec, none prefixed


def test_capability_tokens_validated() -> None:
    with pytest.raises(ManifestError, match="unknown capability"):
        gen.build_units(capabilities=["nope"], repo_root=_REPO)
    with pytest.raises(ManifestError, match="must not be empty"):
        gen.build_units(capabilities=[], repo_root=_REPO)


def test_specs_order_and_subset_are_preserved() -> None:
    specs = [
        _spec("svc-b", _GATEWAY, ".venv/bin/python -m b"),
        _spec("svc-a", _AGENT_RUNNER, ".venv/bin/python -m a"),
        _spec("svc-c", _BOTH, ".venv/bin/python -m c"),
    ]
    units = gen.build_units(specs, capabilities=["agent-runner"], repo_root=_REPO)
    assert [u["id"] for u in units] == ["svc-a", "svc-c"]


def test_exec_prefix_only_for_simple_commands() -> None:
    simple = gen._exec_argv(".venv/bin/python -m gateway", _REPO)
    assert simple == ["/bin/sh", "-c", "cd /checkout/repo && exec .venv/bin/python -m gateway"]
    compound = gen._exec_argv("cd ui/web && npm run start", _REPO)
    assert compound[2] == "cd /checkout/repo && cd ui/web && npm run start"
    assignment = gen._exec_argv("FOO=1 .venv/bin/python -m x", _REPO)
    assert assignment[2] == "cd /checkout/repo && FOO=1 .venv/bin/python -m x"
    quoted = gen._exec_argv(".venv/bin/python -c 'print(1)'", _REPO)
    assert "exec " not in quoted[2]
    already_exec = gen._exec_argv("exec foo", _REPO)
    assert already_exec[2] == "cd /checkout/repo && exec foo"
    assert "exec exec" not in already_exec[2]


def test_generated_manifest_is_consumed_by_load_manifests(tmp_path: Path) -> None:
    path = gen.generate(
        tmp_path / "units.json", capabilities=["gateway", "agent-runner"], repo_root=_REPO
    )
    registry = load_manifests(path)
    expected = gen.build_units(capabilities=["gateway", "agent-runner"], repo_root=_REPO)
    assert [manifest.id for manifest in registry.units] == [u["id"] for u in expected]
    first = registry.units[0]
    assert first.id == "gate"
    assert first.exec[0] == "/bin/sh"
    assert first.attach == "root"


def test_session_host_attach_table_matches_the_g6b_ruling() -> None:
    assert gen.SESSION_HOST_ATTACH == {
        "agent-shell": "agent-host",
        "watcher-session": "agent-host",
        "page-server-instance": "page-server",
        "schedule-runner": "gateway",
        "orchestration-session": "ops",
        "exec-child": "agent-host",
    }


def _real_units() -> dict[str, dict[str, object]]:
    units = gen.build_units(capabilities=["gateway", "agent-runner"], repo_root=_REPO)
    return {cast("str", unit["id"]): unit for unit in units}


def test_declared_shutdown_ceiling_becomes_root_window_and_undeclared_keeps_the_default() -> None:
    declared = ServiceSpec(
        session="svc-a",
        cmd=".venv/bin/python -m a",
        capabilities=_GATEWAY,
        requires_db=False,
        stop_ceiling_s=30.0,
    )
    undeclared = _spec("svc-b", _GATEWAY, ".venv/bin/python -m b")
    units = {
        unit["id"]: unit
        for unit in gen.build_units(
            [declared, undeclared], capabilities=["gateway"], repo_root=_REPO
        )
    }
    assert units["svc-a"]["stop_timeout_s"] == 30.0 + gen.STOP_MARGIN_S
    assert "stop_timeout_s" not in units["svc-b"]


def test_every_roster_unit_gets_a_window_strictly_above_its_declared_ceiling() -> None:
    """Root never waits for a unit less long than the unit's own cleanup may take."""
    from ops.roster import build_services

    default_window = SupervisorConfig().stop_timeout_s
    units = _real_units()
    for spec in build_services():
        if spec.session not in units:
            continue
        window = cast("float | None", units[spec.session].get("stop_timeout_s"))
        if spec.stop_ceiling_s is None:
            assert window is None, f"{spec.session}: a window without a declared ceiling"
            continue
        assert window is not None, f"{spec.session} declares a ceiling but got no window"
        assert window >= spec.stop_ceiling_s + gen.STOP_MARGIN_S
    # The two units whose own cleanup outlasts root's default window declare it.
    for session in ("gateway", "browser-mcp"):
        window = cast("float", units[session]["stop_timeout_s"])
        assert window > default_window, f"{session}: window {window} <= default {default_window}"


def test_gateway_window_follows_the_drain_budget_the_launch_hands_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One setting drives both uvicorn's drain and root's wait: no value can undercut."""
    from gateway import _server

    for drain in (30.0, 5.0, 120.0):
        monkeypatch.setattr(settings.gateway, "gateway_graceful_shutdown_timeout_seconds", drain)
        window = cast("float", _real_units()["gateway"]["stop_timeout_s"])
        launched = _server.serve_kwargs(host="127.0.0.1")["timeout_graceful_shutdown"]
        assert launched == drain
        assert window > launched + gen.STOP_MARGIN_S  # room left for the lifespan cleanup


def test_browser_mcp_window_covers_every_bounded_shutdown_step() -> None:
    from services.browser import shutdown_budget

    window = cast("float", _real_units()["browser-mcp"]["stop_timeout_s"])
    steps = shutdown_budget.SHUTDOWN_STEPS * shutdown_budget.SHUTDOWN_STEP_TIMEOUT_S
    assert window == steps + gen.STOP_MARGIN_S
