"""`ava skill update` / `ava skill upgrade` — the R5 explicit-update commands.

Repo-native skills update via `skill update` (converge only lands missing
copies); user-installed skills with a recorded git source update via
`skill upgrade`. Both share the conflict contract: a locally edited copy
refuses unless `--force`.
"""

import os
import subprocess
from pathlib import Path

import pytest

from cli.commands.skill import cmd_skill_update, cmd_skill_upgrade
from shared import install_registry as reg

# Every test here installs a package, which records `local:<machine>` provenance
# in the cluster registry — that needs a machine identity, which a bare
# `unit_home` deliberately lacks. See the fixture's docstring.
pytestmark = pytest.mark.usefixtures("_installed_machine_identity")


def _write_skill(root: Path, dirname: str, body: str = "# B\n") -> Path:
    d = root / dirname
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {dirname}\ndescription: d\n---\n\n{body}", encoding="utf-8"
    )
    return d


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Synthetic checkout: one builtin skill + one .agents project skill.
    The .agents skill exists only to prove update does NOT touch it
    (issue #146 — project skills reach agents via the local mount)."""
    r = tmp_path / "repo"
    _write_skill(r / "ava_builtins" / "skills", "builtin-a")
    _write_skill(r / ".agents" / "skills", "project-x")
    return r


def _entry(name: str) -> reg.InstalledPackage:
    pkg = reg.get(name)
    assert pkg is not None
    return pkg


# ─── skill update: bootstrap ────────────────────────────────────────────────


def test_update_lands_missing_and_reports(unit_home: Path, repo: Path, capsys) -> None:
    assert cmd_skill_update(None, repo=repo) == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "landed 'builtin-a'" in out
    assert "project-x" not in out
    assert (unit_home / "skills" / "builtin-a" / "SKILL.md").is_file()
    assert not (unit_home / "skills" / "project-x").exists()
    assert reg.get("project-x") is None


def test_update_agents_project_skill_is_not_repo_native(
    unit_home: Path, repo: Path, capsys
) -> None:
    """`.agents/skills` skills are no longer repo-native sources (issue #146):
    update reports them as unknown and never lands a copy."""
    assert cmd_skill_update(["project-x"], repo=repo) == 0
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "'project-x' is not a repo-native skill" in err
    assert not (unit_home / "skills" / "project-x").exists()


def test_update_unknown_name_errors(unit_home: Path, repo: Path, capsys) -> None:
    assert cmd_skill_update(["nope"], repo=repo) == 0  # others still run; unknown reported
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "'nope' is not a repo-native skill" in err


# ─── skill update: source change / conflict / force ─────────────────────────


def test_update_propagates_source_change(unit_home: Path, repo: Path) -> None:
    assert cmd_skill_update(None, repo=repo) == 0
    (repo / "ava_builtins" / "skills" / "builtin-a" / "SKILL.md").write_text(
        "---\nname: builtin-a\ndescription: v2\n---\n\n# v2\n", encoding="utf-8"
    )
    assert cmd_skill_update(None, repo=repo) == 0
    body = (unit_home / "skills" / "builtin-a" / "SKILL.md").read_text(encoding="utf-8")
    assert "# v2" in body


def test_update_conflict_on_local_edit_refuses(unit_home: Path, repo: Path, capsys) -> None:
    assert cmd_skill_update(None, repo=repo) == 0
    copy = unit_home / "skills" / "builtin-a" / "SKILL.md"
    copy.write_text("---\nname: builtin-a\ndescription: MINE\n---\n\nhands off\n", encoding="utf-8")
    (repo / "ava_builtins" / "skills" / "builtin-a" / "SKILL.md").write_text(
        "---\nname: builtin-a\ndescription: v2\n---\n", encoding="utf-8"
    )
    rc = cmd_skill_update(None, repo=repo)
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "conflict" in err and "--force" in err
    assert "hands off" in copy.read_text(encoding="utf-8")


def test_update_force_overwrites_local_edit(unit_home: Path, repo: Path) -> None:
    assert cmd_skill_update(None, repo=repo) == 0
    copy = unit_home / "skills" / "builtin-a" / "SKILL.md"
    copy.write_text("---\nname: builtin-a\ndescription: MINE\n---\n\nhands off\n", encoding="utf-8")
    (repo / "ava_builtins" / "skills" / "builtin-a" / "SKILL.md").write_text(
        "---\nname: builtin-a\ndescription: v2\n---\n", encoding="utf-8"
    )
    assert cmd_skill_update(None, repo=repo, force=True) == 0
    body = copy.read_text(encoding="utf-8")
    assert "hands off" not in body and "v2" in body


def test_update_local_edit_without_source_change_reports_conflict(
    unit_home: Path, repo: Path, capsys
) -> None:
    """Local edits alone (no upstream change) still surface — --force restores."""
    assert cmd_skill_update(None, repo=repo) == 0
    copy = unit_home / "skills" / "builtin-a" / "SKILL.md"
    copy.write_text("---\nname: builtin-a\ndescription: MINE\n---\n\nhands off\n", encoding="utf-8")
    rc = cmd_skill_update(None, repo=repo)
    assert rc == 1
    assert "conflict" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert cmd_skill_update(None, repo=repo, force=True) == 0
    assert "hands off" not in copy.read_text(encoding="utf-8")


# ─── skill update: adoption of hand-installed .agents residue ───────────────


def test_update_adopts_matching_user_residue(unit_home: Path, repo: Path) -> None:
    """The pre-converge way to get a skill into the load dir was a manual
    `skill install --path .agents/skills/<name>` (origin=user) — here of a
    builtin, hand-installed from the open-standard mirror. update adopts the
    copy as repo-native when its content matches the source."""
    _write_skill(unit_home / "skills", "builtin-a", body="# from repo\n")
    (repo / "ava_builtins" / "skills" / "builtin-a" / "SKILL.md").write_text(
        "---\nname: builtin-a\ndescription: d\n---\n\n# from repo\n", encoding="utf-8"
    )
    reg.register(
        reg.InstalledPackage(
            name="builtin-a", type="skill", source=".agents/skills/builtin-a", origin="user"
        )
    )
    assert cmd_skill_update(None, repo=repo) == 0
    entry = _entry("builtin-a")
    assert entry.origin == "repo" and entry.source is None
    assert (unit_home / "skills" / "builtin-a" / "SKILL.md").exists()


def test_update_conflicts_on_diverged_user_residue(unit_home: Path, repo: Path, capsys) -> None:
    _write_skill(unit_home / "skills", "builtin-a", body="# user hacked\n")
    reg.register(
        reg.InstalledPackage(
            name="builtin-a", type="skill", source=".agents/skills/builtin-a", origin="user"
        )
    )
    rc = cmd_skill_update(None, repo=repo)
    assert rc == 1
    assert "--force" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    # --force adopts the repo version
    assert cmd_skill_update(None, repo=repo, force=True) == 0
    assert _entry("builtin-a").origin == "repo"
    body = (unit_home / "skills" / "builtin-a" / "SKILL.md").read_text(encoding="utf-8")
    assert "user hacked" not in body


def test_update_leaves_third_party_user_package_alone(unit_home: Path, repo: Path, capsys) -> None:
    """A genuinely third-party user install squatting a repo name is shadowed
    by converge; update must not adopt it either."""
    _write_skill(unit_home / "skills", "builtin-a", body="# third party\n")
    reg.register(
        reg.InstalledPackage(
            name="builtin-a", type="skill", source="https://example.com/skills.git", origin="user"
        )
    )
    assert cmd_skill_update(None, repo=repo) == 0
    assert _entry("builtin-a").origin == "user"
    body = (unit_home / "skills" / "builtin-a" / "SKILL.md").read_text(encoding="utf-8")
    assert "# third party" in body


# ─── skill upgrade: git-source re-fetch ─────────────────────────────────────


def _git(repo: Path, *args: str) -> None:
    # CI runners carry no git identity; commits need one even in a throwaway
    # fixture repo (exit 128 otherwise).
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "ava-test",
        "GIT_AUTHOR_EMAIL": "ava-test@example.com",
        "GIT_COMMITTER_NAME": "ava-test",
        "GIT_COMMITTER_EMAIL": "ava-test@example.com",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)  # noqa: S603 — fixed argv, test fixture


def _skill_git_repo(tmp_path: Path, name: str = "ext-skill") -> str:
    """A git repo holding one bare skill; returns its URL."""
    r = tmp_path / "ext-src"
    _write_skill(r, name, body="# v1\n")
    _git(r, "init", "-q")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "v1")
    return str(r)


def _install_skill(url: str, name: str = "ext-skill") -> None:
    from cli.commands.skill import cmd_skill_install

    assert cmd_skill_install(url, None, None) == 0
    assert _entry(name).installed_hash is not None


def test_upgrade_skips_unupdatable(unit_home: Path, capsys) -> None:
    _write_skill(unit_home / "skills", "local-skill")
    reg.register(reg.InstalledPackage(name="local-skill", type="skill"))  # no source
    assert cmd_skill_upgrade("local-skill") == 1
    assert "no recorded source" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_upgrade_refetches_from_source(unit_home: Path, tmp_path: Path) -> None:
    url = _skill_git_repo(tmp_path)
    _install_skill(url)
    r = Path(url)
    (r / "SKILL.md").write_text(
        "---\nname: ext-skill\ndescription: d\n---\n\n# v2\n", encoding="utf-8"
    )
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "v2")
    assert cmd_skill_upgrade("ext-skill") == 0
    body = (unit_home / "skills" / "ext-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "# v2" in body


def test_upgrade_local_source_is_copied_never_moved(unit_home: Path, tmp_path: Path) -> None:
    """A local-path source is read in place (never moved): upgrade must not
    relocate the user's own directory or delete its .git (install's docstring
    promises 'never moved' — the old upgrade moved it into $AVA_HOME/skills
    and rmtree'd the checkout's .git). The installed copy updates; the source
    stays put."""
    src_dir = Path(_skill_git_repo(tmp_path))  # a local dir WITH .git
    _install_skill(str(src_dir))

    (src_dir / "SKILL.md").write_text(
        "---\nname: ext-skill\ndescription: d\n---\n\n# v2\n", encoding="utf-8"
    )
    assert cmd_skill_upgrade("ext-skill") == 0

    # The user's source dir is untouched: still there, .git intact.
    assert src_dir.is_dir()
    assert (src_dir / ".git").is_dir()
    assert "# v2" in (src_dir / "SKILL.md").read_text(encoding="utf-8")
    # And the installed copy carries the new content.
    body = (unit_home / "skills" / "ext-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "# v2" in body


def test_upgrade_conflict_refuses_then_force(unit_home: Path, tmp_path: Path, capsys) -> None:
    url = _skill_git_repo(tmp_path)
    _install_skill(url)
    copy = unit_home / "skills" / "ext-skill" / "SKILL.md"
    copy.write_text("---\nname: ext-skill\ndescription: d\n---\n\n# hacked\n", encoding="utf-8")
    r = Path(url)
    (r / "SKILL.md").write_text(
        "---\nname: ext-skill\ndescription: d\n---\n\n# v2\n", encoding="utf-8"
    )
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "v2")

    assert cmd_skill_upgrade("ext-skill") == 1
    assert "modified locally" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "# hacked" in copy.read_text(encoding="utf-8")

    assert cmd_skill_upgrade("ext-skill", force=True) == 0
    assert "# hacked" not in copy.read_text(encoding="utf-8")


# ─── worktree source bound (audit round 2, skills-plugins #3) ───────────────


def test_update_refuses_worktree_repo_for_default_home(
    unit_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`ava skill update` from a worktree checkout must not write the prod home
    (the R5 worktree that synced ava-serious-research into prod)."""

    monkeypatch.setattr("shared.cluster.derive.default_home", lambda: unit_home)
    wt_repo = tmp_path / "repo" / ".worktrees" / "ava-9999-task"
    _write_skill(wt_repo / "ava_builtins" / "skills", "builtin-a")
    assert cmd_skill_update(None, repo=wt_repo) == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "worktree" in err
    assert not (unit_home / "skills" / "builtin-a").exists()


# ─── adopt trust + stale origin_path re-anchor (audit 02 #5/#15) ────────────


def test_update_adopt_sets_builtin_trust(unit_home: Path, repo: Path) -> None:
    """Adopting a hand-installed residue of a repo skill stamps it builtin —
    it ships under the checkout's review, not third-party."""
    # Simulate the pre-incorporation state: user row (no disk copy - update materializes source content)
    from datetime import UTC, datetime

    reg.register(
        reg.InstalledPackage(
            name="builtin-a",
            type="skill",
            origin="user",
            source=".agents/skills/builtin-a",
            trust="unreviewed",
            installed_at=datetime.now(UTC).isoformat(),
        )
    )
    assert cmd_skill_update(["builtin-a"], repo=repo) == 0
    assert _entry("builtin-a").trust == "builtin"


def test_update_unchanged_reanchors_stale_origin_path(unit_home: Path, repo: Path, capsys) -> None:
    """An up-to-date copy whose recorded origin_path points at a deleted
    worktree gets re-anchored to the current source (no-op pass)."""
    cmd_skill_update(None, repo=repo)
    e = _entry("builtin-a")
    e.origin_path = "/Users/x/Ava/.worktrees/ava-dead/.agents/skills/builtin-a"
    reg.save(reg.load())  # persist
    # after reload, update the entry
    pkg = reg.get("builtin-a")
    assert pkg is not None
    pkg.origin_path = "/Users/x/Ava/.worktrees/ava-dead/.agents/skills/builtin-a"
    reg.save(reg.load())
    assert cmd_skill_update(None, repo=repo) == 0
    assert _entry("builtin-a").origin_path == str(repo / "ava_builtins" / "skills" / "builtin-a")
