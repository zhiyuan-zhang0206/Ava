"""The reorganized guide loads and publishes as one complete capability package."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

import ava.skills
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.extensions import external_skills
from cli.commands.extensions.skills_sync import converge_skills

REPO = Path(__file__).resolve().parents[4]
GUIDE = REPO / "ava_builtins" / "skills" / "platform" / "ava-guide"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(GUIDE, repo / "ava_builtins" / "skills" / "platform" / "ava-guide")
    shutil.copytree(
        REPO / "ava_builtins" / "skills" / "practice" / "ava-workflow",
        repo / "ava_builtins" / "skills" / "practice" / "ava-workflow",
    )
    return repo


def test_converged_namespaces_load_real_task_instructions(tmp_path: Path, unit_home: Path) -> None:
    repo = _repo(tmp_path)
    converge_skills(repo, unit_home)

    expected = {
        ("ava_guide", "deploy"): "ava init",
        ("ava_guide", "operations"): "Alert review",
        ("ava_guide", "modification_layers"): "L4",
        ("ava_guide", "packages", "install"): "Find candidates",
        ("ava_guide", "plugins", "develop"): "plugin.py",
        ("ava_guide", "schedules"): "resumable",
        ("ava_guide", "external_agents"): "Mode B",
        ("ava_guide", "pages"): "user input",
        ("ava_workflow", "capability_timescale"): "Gather evidence",
    }
    for segments, instruction in expected.items():
        skill = ava.skills
        for segment in segments:
            skill = getattr(skill, segment)
        body = skill.__doc__
        assert isinstance(body, str)
        assert instruction in body

    for old in (
        "ava_package_installer",
        "ava_schedule_writer",
        "ava_use_other_agents",
        "ava_ui",
        "ava_watcher",
        "ava_ultra_speed",
    ):
        with pytest.raises(AttributeError):
            getattr(ava.skills, old)


def _assert_portable_reading_links(target: Path) -> None:
    """Installed reading links must resolve within their complete skill package."""
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
    tmp_path: Path, client: str
) -> None:
    repo = _repo(tmp_path)
    host_home = tmp_path / "host"
    client_home = host_home / client
    client_home.mkdir(parents=True)
    ava_home = tmp_path / "ava-home"
    (ava_home / "configs").mkdir(parents=True)
    ledger_root = ava_home / "configs" / "external-agent-skills"
    ledger_root.mkdir()
    key = client.removeprefix(".")
    legacy_bytes = b'{"legacy": "private ownership evidence"}\n'
    for name, ledger in (
        ("operating-ava-cluster", key),
        ("deploy-ava-cluster", f"{key}-deploy-ava-cluster"),
    ):
        legacy = client_home / "skills" / name
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("user customization\n")
        (ledger_root / f"{ledger}.json").write_bytes(legacy_bytes)

    context = ConvergeCtx(repo=repo, ava_home=ava_home, roles=None)
    external_skills.converge_external_agent_skill(context, host_home=host_home)
    target = client_home / "skills" / "ava-guide"
    for relative in (
        "references/evaluation.md",
        "presets/SKILL.md",
        "presets/evals/metrics.md",
        "presets/references/role-cards.md",
        "deploy/SKILL.md",
        "operations/references/db-restore.md",
        "packages/install/SKILL.md",
        "schedules/SKILL.md",
        "external-agents/scripts/spawn_codex.py",
        "pages/widgets/ava_reply/reply.js",
        "pages/widgets/markdown/md.html",
        "pages/widgets/markdown/vendor/katex.min.js",
        "external-agents/references/collaboration_protocol.md",
        "external-agents/scripts/ava-relay/.claude-plugin/plugin.json",
    ):
        assert (target / relative).read_bytes() == (GUIDE / relative).read_bytes()
    _assert_portable_reading_links(target)
    marker = json.loads((target / ".ava-managed.json").read_text())
    assert marker["skill"] == "ava-guide"
    assert (ledger_root / f"{key}-ava-guide.json").is_file()
    for name, ledger in (
        ("operating-ava-cluster", key),
        ("deploy-ava-cluster", f"{key}-deploy-ava-cluster"),
    ):
        assert (client_home / "skills" / name / "SKILL.md").read_text() == "user customization\n"
        assert (ledger_root / f"{ledger}.json").read_bytes() == legacy_bytes

    # A source update reaches a nested file; later local edits remain protected.
    source = repo / "ava_builtins" / "skills" / "platform" / "ava-guide"
    (source / "schedules" / "SKILL.md").write_text("schedule instructions v2\n")
    external_skills.converge_external_agent_skill(context, host_home=host_home)
    assert (target / "schedules" / "SKILL.md").read_text() == "schedule instructions v2\n"
    (target / "schedules" / "SKILL.md").write_text("local customization\n")
    (source / "schedules" / "SKILL.md").write_text("schedule instructions v3\n")
    external_skills.converge_external_agent_skill(context, host_home=host_home)
    assert (target / "schedules" / "SKILL.md").read_text() == "local customization\n"
