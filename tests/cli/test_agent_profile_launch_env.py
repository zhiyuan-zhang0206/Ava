"""Agent-profile root manifests bind the runner credential to that child only."""

from pathlib import Path

import pytest

from cli.commands import _root_driver
from ops.service_spec import _AGENT_RUNNER, ServiceSpec
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
    monkeypatch.setattr(_root_driver, "runner_db_url_projection", lambda _url: projected)
    environments = {spec.session: _root_driver._service_extra_env(spec) for spec in (agent, ops)}
    path = generate(
        tmp_path / "manifest.json",
        capabilities=["agent-runner"],
        repo_root=tmp_path,
        specs=(agent, ops),
        environments=environments,
    )
    units = {unit.id: unit for unit in load_manifests(path).units}
    assert dict(units["agent-host"].env) == {"AVA_PROCESS_PROFILE": "agent", "AVA_DB_URL": projected}
    assert "AVA_DB_URL" not in dict(units["ops"].env)
