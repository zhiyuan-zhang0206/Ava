from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import meiva.skills
from base.clock.tests.manual_timers import ManualTimers
from base.host.system.tests.fake_commands import fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.extensions import external_skills
from cli.commands.extensions.skills_sync import converge_skills
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

REPO = Path(__file__).resolve().parents[4]
GUIDE = REPO / "meiva_builtins" / "skills" / "platform" / "meiva-guide"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(GUIDE, repo / "meiva_builtins" / "skills" / "platform" / "meiva-guide")
    shutil.copytree(
        REPO / "meiva_builtins" / "skills" / "practice" / "meiva-workflow",
        repo / "meiva_builtins" / "skills" / "practice" / "meiva-workflow",
    )
    return repo


def test_converged_namespaces_load_real_task_instructions(
    db_url: str, tmp_path: Path, unit_home: Path
) -> None:
    repo = _repo(tmp_path)
    converge_skills(repo, unit_home)

    expected = {
        ("meiva_guide", "deploy"): "meiva init",
        ("meiva_guide", "operations"): "Alert review",
        ("meiva_guide", "modification_layers"): "L4",
        ("meiva_guide", "packages", "install"): "Find candidates",
        ("meiva_guide", "plugins", "develop"): "plugin.py",
        ("meiva_guide", "schedules"): "resumable",
        ("meiva_guide", "external_agents"): "Mode B",
        ("meiva_guide", "pages"): "user input",
        ("meiva_workflow", "capability_timescale"): "Gather evidence",
    }
    for segments, instruction in expected.items():
        skill = meiva.skills
        for segment in segments:
            skill = getattr(skill, segment)
        body = skill.__doc__
        assert isinstance(body, str)
        assert instruction in body

    for old in (
        "meiva_package_installer",
        "meiva_schedule_writer",
        "meiva_use_other_agents",
        "meiva_ui",
        "meiva_watcher",
        "meiva_ultra_speed",
    ):
        with pytest.raises(AttributeError):
            getattr(meiva.skills, old)


def _assert_portable_reading_links(target: Path) -> None:
    for document in target.rglob("*.md"):
        prose = re.sub(r"```.*?```|`[^`]*`", "", document.read_text(), flags=re.DOTALL)
        for link in re.findall(r"\]\(([^)]+)\)", prose):
            if "://" in link or link.startswith("#"):
                continue
            destination = (document.parent / link.split("#")[0]).resolve()
            assert destination.is_relative_to(target)
            assert destination.is_file()


@pytest.mark.parametrize("client", [".codex", ".claude"])
def test_external_guide_preserves_legacy_copies_and_publishes_nested_resources(
    tmp_path: Path,
    client: str,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    repo = _repo(tmp_path)
    host_home = tmp_path / "host"
    client_home = host_home / client
    client_home.mkdir(parents=True)
    meiva_home = tmp_path / "meiva-home"
    (meiva_home / "configs").mkdir(parents=True)
    ledger_root = meiva_home / "configs" / "external-agent-skills"
    ledger_root.mkdir()
    key = client.removeprefix(".")
    legacy_bytes = b'{"legacy": "private ownership evidence"}\n'
    for name, ledger in (
        ("operating-meiva-cluster", key),
        ("deploy-meiva-cluster", f"{key}-deploy-meiva-cluster"),
    ):
        legacy = client_home / "skills" / name
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("user customization\n")
        (ledger_root / f"{ledger}.json").write_bytes(legacy_bytes)

    context = ConvergeCtx(
        repo=repo,
        meiva_home=meiva_home,
        roles=None,
        config=command_runtime.config,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
        helper=refuse_helper,
        host=fake_host(),
        services=frozenset(),
        timers=ManualTimers(),
    )
    external_skills.converge_external_agent_skill(context, host_home=host_home)
    target = client_home / "skills" / "meiva-guide"
    for relative in (
        "references/evaluation.md",
        "deploy/SKILL.md",
        "operations/references/db-restore.md",
        "packages/install/SKILL.md",
        "schedules/SKILL.md",
        "external-agents/scripts/spawn_codex.py",
        "pages/widgets/meiva_reply/reply.js",
        "pages/widgets/markdown/md.html",
        "pages/widgets/markdown/vendor/katex.min.js",
        "external-agents/references/collaboration_protocol.md",
        "external-agents/scripts/spawn_antigravity.py",
    ):
        assert (target / relative).read_bytes() == (GUIDE / relative).read_bytes()
    _assert_portable_reading_links(target)
    marker = json.loads((target / ".meiva-managed.json").read_text())
    assert marker["skill"] == "meiva-guide"
    assert (ledger_root / f"{key}-meiva-guide.json").is_file()
    for name, ledger in (
        ("operating-meiva-cluster", key),
        ("deploy-meiva-cluster", f"{key}-deploy-meiva-cluster"),
    ):
        assert (client_home / "skills" / name / "SKILL.md").read_text() == "user customization\n"
        assert (ledger_root / f"{ledger}.json").read_bytes() == legacy_bytes

    source = repo / "meiva_builtins" / "skills" / "platform" / "meiva-guide"
    (source / "schedules" / "SKILL.md").write_text("schedule instructions v2\n")
    external_skills.converge_external_agent_skill(context, host_home=host_home)
    assert (target / "schedules" / "SKILL.md").read_text() == "schedule instructions v2\n"
    (target / "schedules" / "SKILL.md").write_text("local customization\n")
    (source / "schedules" / "SKILL.md").write_text("schedule instructions v3\n")
    external_skills.converge_external_agent_skill(context, host_home=host_home)
    assert (target / "schedules" / "SKILL.md").read_text() == "local customization\n"
