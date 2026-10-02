"""Agent-profile root manifests bind the runner credential to that child only.

This is the remote-managed gateway plane's provider projection: no write
generation exists there, so only the agent-profile child receives the
provider runner login. A pure agent-runner delivers its installed unit
capability instead (base/cluster/authority/tests/test_unit_capability.py).
"""

from pathlib import Path

import pytest

from cli.commands.lifecycle import root_driver
from ops.roster.service_spec import _AGENT_RUNNER, ServiceSpec
from services.ava_root.manifest import load_manifests
from services.ava_root_glue.manifests import generate


def test_root_manifest_projects_runner_url_for_agent_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = ServiceSpec(
        session="agent-host",
        cmd=".venv/bin/python -m services.agent_host.daemon",
        capabilities=_AGENT_RUNNER,
        requires_db=True,
        profile="agent",
    )
    ops = ServiceSpec(
        session="ops",
        cmd=".venv/bin/python -m services.agent_ops",
        capabilities=_AGENT_RUNNER,
        requires_db=True,
    )
    projected = "postgresql://ava_runner:fixture@127.0.0.1:1/ava"

    def _fake_projection() -> str:
        return projected

    from base.config import settings

    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", lambda: True)
    monkeypatch.setattr(type(settings.data_plane), "is_remote", property(lambda _self: True))
    monkeypatch.setattr(root_driver, "runner_db_url_projection", _fake_projection)
    environments = {spec.session: root_driver._service_extra_env(spec) for spec in (agent, ops)}
    path = generate(
        tmp_path / "manifest.json",
        capabilities=["agent-runner"],
        repo_root=tmp_path,
        specs=(agent, ops),
        environments=environments,
    )
    units = {unit.id: unit for unit in load_manifests(path).units}
    assert dict(units["agent-host"].env) == {
        "AVA_PROCESS_PROFILE": "agent",
        "AVA_DB_URL": projected,
    }
    assert "AVA_DB_URL" not in dict(units["ops"].env)
