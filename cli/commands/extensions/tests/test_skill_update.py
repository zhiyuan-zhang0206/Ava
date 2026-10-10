"""`ava skill update` / `ava skill upgrade` — the R5 explicit-update commands.

Repo-native skills update via `skill update` (converge only lands missing
copies); user-installed skills with a recorded git source update via
`skill upgrade`. Both share one contract: a locally edited copy is replaced,
and the replacement is reported (local copies are never hand-edited).
"""

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.config import ConfigBoot
from base.packages.extensions import install_registry as reg
from cli.commands.extensions.skill import cmd_skill_update, cmd_skill_upgrade
from tests.path_scoped.cli_tests import operator_database as operator_database

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


def _set_mode_off(name: str) -> None:
    """Opt a package out of the content channel — the designed escape hatch
    that keeps `skill update` applying checkout content (design §5.7-1)."""
    with reg.mutate() as registry:
        row = next(p for p in registry.packages if p.name == name)
        row.update.mode = "off"


# ─── skill update: bootstrap ────────────────────────────────────────────────


def test_update_lands_missing_and_reports(
    process_config: ConfigBoot, unit_home: Path, repo: Path, capsys
) -> None:
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "landed 'builtin-a'" in out
    assert "project-x" not in out
    assert (unit_home / "skills" / "builtin-a" / "SKILL.md").is_file()
    assert not (unit_home / "skills" / "project-x").exists()
    assert reg.get("project-x") is None


def test_update_agents_project_skill_is_not_repo_native(
    process_config: ConfigBoot, unit_home: Path, repo: Path, capsys
) -> None:
    """`.agents/skills` skills are no longer repo-native sources (issue #146):
    update reports them as unknown and never lands a copy."""
    assert cmd_skill_update(["project-x"], config=process_config, repo=repo) == 0
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "'project-x' is not a repo-native skill" in err
    assert not (unit_home / "skills" / "project-x").exists()


def test_update_unknown_name_errors(
    process_config: ConfigBoot, unit_home: Path, repo: Path, capsys
) -> None:
    assert (
        cmd_skill_update(["nope"], config=process_config, repo=repo) == 0
    )  # others still run; unknown reported
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "'nope' is not a repo-native skill" in err


# ─── skill update: source change / local edits ──────────────────────────────


def test_update_propagates_source_change(
    process_config: ConfigBoot, unit_home: Path, repo: Path
) -> None:
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    _set_mode_off("builtin-a")
    (repo / "ava_builtins" / "skills" / "builtin-a" / "SKILL.md").write_text(
        "---\nname: builtin-a\ndescription: v2\n---\n\n# v2\n", encoding="utf-8"
    )
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    body = (unit_home / "skills" / "builtin-a" / "SKILL.md").read_text(encoding="utf-8")
    assert "# v2" in body


def test_update_replaces_a_local_edit_and_reports(
    process_config: ConfigBoot, unit_home: Path, repo: Path, capsys
) -> None:
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    _set_mode_off("builtin-a")
    copy = unit_home / "skills" / "builtin-a" / "SKILL.md"
    copy.write_text("---\nname: builtin-a\ndescription: MINE\n---\n\nhands off\n", encoding="utf-8")
    (repo / "ava_builtins" / "skills" / "builtin-a" / "SKILL.md").write_text(
        "---\nname: builtin-a\ndescription: v2\n---\n", encoding="utf-8"
    )
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "note:" in out and "local copy differed" in out
    body = copy.read_text(encoding="utf-8")
    assert "hands off" not in body and "v2" in body


def test_update_restores_a_local_edit_without_a_source_change(
    process_config: ConfigBoot, unit_home: Path, repo: Path, capsys
) -> None:
    """Local edits alone (no upstream change) are converged too: the source
    tree is restored and the replacement is reported."""
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    _set_mode_off("builtin-a")
    copy = unit_home / "skills" / "builtin-a" / "SKILL.md"
    copy.write_text("---\nname: builtin-a\ndescription: MINE\n---\n\nhands off\n", encoding="utf-8")
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "note:" in out and "local copy differed" in out
    body = copy.read_text(encoding="utf-8")
    assert "hands off" not in body and "description: d" in body


# ─── skill update: adoption of hand-installed .agents residue ───────────────


def test_update_adopts_matching_user_residue(
    process_config: ConfigBoot, unit_home: Path, repo: Path
) -> None:
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
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    entry = _entry("builtin-a")
    assert entry.origin == "repo" and entry.source is None
    assert (unit_home / "skills" / "builtin-a" / "SKILL.md").exists()


def test_update_converges_a_diverged_user_residue(
    process_config: ConfigBoot, unit_home: Path, repo: Path, capsys
) -> None:
    _write_skill(unit_home / "skills", "builtin-a", body="# user hacked\n")
    reg.register(
        reg.InstalledPackage(
            name="builtin-a", type="skill", source=".agents/skills/builtin-a", origin="user"
        )
    )
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "note:" in out and "local copy differed" in out
    assert _entry("builtin-a").origin == "repo"
    body = (unit_home / "skills" / "builtin-a" / "SKILL.md").read_text(encoding="utf-8")
    assert "user hacked" not in body


def test_update_leaves_third_party_user_package_alone(
    process_config: ConfigBoot, unit_home: Path, repo: Path, capsys
) -> None:
    """A genuinely third-party user install squatting a repo name is shadowed
    by converge; update must not adopt it either."""
    _write_skill(unit_home / "skills", "builtin-a", body="# third party\n")
    reg.register(
        reg.InstalledPackage(
            name="builtin-a", type="skill", source="https://example.com/skills.git", origin="user"
        )
    )
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
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
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)


def _skill_git_repo(tmp_path: Path, name: str = "ext-skill") -> str:
    """A git repo holding one bare skill; returns its URL."""
    r = tmp_path / "ext-src"
    _write_skill(r, name, body="# v1\n")
    _git(r, "init", "-q")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "v1")
    return str(r)


def _install_skill(
    url: str, name: str = "ext-skill", *, operator_database: Callable[[], Any]
) -> None:
    from cli.commands.extensions.skill import cmd_skill_install

    assert cmd_skill_install(url, None, None, database_factory=operator_database) == 0
    assert _entry(name).installed_hash is not None


def test_upgrade_skips_unupdatable(unit_home: Path, capsys) -> None:
    _write_skill(unit_home / "skills", "local-skill")
    reg.register(reg.InstalledPackage(name="local-skill", type="skill"))  # no source
    assert cmd_skill_upgrade("local-skill") == 1
    assert "no recorded source" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_upgrade_refetches_from_source(
    unit_home: Path, tmp_path: Path, operator_database: Callable[[], Any]
) -> None:
    url = _skill_git_repo(tmp_path)
    _install_skill(url, operator_database=operator_database)
    r = Path(url)
    (r / "SKILL.md").write_text(
        "---\nname: ext-skill\ndescription: d\n---\n\n# v2\n", encoding="utf-8"
    )
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "v2")
    assert cmd_skill_upgrade("ext-skill") == 0
    body = (unit_home / "skills" / "ext-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "# v2" in body


def test_upgrade_local_source_is_copied_never_moved(
    unit_home: Path, tmp_path: Path, operator_database: Callable[[], Any]
) -> None:
    """A local-path source is read in place (never moved): upgrade must not
    relocate the user's own directory or delete its .git (install's docstring
    promises 'never moved' — the old upgrade moved it into $AVA_HOME/skills
    and rmtree'd the checkout's .git). The installed copy updates; the source
    stays put."""
    src_dir = Path(_skill_git_repo(tmp_path))  # a local dir WITH .git
    _install_skill(str(src_dir), operator_database=operator_database)

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


def test_upgrade_replaces_a_local_edit(
    unit_home: Path, tmp_path: Path, capsys, operator_database: Callable[[], Any]
) -> None:
    url = _skill_git_repo(tmp_path)
    _install_skill(url, operator_database=operator_database)
    copy = unit_home / "skills" / "ext-skill" / "SKILL.md"
    copy.write_text("---\nname: ext-skill\ndescription: d\n---\n\n# hacked\n", encoding="utf-8")
    r = Path(url)
    (r / "SKILL.md").write_text(
        "---\nname: ext-skill\ndescription: d\n---\n\n# v2\n", encoding="utf-8"
    )
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "v2")

    assert cmd_skill_upgrade("ext-skill") == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "modified locally" in out
    body = copy.read_text(encoding="utf-8")
    assert "# hacked" not in body and "# v2" in body


# ─── worktree source bound (audit round 2, skills-plugins #3) ───────────────


def test_update_refuses_worktree_repo_for_default_home(
    process_config: ConfigBoot,
    unit_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    """`ava skill update` from a worktree checkout must not write the prod home
    (the R5 worktree that synced ava-serious-research into prod)."""

    monkeypatch.setattr("base.cluster.derive.default_home", lambda: unit_home)
    wt_repo = tmp_path / "repo" / ".worktrees" / "ava-9999-task"
    _write_skill(wt_repo / "ava_builtins" / "skills", "builtin-a")
    assert cmd_skill_update(None, config=process_config, repo=wt_repo) == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "worktree" in err
    assert not (unit_home / "skills" / "builtin-a").exists()


# ─── adopt trust + stale origin_path re-anchor (audit 02 #5/#15) ────────────


def test_update_adopt_sets_builtin_trust(
    process_config: ConfigBoot, unit_home: Path, repo: Path
) -> None:
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
    assert cmd_skill_update(["builtin-a"], config=process_config, repo=repo) == 0
    assert _entry("builtin-a").trust == "builtin"


def test_update_unchanged_reanchors_stale_origin_path(
    process_config: ConfigBoot, unit_home: Path, repo: Path, capsys
) -> None:
    """An up-to-date copy whose recorded origin_path points at a deleted
    worktree gets re-anchored to the current source (no-op pass)."""
    cmd_skill_update(None, config=process_config, repo=repo)
    e = _entry("builtin-a")
    e.origin_path = "/Users/x/Ava/.worktrees/ava-dead/.agents/skills/builtin-a"
    reg.save(reg.load())  # persist
    # after reload, update the entry
    pkg = reg.get("builtin-a")
    assert pkg is not None
    pkg.origin_path = "/Users/x/Ava/.worktrees/ava-dead/.agents/skills/builtin-a"
    reg.save(reg.load())
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    assert _entry("builtin-a").origin_path == str(repo / "ava_builtins" / "skills" / "builtin-a")


def test_update_skips_channel_managed_packages(
    process_config: ConfigBoot, unit_home: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Resolved core-channel rows belong to `ava packages refresh` (design
    §5.7-1): `skill update` reports the skip and leaves the copy alone, and an
    explicit `mode=off` opts the package back onto the checkout path."""
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    capsys.readouterr()
    (repo / "ava_builtins" / "skills" / "builtin-a" / "SKILL.md").write_text(
        "---\nname: builtin-a\ndescription: v2\n---\n\n# v2\n", encoding="utf-8"
    )
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "channel-managed" in out and "skipped" in out
    body = (unit_home / "skills" / "builtin-a" / "SKILL.md").read_text(encoding="utf-8")
    assert "# v2" not in body
    _set_mode_off("builtin-a")
    assert cmd_skill_update(None, config=process_config, repo=repo) == 0
    body = (unit_home / "skills" / "builtin-a" / "SKILL.md").read_text(encoding="utf-8")
    assert "# v2" in body
