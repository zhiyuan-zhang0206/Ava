"""`ava skill install` — Agent Skills standard packages install unmodified.

Every fixture here is written to the published standard (agentskills.io): a
`name`/`description` frontmatter plus the optional `license` / `compatibility` /
`metadata` / `allowed-tools` fields, in the directory layouts the ecosystem
ships (bare skill, `skills/`, `.claude/skills/`). Nothing is Ava-specific, and
the assertions run all the way to the namespace mount — installed *and*
reachable as `ava.skills.<name>`.

Local-path sources are used in place; git sources go through a local `file://`
fixture repo, so no test touches the network. `unit_home` isolates `~/.ava`.
"""

import subprocess
from pathlib import Path

import pytest

import ava.skills as skills_mod
from cli.commands import cmd_skill_install
from shared import install_registry as reg

# Every test here installs a package, which records `local:<machine>` provenance
# in the cluster registry — that needs a machine identity, which a bare
# `unit_home` deliberately lacks. See the fixture's docstring.
pytestmark = pytest.mark.usefixtures("_installed_machine_identity")

# A skill carrying every optional field the standard defines — the compatibility
# claim in one fixture. Ava honors name + description and preserves the rest on
# disk without tripping over it.
_STANDARD_SKILL = """\
---
name: {name}
description: {description}
license: Apache-2.0
compatibility: Requires git and network access
allowed-tools: Bash(git:*) Bash(jq:*) Read
metadata:
  author: example-org
  version: "1.0"
---

# {name}

Step-by-step instructions live here.
"""


def _write_skill(d: Path, name: str, description: str = "does a thing, use when asked") -> Path:
    """Lay down a standard skill package at `d` (dir named after the skill)."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        _STANDARD_SKILL.format(name=name, description=description), encoding="utf-8"
    )
    (d / "references").mkdir(exist_ok=True)
    (d / "references" / "REFERENCE.md").write_text(f"# {name} reference\n", encoding="utf-8")
    (d / "scripts").mkdir(exist_ok=True)
    (d / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
    return d


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _commit_repo(repo: Path) -> str:
    """One-commit git repo at `repo`; return its file:// URL."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return f"file://{repo}"


def _installed_names() -> set[str]:
    return {s["name"] for s in skills_mod._names()}


def test_bare_standard_skill_from_local_path(unit_home: Path, tmp_path: Path) -> None:
    """A skill folder with SKILL.md at its root installs from a local path, with
    its bundled `scripts/` + `references/` carried along, and mounts."""
    src = _write_skill(tmp_path / "pdf-processing", "pdf-processing")

    assert cmd_skill_install(str(src), None, None) == 0

    dest = unit_home / "skills" / "pdf-processing"
    assert (dest / "SKILL.md").is_file()
    assert (dest / "references" / "REFERENCE.md").is_file()
    assert (dest / "scripts" / "run.py").is_file()
    assert src.is_dir()  # a local source is read in place, never moved
    pkg = reg.get("pdf-processing")
    assert pkg is not None and pkg.type == "skill" and pkg.enabled
    assert "pdf-processing" in _installed_names()


def test_optional_standard_fields_do_not_block_the_mount(unit_home: Path, tmp_path: Path) -> None:
    """`allowed-tools` / `license` / `compatibility` / `metadata` are valid
    standard fields Ava does not act on — they must not keep the skill from
    loading, and the file must reach the agent verbatim."""
    _write_skill(tmp_path / "code-review", "code-review")
    assert cmd_skill_install(str(tmp_path / "code-review"), None, None) == 0

    proxy = skills_mod.code_review
    assert proxy.name == "code-review"
    assert "allowed-tools: Bash(git:*) Bash(jq:*) Read" in (proxy.__doc__ or "")
    assert "license: Apache-2.0" in (proxy.__doc__ or "")


def test_claude_code_skills_tree(unit_home: Path, tmp_path: Path) -> None:
    """An unmodified `.claude/skills/` tree — the Claude Code on-disk layout —
    installs every skill it holds, each as its own tracked package."""
    repo = tmp_path / "dotfiles"
    _write_skill(repo / ".claude" / "skills" / "commit-style", "commit-style")
    _write_skill(repo / ".claude" / "skills" / "release-notes", "release-notes")
    (repo / "README.md").write_text("my dotfiles\n", encoding="utf-8")
    url = _commit_repo(repo)

    assert cmd_skill_install(url, None, None) == 0

    assert {"commit-style", "release-notes"} <= _installed_names()
    assert reg.get("commit-style") is not None and reg.get("release-notes") is not None
    assert not (unit_home / "skills" / "commit-style" / ".git").exists()


def test_skills_collection_keeps_nested_subskills(unit_home: Path, tmp_path: Path) -> None:
    """A `skills/` collection installs each top-level skill whole, so a skill
    with sub-skills keeps its namespace shape (`ava.skills.<pkg>.<child>`)."""
    repo = tmp_path / "superpowers"
    _write_skill(repo / "skills" / "workflows", "workflows")
    _write_skill(repo / "skills" / "workflows" / "brainstorming", "brainstorming")
    url = _commit_repo(repo)

    assert cmd_skill_install(url, None, None) == 0

    assert (unit_home / "skills" / "workflows" / "brainstorming" / "SKILL.md").is_file()
    assert skills_mod.workflows.brainstorming.name == "brainstorming"


def test_bare_collection_of_skill_folders(unit_home: Path, tmp_path: Path) -> None:
    """A repo that is just a pile of skill folders (no `skills/` wrapper)
    installs them all; hidden dirs like `.git` are never mistaken for one."""
    repo = tmp_path / "skill-pack"
    _write_skill(repo / "data-analysis", "data-analysis")
    _write_skill(repo / "web-research", "web-research")
    url = _commit_repo(repo)

    assert cmd_skill_install(url, None, None) == 0
    assert {"data-analysis", "web-research"} <= _installed_names()


def test_path_selects_a_subdirectory(unit_home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "monorepo"
    _write_skill(repo / "tools" / "pdf-processing", "pdf-processing")
    url = _commit_repo(repo)

    assert cmd_skill_install(url, None, "tools/pdf-processing") == 0
    assert "pdf-processing" in _installed_names()
    assert reg.get("pdf-processing").path == "tools/pdf-processing"  # type: ignore[union-attr]


def test_crlf_and_bom_skill_installs(unit_home: Path, tmp_path: Path) -> None:
    """A standard skill authored on Windows (CRLF, UTF-8 BOM) is still a valid
    standard skill — the encoding must not be what rejects it."""
    src = tmp_path / "windows-skill"
    src.mkdir()
    body = "---\nname: windows-skill\ndescription: written on windows\n---\n\n# hi\n"
    (src / "SKILL.md").write_text("﻿" + body.replace("\n", "\r\n"), encoding="utf-8")

    assert cmd_skill_install(str(src), None, None) == 0
    assert "windows-skill" in _installed_names()


def test_collision_leaves_the_load_dir_untouched(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """One already-installed skill aborts the whole multi-skill install before
    the first copy — no half-populated load dir."""
    repo = tmp_path / "pack"
    _write_skill(repo / "alpha", "alpha")
    _write_skill(repo / "beta", "beta")
    assert cmd_skill_install(str(repo / "alpha"), None, None) == 0

    assert cmd_skill_install(str(repo), None, None) == 1
    assert "already installed: alpha" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert not (unit_home / "skills" / "beta").exists()
    assert reg.get("beta") is None


def test_no_skill_in_source_fails_fast(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    src = tmp_path / "not-a-skill"
    (src / "docs").mkdir(parents=True)
    (src / "README.md").write_text("nothing here\n", encoding="utf-8")

    assert cmd_skill_install(str(src), None, None) == 1
    assert "no skill found" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert reg.load().packages == []


def test_malformed_skill_md_fails_fast(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Unlike the runtime scan (which skip-warns past a broken third-party
    skill), an explicit install refuses it — the user is here to hear about it."""
    src = tmp_path / "broken"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: broken\n---\n\nno description\n", encoding="utf-8")

    assert cmd_skill_install(str(src), None, None) == 1
    assert "description" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert reg.load().packages == []


def test_missing_path_in_source_errors(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    src = _write_skill(tmp_path / "solo", "solo")
    assert cmd_skill_install(str(src), None, "nope") == 1
    assert "not found in source" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_install_skips_junk_names(unit_home: Path, tmp_path: Path) -> None:
    """Copy hygiene matches converge: __pycache__ / .DS_Store never ride along
    into the load dir (audit #10)."""
    src = tmp_path / "clean-skill"
    _write_skill(src, "clean-skill")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.pyc").write_bytes(b"x")
    (src / ".DS_Store").write_bytes(b"x")

    assert cmd_skill_install(str(src), None, None) == 0

    dest = unit_home / "skills" / "clean-skill"
    assert (dest / "SKILL.md").is_file()
    assert not (dest / "__pycache__").exists()
    assert not (dest / ".DS_Store").exists()
