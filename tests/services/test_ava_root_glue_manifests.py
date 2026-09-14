"""services.ava_root_glue.manifests: roster -> K2 manifest generation."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

# The capability constants are typed frozenset[MachineRole]; capability values
# are irrelevant to the roster-driven assertions here (same precedent as
# tests/cli/test_cluster_health.py).
from ops.service_spec import _AGENT_RUNNER, _BOTH, _GATEWAY, ServiceSpec
from services.ava_root.manifest import ManifestError, load_manifests
from services.ava_root_glue import manifests as gen
from shared.machine import MachineRole

_REPO = Path("/checkout/repo")


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
    assert gen.build_units(capabilities=["observability-station"], repo_root=_REPO) == []


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


def test_watchdogs_are_absorbed_not_emitted() -> None:
    specs = [
        _spec(
            "gateway-watchdog",
            _GATEWAY,
            ".venv/bin/python -m services.watchdog.daemon --role gateway",
        ),
        _spec("svc", _GATEWAY, ".venv/bin/python -m x"),
    ]
    units = gen.build_units(specs, capabilities=["gateway"], repo_root=_REPO)
    assert [u["id"] for u in units] == ["svc"]


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


def test_generated_manifest_is_consumed_by_load_manifests(tmp_path: Path) -> None:
    path = gen.generate(
        tmp_path / "units.json", capabilities=["gateway", "agent-runner"], repo_root=_REPO
    )
    registry = load_manifests(path)
    expected = gen.build_units(capabilities=["gateway", "agent-runner"], repo_root=_REPO)
    assert [manifest.id for manifest in registry.units] == [u["id"] for u in expected]
    first = registry.units[0]
    assert first.id == "gateway"
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
