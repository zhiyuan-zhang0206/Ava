"""skills converge -- repo/plugin skills sync to the single `~/.ava/skills/` load directory.

Uses synthetic repo tree + `unit_home` isolation; asserts disk copy, registry entry, and scanner
visibility all agree. `ava skill enable/disable/register` registry toggles are also tested here.
"""

from pathlib import Path

import pytest

import ava.skills as skills_mod
from cli.commands._converge_skills import converge_skills
from cli.commands.skill import cmd_skill_disable, cmd_skill_enable, cmd_skill_register
from shared import install_registry as reg


def _write_skill(root: Path, dirname: str, name: str | None = None, body: str = "# B\n") -> Path:
    d = root / dirname
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name or dirname}\ndescription: d\n---\n\n{body}", encoding="utf-8"
    )
    return d


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A synthetic source tree: one repo skill + one built-in plugin skill."""
    repo = tmp_path / "repo"
    _write_skill(repo / "ava_builtins" / "skills", "goal")
    _write_skill(repo / "ava_builtins" / "plugins" / "superpowers" / "skills", "brainstorming")
    return repo


def _loaded_names(unit_home: Path) -> set[str]:
    return {s["name"] for s in skills_mod._names()}


def _entry(name: str) -> reg.InstalledPackage:
    pkg = reg.get(name)
    assert pkg is not None
    return pkg


# ─── first converge / idempotence ──────────────────────────────────────────


def test_first_converge_copies_registers_and_surfaces(unit_home: Path, fake_repo: Path) -> None:
    result = converge_skills(fake_repo, unit_home)
    assert sorted(result.copied) == ["goal", "superpowers"]
    assert result.warnings == []

    assert (unit_home / "skills" / "goal" / "SKILL.md").is_file()
    assert (unit_home / "skills" / "superpowers" / "brainstorming" / "SKILL.md").is_file()

    goal = reg.get("goal")
    assert goal is not None and goal.origin == "repo" and goal.enabled
    assert goal.content_hash and goal.installed_at
    sp = reg.get("superpowers")
    assert sp is not None and sp.origin == "plugin" and sp.type == "skill"

    assert _loaded_names(unit_home) == {"goal", "brainstorming"}


def test_second_converge_is_noop(unit_home: Path, fake_repo: Path) -> None:
    converge_skills(fake_repo, unit_home)
    first = reg.load().model_dump_json()
    result = converge_skills(fake_repo, unit_home)
    assert result.copied == [] and result.updated == [] and result.removed == []
    assert sorted(result.unchanged) == ["goal", "superpowers"]
    assert reg.load().model_dump_json() == first  # updated_at untouched on no-op


# ─── source updates / user edits ───────────────────────────────────────────


def test_repo_source_change_not_propagated_by_converge(unit_home: Path, fake_repo: Path) -> None:
    """Repo-native sources are bootstrap-only (R5 ruling): a source change is
    NOT synced by converge — the explicit `ava skill update` owns updates."""
    converge_skills(fake_repo, unit_home)
    old_hash = _entry("goal").content_hash
    (fake_repo / "ava_builtins" / "skills" / "goal" / "SKILL.md").write_text(
        "---\nname: goal\ndescription: v2\n---\n\n# v2\n", encoding="utf-8"
    )
    result = converge_skills(fake_repo, unit_home)
    assert result.updated == []
    assert "goal" in result.unchanged
    body = (unit_home / "skills" / "goal" / "SKILL.md").read_text(encoding="utf-8")
    assert "# v2" not in body
    assert _entry("goal").content_hash == old_hash


def test_repo_copy_user_edit_untouched_by_converge(unit_home: Path, fake_repo: Path) -> None:
    """A user-edited repo-native copy is left alone, silently: converge no
    longer attempts to update repo-native copies at all, so the edit is not a
    converge concern — `ava skill update` reports the conflict."""
    converge_skills(fake_repo, unit_home)
    copy = unit_home / "skills" / "goal" / "SKILL.md"
    copy.write_text("---\nname: goal\ndescription: MINE\n---\n\nhands off\n", encoding="utf-8")
    (fake_repo / "ava_builtins" / "skills" / "goal" / "SKILL.md").write_text(
        "---\nname: goal\ndescription: v2\n---\n", encoding="utf-8"
    )
    result = converge_skills(fake_repo, unit_home)
    assert result.updated == []
    assert "goal" in result.unchanged
    assert not any("modified locally" in w for w in result.warnings)
    assert "hands off" in copy.read_text(encoding="utf-8")


# ─── source removal ─────────────────────────────────────────────────────────


def test_source_removed_cleans_untouched_copy(unit_home: Path, fake_repo: Path) -> None:
    import shutil

    converge_skills(fake_repo, unit_home)
    shutil.rmtree(fake_repo / "ava_builtins" / "skills" / "goal")
    result = converge_skills(fake_repo, unit_home)
    assert result.removed == ["goal"]
    assert not (unit_home / "skills" / "goal").exists()
    assert reg.get("goal") is None


def test_source_removed_keeps_edited_copy(unit_home: Path, fake_repo: Path) -> None:
    import shutil

    converge_skills(fake_repo, unit_home)
    (unit_home / "skills" / "goal" / "SKILL.md").write_text(
        "---\nname: goal\ndescription: MINE\n---\n", encoding="utf-8"
    )
    shutil.rmtree(fake_repo / "ava_builtins" / "skills" / "goal")
    result = converge_skills(fake_repo, unit_home)
    assert result.removed == []
    assert any("is gone" in w for w in result.warnings)
    assert (unit_home / "skills" / "goal").exists()
    assert reg.get("goal") is not None


# ─── untracked / conflicts ──────────────────────────────────────────────────


def test_untracked_dir_warned_and_not_loaded(unit_home: Path, fake_repo: Path) -> None:
    _write_skill(unit_home / "skills", "stray")
    result = converge_skills(fake_repo, unit_home)
    assert any("'stray'" in w and "not loaded" in w for w in result.warnings)
    assert "stray" not in _loaded_names(unit_home)


def test_lost_registry_row_adopted_when_copy_matches_source(
    unit_home: Path, fake_repo: Path
) -> None:
    """A wiped registry must not strand converge-managed copies forever.

    Regression for the P0 (frontend commands + skills vanished): when
    installed.json loses its repo/plugin rows (rewritten by a rollout),
    the load-dir copies are still byte-identical to their sources. Converge
    adopts them back instead of warning 'untracked shadows' — which left the
    registry empty and the skill scanner gating everything out."""
    converge_skills(fake_repo, unit_home)
    assert reg.get("goal") is not None

    # Simulate the registry loss: keep only a user row.
    reg.save(reg.Registry(packages=[reg.InstalledPackage(name="mine", type="skill")]))
    assert reg.get("goal") is None

    result = converge_skills(fake_repo, unit_home)
    assert not any("shadows" in w for w in result.warnings)
    adopted = reg.get("goal")
    assert adopted is not None
    assert adopted.origin == "repo" and adopted.content_hash
    assert "goal" in _loaded_names(unit_home)


def test_lost_registry_row_different_copy_still_warned(unit_home: Path, fake_repo: Path) -> None:
    """A content-differing untracked copy stays a warning — it may be a
    user's deliberate edit, and adopting it would let converge overwrite it."""
    converge_skills(fake_repo, unit_home)
    (unit_home / "skills" / "goal" / "SKILL.md").write_text(
        "---\nname: goal\ndescription: EDITED\n---\n", encoding="utf-8"
    )
    reg.save(reg.Registry(packages=[]))
    result = converge_skills(fake_repo, unit_home)
    assert any("shadows" in w and "content differs" in w for w in result.warnings)
    assert reg.get("goal") is None
    assert "goal" not in _loaded_names(unit_home)


def _web_sources_repo(tmp_path: Path) -> Path:
    """A repo with a web-sources skill + local adapter subdirs in the load dir."""
    repo = tmp_path / "repo-ws"
    _write_skill(repo / "ava_builtins" / "skills", "web-sources")
    return repo


def test_preserved_subtrees_survive_bootstrap_only(unit_home: Path, tmp_path: Path) -> None:
    """Local adapters (web-sources/people, web-sources/rmrb) inside a managed
    package survive converge: repo-native packages are bootstrap-only, so the
    copy — local adapters included — is never rewritten by converge."""
    repo = _web_sources_repo(tmp_path)
    converge_skills(repo, unit_home)
    for sub in ("people", "rmrb"):
        d = unit_home / "skills" / "web-sources" / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {sub}\ndescription: local\n---\n", encoding="utf-8"
        )
        (d / ".preserved").touch()
    (repo / "ava_builtins" / "skills" / "web-sources" / "SKILL.md").write_text(
        "---\nname: web-sources\ndescription: d2\n---\n\nnew content\n",
        encoding="utf-8",
    )
    result = converge_skills(repo, unit_home)
    assert result.updated == []
    assert "web-sources" in result.unchanged
    # Converge left the copy (and the adapters) alone; updating to the new
    # source is `ava skill update`'s job.
    body = (unit_home / "skills" / "web-sources" / "SKILL.md").read_text(encoding="utf-8")
    assert "new content" not in body
    for sub in ("people", "rmrb"):
        assert (unit_home / "skills" / "web-sources" / sub / "SKILL.md").exists()


def test_preserved_only_drift_reads_unchanged(unit_home: Path, tmp_path: Path) -> None:
    """A package whose only drift is preserved local content is in-sync: no
    warning, no freeze, no rewrite."""
    repo = _web_sources_repo(tmp_path)
    converge_skills(repo, unit_home)
    d = unit_home / "skills" / "web-sources" / "people"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: people\ndescription: local\n---\n", encoding="utf-8")
    (d / ".preserved").touch()
    result = converge_skills(repo, unit_home)
    assert "web-sources" in result.unchanged
    assert not any("modified locally" in w for w in result.warnings)
    assert (unit_home / "skills" / "web-sources" / "people" / "SKILL.md").exists()


def test_cleanup_keeps_source_vanish_with_preserved_subtree(
    unit_home: Path, tmp_path: Path
) -> None:
    """A source-vanished package holding preserved local content is kept with
    a warning, not silently removed."""
    import shutil

    repo = _web_sources_repo(tmp_path)
    converge_skills(repo, unit_home)
    d = unit_home / "skills" / "web-sources" / "people"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: people\ndescription: local\n---\n", encoding="utf-8")
    (d / ".preserved").touch()
    shutil.rmtree(repo / "ava_builtins" / "skills" / "web-sources")
    result = converge_skills(repo, unit_home)
    assert result.removed == []
    assert any("preserved local content" in w for w in result.warnings)
    assert (unit_home / "skills" / "web-sources" / "people" / "SKILL.md").exists()


def test_user_install_shadowing_source_kept(unit_home: Path, fake_repo: Path) -> None:
    """A user-installed package squatting a repo skill's name wins; the source
    is not synced (never destroy user content)."""
    _write_skill(unit_home / "skills", "goal", body="user version\n")
    reg.register(reg.InstalledPackage(name="goal", type="skill", source="file:///x"))
    result = converge_skills(fake_repo, unit_home)
    assert any("shadows" in w for w in result.warnings)
    copy = unit_home / "skills" / "goal" / "SKILL.md"
    assert "user version" in copy.read_text(encoding="utf-8")
    assert _entry("goal").origin == "user"


def test_same_name_repo_beats_plugin(unit_home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo2"
    _write_skill(repo / "ava_builtins" / "skills", "dup", body="repo version\n")
    _write_skill(repo / "ava_builtins" / "plugins" / "dup" / "skills", "inner")
    result = converge_skills(repo, unit_home)
    assert any("already provided" in w for w in result.warnings)
    assert "repo version" in (unit_home / "skills" / "dup" / "SKILL.md").read_text(encoding="utf-8")


# ─── installed plugins (~/.ava/plugins/<p>/skills/) ─────────────────────────


def test_installed_plugin_skills_gate_on_plugin_entry(unit_home: Path, fake_repo: Path) -> None:
    """An installed plugin's skills sync under its name and gate on the
    plugin's own registry entry — no duplicate type='skill' row."""
    _write_skill(unit_home / "plugins" / "pr-kit" / "skills", "review")
    reg.register(reg.InstalledPackage(name="pr-kit", type="plugin", source="file:///y"))
    result = converge_skills(fake_repo, unit_home)
    assert "pr-kit" in result.copied
    entry = _entry("pr-kit")
    assert entry.type == "plugin" and entry.origin == "plugin" and entry.content_hash
    assert [p.name for p in reg.load().packages].count("pr-kit") == 1
    assert "review" in _loaded_names(unit_home)

    assert cmd_skill_disable("pr-kit") == 0
    assert "review" not in _loaded_names(unit_home)
    assert cmd_skill_enable("pr-kit") == 0
    assert "review" in _loaded_names(unit_home)


# ─── ava skill enable / disable / register ──────────────────────────────────


def test_skill_disable_enable_roundtrip(unit_home: Path, fake_repo: Path) -> None:
    converge_skills(fake_repo, unit_home)
    assert cmd_skill_disable("goal") == 0
    assert _entry("goal").enabled is False
    assert "goal" not in _loaded_names(unit_home)
    assert (unit_home / "skills" / "goal").exists()  # installed ≠ enabled
    assert cmd_skill_enable("goal") == 0
    assert "goal" in _loaded_names(unit_home)


def test_skill_enable_unknown_errors(unit_home: Path) -> None:
    assert cmd_skill_enable("nope") == 1


def test_skill_register_tracks_stray_dir(unit_home: Path, fake_repo: Path) -> None:
    _write_skill(unit_home / "skills", "stray")
    assert cmd_skill_register("stray") == 0
    entry = reg.get("stray")
    assert entry is not None and entry.origin == "user" and entry.enabled
    assert "stray" in _loaded_names(unit_home)
    # converge leaves it alone afterwards
    result = converge_skills(fake_repo, unit_home)
    assert all("stray" not in w for w in result.warnings)


def test_skill_register_missing_dir_errors(unit_home: Path) -> None:
    assert cmd_skill_register("ghost") == 1


def test_skill_register_bad_skill_md_errors(unit_home: Path) -> None:
    d = unit_home / "skills" / "bad"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
    assert cmd_skill_register("bad") == 1
    assert reg.get("bad") is None


# ─── atomic copy staging / shared predicate (audit #3, #8) ──────────────────


def test_copy_staging_dirs_invisible_after_update(unit_home: Path, fake_repo: Path) -> None:
    """A re-copy stages through dot-prefixed temp/trash siblings: none linger
    afterwards, and dot-prefixed strays are never warned as untracked (audit #3).
    Installed-plugin skills are still synced on change (the repo-native half of
    the load dir is bootstrap-only), so this exercises the update path."""
    _write_skill(unit_home / "plugins" / "pr-kit" / "skills", "review")
    reg.register(reg.InstalledPackage(name="pr-kit", type="plugin", source="file:///y"))
    converge_skills(fake_repo, unit_home)
    (unit_home / "plugins" / "pr-kit" / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: v2\n---\n\n# v2\n", encoding="utf-8"
    )
    result = converge_skills(fake_repo, unit_home)
    assert "pr-kit" in result.updated
    staging = [p.name for p in (unit_home / "skills").iterdir() if p.name.startswith(".")]
    assert staging == []
    assert not any(w.startswith("'.") for w in result.warnings)


def test_dot_dir_untracked_not_warned(unit_home: Path, fake_repo: Path) -> None:
    (unit_home / "skills" / ".staging-leftover").mkdir(parents=True)
    result = converge_skills(fake_repo, unit_home)
    assert all(".staging-leftover" not in w for w in result.warnings)
    assert not (unit_home / "skills" / ".staging-leftover" / "SKILL.md").exists()


def test_contains_skill_md_any_depth(unit_home: Path, tmp_path: Path) -> None:
    """The shared skill-bearing-tree predicate sees nested-only trees (the
    ava_code/ava_fleet plugin shape) and ignores SKILL.md-less dirs (audit #8)."""
    from cli.commands._skill_package import contains_skill_md

    nested = tmp_path / "nested"
    (nested / "pr").mkdir(parents=True)
    (nested / "pr" / "SKILL.md").write_text("---\nname: pr\ndescription: d\n---\n")
    assert contains_skill_md(nested)
    assert contains_skill_md(nested / "pr")

    empty = tmp_path / "empty"
    empty.mkdir()
    assert not contains_skill_md(empty)


# ─── .agents/skills is NOT a converge source (issue #146) ───────────────────


def _agents_repo(tmp_path: Path) -> Path:
    """A repo carrying one builtin skill, one .agents project skill, and a
    symlinked builtin mirror inside .agents/skills (the real-world shape)."""
    repo = tmp_path / "repo-agents"
    _write_skill(repo / "ava_builtins" / "skills", "builtin-a")
    _write_skill(repo / ".agents" / "skills", "project-x")
    # Builtin mirror: a symlink back to ava_builtins/skills (the open-standard
    # path exposing builtins to other clients). Converge must not enumerate
    # .agents/skills at all, so the link is irrelevant to the load dir.
    (repo / ".agents" / "skills" / "builtin-a").symlink_to(
        repo / "ava_builtins" / "skills" / "builtin-a", target_is_directory=True
    )
    return repo


def test_agents_project_skills_not_converged(unit_home: Path, tmp_path: Path) -> None:
    """Kernel-contributor project skills reach agents only through the
    project-local mount (project_skill_roots), never through the fleet-wide
    load dir: converge lands nothing from .agents/skills and records no row."""
    repo = _agents_repo(tmp_path)
    result = converge_skills(repo, unit_home)
    assert sorted(result.copied) == ["builtin-a"]
    assert result.removed == []
    assert not any("already provided" in w for w in result.warnings)
    assert not (unit_home / "skills" / "project-x").exists()
    assert reg.get("project-x") is None


def test_legacy_agents_skill_converge_copy_cleaned_up(unit_home: Path, tmp_path: Path) -> None:
    """The fleet transition (issue #146): machines that converged
    .agents/skills before the stop carry a repo-origin copy + registry row.
    Once the source is gone, an untouched copy is derived state -> removed and
    deregistered, so runtime agents' indexes lose the L4 noise."""
    import shutil

    from shared.install_registry import InstalledPackage, tree_hash

    repo = _agents_repo(tmp_path)
    # Pre-#146 state, as converge used to write it: copy + repo row.
    src = repo / ".agents" / "skills" / "project-x"
    dest = unit_home / "skills" / "project-x"
    shutil.copytree(src, dest)
    reg.register(
        InstalledPackage(
            name="project-x",
            type="skill",
            origin="repo",
            origin_path=str(src),
            trust="builtin",
            content_hash=tree_hash(src),
            installed_at="2026-08-20T00:00:00+00:00",
        )
    )
    result = converge_skills(repo, unit_home)
    assert result.removed == ["project-x"]
    assert not dest.exists()
    assert reg.get("project-x") is None
    # The real builtin still converges alongside.
    assert (unit_home / "skills" / "builtin-a" / "SKILL.md").is_file()


# ─── identity fold (design R2-B / audit 02 #4) ─────────────────────────────


def test_dash_source_row_matches_underscore_registry_entry(
    unit_home: Path, fake_repo: Path
) -> None:
    """A registry row written `ava_goal` (legacy spelling) and a source
    `ava-goal/` are one skill: converge must match them through the fold, not
    raw names, or the row would never be updated (audit 02 #4 bare
    comparison in `_sync_one`)."""
    _write_skill(fake_repo / "ava_builtins" / "skills", "ava-goal")
    converge_skills(fake_repo, unit_home)
    entry = reg.get("ava-goal")
    assert entry is not None
    # Hand-rewrite the row to the legacy underscore spelling, then converge
    # again: the row must be found and updated, not duplicated.
    registry = reg.load()
    for pkg in registry.packages:
        if pkg.name == "ava-goal":
            pkg.name = "ava_goal"
    reg.save(registry)
    result = converge_skills(fake_repo, unit_home)
    # the legacy-spelled row was FOUND through the fold and kept up to date —
    # no second row was created for the canonical spelling
    assert "ava-goal" in result.unchanged
    assert result.copied == [] and result.updated == []
    rows = reg.load().packages
    assert len(rows) == 3  # goal + superpowers + ava-goal — no dual-row state
    assert reg.get("ava-goal") is not None  # dash query finds the underscore row
    assert len([p for p in rows if p.name in ("ava-goal", "ava_goal")]) == 1


def test_cross_source_same_key_conflict_is_reported_not_duplicated(
    unit_home: Path, fake_repo: Path
) -> None:
    """A repo skill `foo-bar/` and a plugin skill `foo_bar/` fold to one key:
    the dual-row state the registry read now refuses. Converge reports the
    conflict instead of writing both rows."""
    _write_skill(fake_repo / "ava_builtins" / "skills", "foo-bar")
    _write_skill(fake_repo / "ava_builtins" / "plugins" / "foo_bar" / "skills", "sub")
    result = converge_skills(fake_repo, unit_home)
    assert any("already provided" in w for w in result.warnings)
    rows = reg.load().packages
    assert len([p for p in rows if p.name in ("foo-bar", "foo_bar")]) == 1


# ─── worktree source bound (audit round 2, skills-plugins #3) ──────────────


def test_worktree_repo_refused_for_default_home(
    unit_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree checkout's sources must never sync into the prod home's load
    dir: the branch content (possibly unmerged) would silently replace what
    main ships (observed 2026-08-08 with ava-serious-research)."""

    monkeypatch.setattr("shared.cluster.derive.default_home", lambda: unit_home)
    repo = tmp_path / "repo" / ".worktrees" / "ava-9999-task"
    _write_skill(repo / "ava_builtins" / "skills", "goal")
    with pytest.raises(RuntimeError, match="worktree"):
        converge_skills(repo, unit_home)


def test_worktree_repo_allowed_for_dev_home(unit_home: Path, tmp_path: Path) -> None:
    """A dev worktree cluster owns its own home (`~/.ava-<dir>`), so syncing
    its checkout into that home is the normal path and stays allowed."""
    repo = tmp_path / "repo" / ".worktrees" / "ava-9999-task"
    _write_skill(repo / "ava_builtins" / "skills", "goal")
    dev_home = tmp_path / "dev-home"
    result = converge_skills(repo, dev_home)
    assert result.copied == ["goal"]
    assert (dev_home / "skills" / "goal" / "SKILL.md").is_file()


def test_worktree_refusal_overridable_by_env(
    unit_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setattr("shared.cluster.derive.default_home", lambda: unit_home)
    monkeypatch.setenv("AVA_CONVERGE_ALLOW_WORKTREE", "1")
    repo = tmp_path / "repo" / ".worktrees" / "ava-9999-task"
    _write_skill(repo / "ava_builtins" / "skills", "goal")
    result = converge_skills(repo, unit_home)
    assert result.copied == ["goal"]


def test_unmarked_local_subtree_not_preserved(unit_home: Path, tmp_path: Path) -> None:
    """A local subtree WITHOUT the .preserved marker is not protected: the
    marker is the single source of truth for preserve-on-recopy (audit 02
    #11) — a new adapter that forgets it gets cleaned, visibly, rather than
    a global registry silently missing the entry."""

    repo = _web_sources_repo(tmp_path)
    converge_skills(repo, unit_home)
    d = unit_home / "skills" / "web-sources" / "newadapter"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: newadapter\ndescription: local\n---\n", encoding="utf-8"
    )
    # 包源变更 → converge 不会更新（bootstrap-only），所以直接验证 tree_hash 与 _copy_tree 语义：
    # 未标记子树计入 drift（不是 preserved）
    skip = reg.preserved_subpaths(unit_home / "skills" / "web-sources")
    assert skip == frozenset()  # no marker, no protection
    assert reg.tree_hash(unit_home / "skills" / "web-sources") != reg.tree_hash(
        repo / "ava_builtins" / "skills" / "web-sources"
    )
