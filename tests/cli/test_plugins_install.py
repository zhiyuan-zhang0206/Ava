"""`ava plugins install/uninstall/installed/upgrade` — using local file:// git fixture
Run end-to-end, no network. `unit_home` isolates ~/.ava.
"""

import subprocess
from pathlib import Path

import pytest

import ava.skills as skills_mod
from cli.commands import (
    cmd_plugins_install,
    cmd_plugins_installed,
    cmd_plugins_uninstall,
    cmd_plugins_upgrade,
)
from shared import install_registry as reg


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _make_skill_repo(root: Path, name: str, description: str = "a fixture skill") -> str:
    """Build a one-commit git repo whose root is a skill package; return its file:// URL."""
    repo = root / f"{name}-src"
    repo.mkdir()
    (repo / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n", encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return f"file://{repo}"


def _make_plugin_repo(root: Path) -> str:
    repo = root / "plugin-src"
    repo.mkdir()
    (repo / "plugin.py").write_text("__description__ = 'x'\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return f"file://{repo}"


def test_install_skill_records_and_surfaces(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    url = _make_skill_repo(tmp_path, "demo")
    assert cmd_plugins_install(url, None, None) == 0

    pkg = reg.get("demo")
    assert pkg is not None and pkg.type == "skill" and pkg.source == url and pkg.enabled
    assert (unit_home / "skills" / "demo" / "SKILL.md").is_file()
    # .git stripped from the installed copy
    assert not (unit_home / "skills" / "demo" / ".git").exists()
    # scanner surfaces it live (no restart)
    assert "demo" in {s["name"] for s in skills_mod._names()}


def test_install_disabled_skill_hidden_from_scanner(unit_home: Path, tmp_path: Path) -> None:
    url = _make_skill_repo(tmp_path, "demo")
    cmd_plugins_install(url, None, None)
    reg.register(
        reg.InstalledPackage(name="demo", type="skill", source=url, ref=None, enabled=False)
    )
    assert "demo" not in {s["name"] for s in skills_mod._names()}


def test_install_rejects_duplicate(unit_home: Path, tmp_path: Path) -> None:
    url = _make_skill_repo(tmp_path, "demo")
    assert cmd_plugins_install(url, None, None) == 0
    assert cmd_plugins_install(url, None, None) == 1  # already installed


def test_install_rejects_unrecognized(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    url = _make_plugin_repo(tmp_path)
    assert cmd_plugins_install(url, None, None) == 1
    assert "unrecognized package" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert reg.load().packages == []


def test_install_bare_mcp_package_signposts_mcp_install(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A standalone MCP package (bare .mcp.json) points the user at `ava mcp install`."""
    repo = tmp_path / "mcp-src"
    repo.mkdir()
    (repo / ".mcp.json").write_text(
        '{"mcpServers": {"acme": {"command": ".venv/bin/python", "args": ["-m", "acme"]}}}',
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    assert cmd_plugins_install(f"file://{repo}", None, None) == 1
    assert "ava mcp install" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert reg.load().packages == []


def test_uninstall_removes_dir_and_entry(unit_home: Path, tmp_path: Path) -> None:
    url = _make_skill_repo(tmp_path, "demo")
    cmd_plugins_install(url, None, None)
    assert cmd_plugins_uninstall("demo") == 0
    assert reg.get("demo") is None
    assert not (unit_home / "skills" / "demo").exists()


def test_uninstall_unknown_errors(unit_home: Path) -> None:
    assert cmd_plugins_uninstall("nope") == 1


def test_installed_lists_entries(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    assert cmd_plugins_installed() == 0
    assert "(none)" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    cmd_plugins_install(_make_skill_repo(tmp_path, "demo"), None, None)
    assert cmd_plugins_installed() == 0
    assert "demo" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_upgrade_refetches(unit_home: Path, tmp_path: Path) -> None:
    url = _make_skill_repo(tmp_path, "demo", description="v1")
    cmd_plugins_install(url, None, None)
    # mutate the source repo + commit, then upgrade
    repo = tmp_path / "demo-src"
    (repo / "SKILL.md").write_text(
        "---\nname: demo\ndescription: v2\n---\n\n# demo v2\n", encoding="utf-8"
    )
    _git(repo, "commit", "-aqm", "v2")
    assert cmd_plugins_upgrade("demo") == 0
    installed = (unit_home / "skills" / "demo" / "SKILL.md").read_text()
    assert "v2" in installed


# --- Claude Code plugin install (git-subdir + agents->references) ---


def _make_claude_code_plugin_repo(
    root: Path,
    subdir: str = "plugins/pr-toolkit",
    *,
    agents: bool = True,
    mcp: bool = False,
    skills: bool = False,
    commands: bool = False,
) -> str:
    """Build a monorepo holding a Claude Code plugin at `subdir`; return its file:// URL.

    The plugin name comes from `.claude-plugin/plugin.json`. `agents=False` drops
    the specialist set; `mcp=True` bundles a root `.mcp.json`; `skills=True`
    ships a `skills/` tree (the superpowers shape); `commands=True` ships a
    `commands/` slash-command. A plugin with none of these is the refusal case.
    """
    repo = root / "claude-code-src"
    repo.mkdir(exist_ok=True)
    pkg = repo / subdir
    (pkg / ".claude-plugin").mkdir(parents=True)
    (pkg / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "pr-toolkit", "description": "Review agents"}', encoding="utf-8"
    )
    if skills:
        for skill_name in ("brainstorming", "writing-plans"):
            skill_dir = pkg / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {skill_name}\ndescription: A {skill_name} skill.\n---\n\n# {skill_name}\n",
                encoding="utf-8",
            )
            (skill_dir / "helper.md").write_text("supporting file\n", encoding="utf-8")
    if mcp:
        (pkg / ".mcp.json").write_text(
            '{"mcpServers": {"demo-server": {"command": "echo", "args": ["hi"]}}}',
            encoding="utf-8",
        )
    if commands:
        (pkg / "commands").mkdir(exist_ok=True)
        (pkg / "commands" / "deploy.md").write_text(
            "---\ndescription: Deploy the thing\nargument-hint: env\n---\nDeploy to {env}.\n",
            encoding="utf-8",
        )
    if agents:
        (pkg / "agents").mkdir()
        (pkg / "agents" / "code-reviewer.md").write_text(
            "---\nname: code-reviewer\ndescription: Review code for bugs. Always run.\nmodel: opus\n---\n"
            "You are a code reviewer.\n",
            encoding="utf-8",
        )
        (pkg / "agents" / "code-simplifier.md").write_text(
            "---\nname: code-simplifier\ndescription: Simplify after review.\n---\nYou simplify.\n",
            encoding="utf-8",
        )
        (pkg / "commands").mkdir(exist_ok=True)
        (pkg / "commands" / "review-pr.md").write_text("# review\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return f"file://{repo}"


def test_install_claude_code_plugin_from_subdir_surfaces_skill(
    unit_home: Path, tmp_path: Path
) -> None:
    url = _make_claude_code_plugin_repo(tmp_path)
    assert cmd_plugins_install(url, None, "plugins/pr-toolkit") == 0

    pkg = reg.get("pr-toolkit")
    assert pkg is not None
    assert pkg.type == "plugin" and pkg.path == "plugins/pr-toolkit" and pkg.enabled

    skill_md = unit_home / "plugins" / "pr-toolkit" / "skills" / "pr-toolkit" / "SKILL.md"
    assert skill_md.is_file()
    refs = unit_home / "plugins" / "pr-toolkit" / "skills" / "pr-toolkit" / "references"
    assert {p.name for p in refs.iterdir()} == {"code-reviewer.md", "code-simplifier.md"}
    # Claude Code agent frontmatter stripped, header carries name + description
    body = (refs / "code-reviewer.md").read_text()
    assert body.startswith("# code-reviewer") and "model: opus" not in body
    # scanner surfaces the generated orchestrator skill live (source-4, no restart)
    assert "pr-toolkit" in {s["name"] for s in skills_mod._names()}


def test_install_claude_code_plugin_without_anything_refused(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    url = _make_claude_code_plugin_repo(tmp_path, agents=False, mcp=False, skills=False)
    assert cmd_plugins_install(url, None, "plugins/pr-toolkit") == 1
    assert "nothing to" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert reg.load().packages == []


def test_install_claude_code_plugin_skills_only_surfaces(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # superpowers shape: a Claude Code plugin shipping skills/ but no agents/ or .mcp.json
    url = _make_claude_code_plugin_repo(tmp_path, agents=False, mcp=False, skills=True)
    assert cmd_plugins_install(url, None, "plugins/pr-toolkit") == 0

    pkg = reg.get("pr-toolkit")
    assert pkg is not None and pkg.type == "plugin" and pkg.enabled
    # skills copied verbatim (supporting files too), no generated orchestrator
    base = unit_home / "plugins" / "pr-toolkit" / "skills"
    assert (base / "brainstorming" / "SKILL.md").is_file()
    assert (base / "brainstorming" / "helper.md").is_file()
    assert (base / "writing-plans" / "SKILL.md").is_file()
    # scanner surfaces both shipped skills live (source-4, no restart)
    surfaced = {s["name"] for s in skills_mod._names()}
    assert {"brainstorming", "writing-plans"} <= surfaced
    # install message itemizes the shipped skills
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "brainstorming" in out and "writing-plans" in out


def test_upgrade_claude_code_plugin_skills_only(unit_home: Path, tmp_path: Path) -> None:
    url = _make_claude_code_plugin_repo(tmp_path, agents=False, mcp=False, skills=True)
    cmd_plugins_install(url, None, "plugins/pr-toolkit")
    # add a third skill to the source, commit, upgrade
    repo = tmp_path / "claude-code-src"
    new_skill = repo / "plugins" / "pr-toolkit" / "skills" / "debugging"
    new_skill.mkdir(parents=True)
    (new_skill / "SKILL.md").write_text(
        "---\nname: debugging\ndescription: A debugging skill.\n---\n\n# debugging\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add skill")
    assert cmd_plugins_upgrade("pr-toolkit") == 0
    assert (unit_home / "plugins" / "pr-toolkit" / "skills" / "debugging" / "SKILL.md").is_file()


def test_install_claude_code_plugin_reports_contributions(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    url = _make_claude_code_plugin_repo(tmp_path, agents=True, mcp=True)
    assert cmd_plugins_install(url, None, "plugins/pr-toolkit") == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    # the install message itemizes what the plugin folder contributed
    assert "contributes:" in out
    assert "skill 'pr-toolkit'" in out
    assert "code-reviewer" in out and "code-simplifier" in out
    assert "demo-server" in out
    # the plugin's commands/ (review-pr.md) is itemized too
    assert "/review-pr" in out


def test_install_claude_code_plugin_with_bundled_mcp(unit_home: Path, tmp_path: Path) -> None:
    from ava._mcp_config import load_mcp_config

    url = _make_claude_code_plugin_repo(tmp_path, agents=True, mcp=True)
    assert cmd_plugins_install(url, None, "plugins/pr-toolkit") == 0

    # skill still materialized
    assert (unit_home / "plugins" / "pr-toolkit" / "skills" / "pr-toolkit" / "SKILL.md").is_file()
    # .mcp.json carried into the overlay, where the shared loader merges it
    assert (unit_home / "plugins" / "pr-toolkit" / ".mcp.json").is_file()
    assert "demo-server" in load_mcp_config()


def test_install_claude_code_plugin_mcp_only(unit_home: Path, tmp_path: Path) -> None:
    from ava._mcp_config import load_mcp_config

    url = _make_claude_code_plugin_repo(tmp_path, agents=False, mcp=True)
    assert cmd_plugins_install(url, None, "plugins/pr-toolkit") == 0

    pkg = reg.get("pr-toolkit")
    assert pkg is not None and pkg.type == "plugin" and pkg.enabled
    # no skill subtree (no agents), but the MCP is contributed
    assert not (unit_home / "plugins" / "pr-toolkit" / "skills").exists()
    assert (unit_home / "plugins" / "pr-toolkit" / ".mcp.json").is_file()
    assert "demo-server" in load_mcp_config()
    # nothing for the skill scanner to surface
    assert "pr-toolkit" not in {s["name"] for s in skills_mod._names()}


def test_install_claude_code_plugin_commands_surface(unit_home: Path, tmp_path: Path) -> None:
    from ava._commands import discover_commands

    # commands-only plugin: no skills/agents/mcp, just commands/
    url = _make_claude_code_plugin_repo(
        tmp_path, agents=False, mcp=False, skills=False, commands=True
    )
    assert cmd_plugins_install(url, None, "plugins/pr-toolkit") == 0

    # commands/ copied verbatim into the overlay
    assert (unit_home / "plugins" / "pr-toolkit" / "commands" / "deploy.md").is_file()
    # discover_commands surfaces it namespaced under the plugin in the canonical
    # `:`-joined form (same display rule as skill identifiers): /pr-toolkit:deploy
    cmds = {c["name"]: c for c in discover_commands()}
    assert "pr-toolkit:deploy" in cmds
    assert cmds["pr-toolkit:deploy"]["description"] == "Deploy the thing"
    assert cmds["pr-toolkit:deploy"]["instruction_hint"] == "env"


def test_uninstall_claude_code_plugin_removes_plugin_dir(unit_home: Path, tmp_path: Path) -> None:
    url = _make_claude_code_plugin_repo(tmp_path)
    cmd_plugins_install(url, None, "plugins/pr-toolkit")
    assert cmd_plugins_uninstall("pr-toolkit") == 0
    assert reg.get("pr-toolkit") is None
    assert not (unit_home / "plugins" / "pr-toolkit").exists()


def test_upgrade_claude_code_plugin_rematerializes(unit_home: Path, tmp_path: Path) -> None:
    url = _make_claude_code_plugin_repo(tmp_path)
    cmd_plugins_install(url, None, "plugins/pr-toolkit")
    # add a third agent to the source, commit, upgrade
    repo = tmp_path / "claude-code-src"
    (repo / "plugins" / "pr-toolkit" / "agents" / "type-design-analyzer.md").write_text(
        "---\nname: type-design-analyzer\ndescription: Review type design.\n---\nYou analyze types.\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add agent")
    assert cmd_plugins_upgrade("pr-toolkit") == 0
    refs = unit_home / "plugins" / "pr-toolkit" / "skills" / "pr-toolkit" / "references"
    assert (refs / "type-design-analyzer.md").is_file()
