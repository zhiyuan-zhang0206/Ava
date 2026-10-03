"""ava.skills mount projection: hash-based dedup across roots, auto-promote of a same-named child, dash/underscore folding, and the merged-traversal gate regressions; split from ava/tests/test_skills.py (task #4922)."""

from pathlib import Path

import pytest

import ava.skills as skills_mod
from ava.sdk_surface import skill_sources
from ava.tests._skills_helpers import _overlay_all_enabled as _overlay_all_enabled
from ava.tests._skills_helpers import _write_skill
from ava.tests._skills_helpers import fake_skills_dir as fake_skills_dir
from base.packages.extensions import install_registry

# Every test runs in a per-test unit home whose `skills/` does not exist by
# default, so the real ~/.agents/skills/ never leaks into a scan; the
# fake_skills_dir fixture creates the load dir.
pytestmark = pytest.mark.usefixtures("unit_home")


# ─── hash-based dedup ─────────────────────────────────────────────────────


def test_mount_dedup_by_content_hash_across_roots(fake_skills_dir: Path, tmp_path: Path) -> None:
    """Two SKILL.md files at different roots with identical content → only
    the first is loaded; the second is skipped by content-hash dedup. This is
    the .claude/skills + .agents/skills case — a project-local skill appearing in
    both directories."""
    # First root (.claude/skills equivalent)
    root_a = tmp_path / "root_a"
    skill_a = root_a / "my-skill"
    skill_a.mkdir(parents=True)
    (skill_a / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Shared skill content\n---\n\n# Body\n",
        encoding="utf-8",
    )

    # Second root (.agents/skills equivalent) — same content, different path
    root_b = tmp_path / "root_b"
    skill_b = root_b / "my-skill"
    skill_b.mkdir(parents=True)
    (skill_b / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Shared skill content\n---\n\n# Body\n",
        encoding="utf-8",
    )

    skills = skills_mod.skills_in([root_a, root_b])
    # Only one skill — the second was skipped by hash dedup
    assert len(skills) == 1
    assert skills[0]["name"] == "my-skill"


def test_mount_hash_dedup_respects_different_content(tmp_path: Path) -> None:
    """Two SKILL.md files at different roots with different content → both
    are loaded. Hash dedup must not collapse distinct skills."""
    root_a = tmp_path / "root_a"
    skill_a = root_a / "skill_a"
    skill_a.mkdir(parents=True)
    (skill_a / "SKILL.md").write_text(
        "---\nname: skill-a\ndescription: Content A\n---\n\n# Body A\n",
        encoding="utf-8",
    )

    root_b = tmp_path / "root_b"
    skill_b = root_b / "skill_b"
    skill_b.mkdir(parents=True)
    (skill_b / "SKILL.md").write_text(
        "---\nname: skill-b\ndescription: Content B\n---\n\n# Body B\n",
        encoding="utf-8",
    )

    skills = skills_mod.skills_in([root_a, root_b])
    # Both loaded — content differs
    assert len(skills) == 2
    names = {s["name"] for s in skills}
    assert names == {"skill-a", "skill-b"}


def test_mount_hash_dedup_does_not_affect_single_root(fake_skills_dir: Path) -> None:
    """Single root with unique skills — hash dedup is a no-op; all skills
    are loaded normally."""
    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")
    _write_skill(fake_skills_dir, "beta", "name: beta\ndescription: b")
    # Use skills_in with a single root (simulates _scan_tree's per-root calls
    # with a shared seen_hashes set — here the set is fresh per skills_in).
    skills = skills_mod.skills_in([fake_skills_dir])
    assert len(skills) == 2
    names = {s["name"] for s in skills}
    assert names == {"alpha", "beta"}


def test_mount_hash_dedup_skips_third_identical_copy(tmp_path: Path) -> None:
    """Three roots, all with the same SKILL.md content → only the first is
    loaded."""
    content = "---\nname: triple\ndescription: Same content everywhere\n---\n\n# Shared\n"
    roots: list[Path] = []
    for i in range(3):
        root = tmp_path / f"root_{i}"
        skill_dir = root / "triple"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        roots.append(root)

    skills = skills_mod.skills_in(roots)
    assert len(skills) == 1
    assert skills[0]["name"] == "triple"


def test_mount_hash_dedup_different_name_same_content_still_deduped(tmp_path: Path) -> None:
    """Two SKILL.md files with identical body content but different
    frontmatter `name` → still deduped. The hash is over the full raw file,
    including frontmatter, so different names produce different hashes and are
    NOT deduped. But if someone copies the exact file (same frontmatter, same
    body) to two locations, it is deduped."""
    # Same content including same frontmatter name → deduped
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    for root in (root_a, root_b):
        skill_dir = root / "same-name"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: same-name\ndescription: d\n---\n\nBody\n",
            encoding="utf-8",
        )

    skills = skills_mod.skills_in([root_a, root_b])
    assert len(skills) == 1


def test_mount_hash_dedup_preserves_second_unique_skill(tmp_path: Path) -> None:
    """Two roots: first has skill A, second has both skill A (same content)
    and skill B (different). Skill A from the second root is deduped; skill B
    is still loaded."""
    content_a = "---\nname: shared\ndescription: Shared skill\n---\n\n# A\n"
    content_b = "---\nname: unique\ndescription: Unique skill\n---\n\n# B\n"

    root_a = tmp_path / "root_a"
    skill_a1 = root_a / "shared"
    skill_a1.mkdir(parents=True)
    (skill_a1 / "SKILL.md").write_text(content_a, encoding="utf-8")

    root_b = tmp_path / "root_b"
    # Same content as root_a's skill
    skill_a2 = root_b / "shared"
    skill_a2.mkdir(parents=True)
    (skill_a2 / "SKILL.md").write_text(content_a, encoding="utf-8")
    # Different content
    skill_b = root_b / "unique"
    skill_b.mkdir(parents=True)
    (skill_b / "SKILL.md").write_text(content_b, encoding="utf-8")

    skills = skills_mod.skills_in([root_a, root_b])
    assert len(skills) == 2
    names = {s["name"] for s in skills}
    assert names == {"shared", "unique"}


def test_mount_hash_dedup_same_content_across_mount_calls_in_scan_tree(
    fake_skills_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    """Integration-style: register a provider root that has identical content
    to a skill already in the fake_skills_dir overlay. The provider copy is
    deduped by hash."""
    content = "---\nname: dup-skill\ndescription: Same everywhere\n---\n\n# Dup\n"

    # Put skill in overlay (fake_skills_dir)
    _write_skill(
        fake_skills_dir, "dup-skill", "name: dup-skill\ndescription: Same everywhere", body="# Dup"
    )

    # Create a provider root with identical SKILL.md content
    provider_root = tmp_path / "provider"
    prov_skill = provider_root / "dup-skill"
    prov_skill.mkdir(parents=True)
    (prov_skill / "SKILL.md").write_text(content, encoding="utf-8")

    # Override the SKILL.md in fake_skills_dir to have exact same content
    (fake_skills_dir / "dup-skill" / "SKILL.md").write_text(content, encoding="utf-8")

    with skill_sources.scoped(lambda: [provider_root]):
        names = skills_mod.names()
        # Only one "dup-skill" — hash dedup prevented the provider duplicate
        dup_count = sum(1 for s in names if s["name"] == "dup-skill")
        assert dup_count == 1


# ─── auto-promote: same-named child becomes root skill ─────────────────────


def _redundant_skill_structure(root: Path) -> None:
    """Simulates the `ava_fleet/ava_fleet/SKILL.md` pattern — a plugin skill
    where the namespace folder has no own SKILL.md but a child folder with the
    same name does."""
    (root / "ava_fleet" / "ava_fleet").mkdir(parents=True)
    (root / "ava_fleet" / "ava_fleet" / "SKILL.md").write_text(
        "---\nname: ava-fleet\ndescription: Fleet coordination patterns\n---\n\n# Fleet\n",
        encoding="utf-8",
    )
    (root / "ava_fleet" / "sub-skill").mkdir()
    (root / "ava_fleet" / "sub-skill" / "SKILL.md").write_text(
        "---\nname: sub-skill\ndescription: A sub skill\n---\n\n# Sub\n",
        encoding="utf-8",
    )


def test_auto_promote_same_named_child_to_root(fake_skills_dir: Path) -> None:
    """When a namespace has no own SKILL.md but a child with the same name
    has one, the child is auto-promoted to root skill. `ava.skills.ava_fleet`
    works directly — no more `ava_fleet.ava_fleet` redundancy."""
    _redundant_skill_structure(fake_skills_dir)

    # The namespace is now a root skill
    node = skills_mod.ava_fleet
    assert isinstance(node, skills_mod._Namespace)
    assert node.name == "ava-fleet"
    assert node._description == "Fleet coordination patterns"
    assert "Fleet" in (node.__doc__ or "")

    # Sub-skill still accessible
    assert "sub_skill" in dir(node)
    proxy = node.sub_skill
    assert isinstance(proxy, skills_mod._SkillProxy)
    assert proxy.name == "sub-skill"


def test_auto_promote_backward_compat_child_still_accessible(
    fake_skills_dir: Path,
) -> None:
    """`ava.skills.ava_fleet.ava_fleet` still works after auto-promotion —
    backward compatibility for existing code that uses the old path."""
    _redundant_skill_structure(fake_skills_dir)

    # Old path still accessible
    proxy = skills_mod.ava_fleet.ava_fleet
    assert isinstance(proxy, skills_mod._SkillProxy)
    assert proxy.name == "ava-fleet"
    assert "Fleet" in (proxy.__doc__ or "")


def test_auto_promote_names_only_emits_root_not_duplicate(
    fake_skills_dir: Path,
) -> None:
    """`names()` emits the auto-promoted root skill but not the redundant
    child — no `ava_fleet.ava_fleet` in the flat listing."""
    _redundant_skill_structure(fake_skills_dir)

    by_id = {skills_mod.identifier(s): s for s in skills_mod.names()}
    assert "ava-fleet" in by_id  # bare root skill
    assert by_id["ava-fleet"]["namespace"] == ()
    # The child should NOT appear as a separate entry
    assert "ava-fleet:ava-fleet" not in by_id
    # Sub-skill still appears, under the canonical dash rendering of its
    # namespace folder (which stays `ava_fleet/` on disk — a plugin dir is a
    # Python package).
    assert "ava-fleet:sub-skill" in by_id


def test_auto_promote_does_not_override_existing_root_skill(
    fake_skills_dir: Path,
) -> None:
    """When a folder already has its own SKILL.md (a natural root skill),
    auto-promotion does NOT override it. The existing root skill wins."""
    # Natural root skill: SKILL.md at parent level
    (fake_skills_dir / "sources").mkdir()
    (fake_skills_dir / "sources" / "SKILL.md").write_text(
        "---\nname: sources\ndescription: Natural root\n---\n\n# Router\n",
        encoding="utf-8",
    )
    # Redundant child with same name
    (fake_skills_dir / "sources" / "sources").mkdir()
    (fake_skills_dir / "sources" / "sources" / "SKILL.md").write_text(
        "---\nname: sources\ndescription: Redundant child\n---\n\n# Child\n",
        encoding="utf-8",
    )

    node = skills_mod.sources
    assert isinstance(node, skills_mod._Namespace)
    # The natural root skill wins
    assert node._description == "Natural root"
    assert "Router" in (node.__doc__ or "")


def test_auto_promote_deep_nesting(fake_skills_dir: Path) -> None:
    """Auto-promotion works at any depth — not just the top level."""
    (fake_skills_dir / "a" / "b" / "b").mkdir(parents=True)
    (fake_skills_dir / "a" / "b" / "b" / "SKILL.md").write_text(
        "---\nname: b\ndescription: Deep redundant skill\n---\n\n# B\n",
        encoding="utf-8",
    )
    (fake_skills_dir / "a" / "b" / "c").mkdir()
    (fake_skills_dir / "a" / "b" / "c" / "SKILL.md").write_text(
        "---\nname: c\ndescription: Sibling\n---\n\n# C\n",
        encoding="utf-8",
    )

    # `a.b` is a root skill (auto-promoted from `a/b/b/SKILL.md`)
    b_node = skills_mod.a.b
    assert isinstance(b_node, skills_mod._Namespace)
    assert b_node.name == "b"
    assert b_node._description == "Deep redundant skill"

    # Sibling still accessible
    assert b_node.c.name == "c"

    # Old path still works
    assert b_node.b.name == "b"

    # names: root `a.b` present, child `a.b.b` absent
    by_id = {skills_mod.identifier(s): s for s in skills_mod.names()}
    assert "a:b" in by_id
    assert "a:b:b" not in by_id
    assert "a:b:c" in by_id


def test_auto_promote_help_renders_root_skill(
    fake_skills_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava.help` on an auto-promoted skill shows the full SKILL.md body
    and lists its children."""
    import ava

    _redundant_skill_structure(fake_skills_dir)

    ava.help(ava.skills.ava_fleet)
    out = capsys.readouterr().out
    assert "### ava.skills.ava_fleet" in out
    assert "Fleet" in out  # root SKILL.md body
    assert "#### ava.skills.ava-fleet:sub-skill\n\nA sub skill" in out  # child listed


# ─── dash/underscore projection ────────────────────────────────────────────
#
# Dash is canonical on disk and in `identifier`; underscore is the Python
# projection rendered by `target` and used for attribute access. Everything in
# between folds through `base.packages.skills.names.match_key`.


def test_dash_dir_renders_dash_identifier_and_underscore_target(fake_skills_dir: Path) -> None:
    """The canonical case: a dash-named skill displays with dashes and is
    reached through the underscore attribute path."""
    _write_skill(
        fake_skills_dir, "write-a-pr-description", "name: write-a-pr-description\ndescription: d"
    )
    (skill,) = skills_mod.names()
    assert skills_mod.identifier(skill) == "write-a-pr-description"
    assert skills_mod.target(skill) == "write_a_pr_description"
    assert skills_mod.write_a_pr_description.name == "write-a-pr-description"


def test_legacy_underscore_dir_still_loads_and_displays_dash(fake_skills_dir: Path) -> None:
    """A hand-installed skill still spelled with underscores (the shape of an
    instance-local `~/.agents/skills/` package nobody renamed) keeps loading, keeps
    resolving under the Python path, and presents the canonical dash name."""
    _write_skill(fake_skills_dir, "wechat_ocr", "name: wechat_ocr\ndescription: read wechat")
    (skill,) = skills_mod.names()
    assert skills_mod.identifier(skill) == "wechat-ocr"
    assert skills_mod.target(skill) == "wechat_ocr"
    assert skills_mod.wechat_ocr.name == "wechat_ocr"  # raw frontmatter preserved


def test_legacy_underscore_namespace_dir_still_loads(fake_skills_dir: Path) -> None:
    """Same for a namespace folder: `web_ai/console/` reads as `web-ai:console`
    and is reached at `ava.skills.web_ai.console`."""
    (fake_skills_dir / "web_ai").mkdir()
    _write_skill(fake_skills_dir / "web_ai", "console", "name: console\ndescription: d")
    (skill,) = skills_mod.names()
    assert skills_mod.identifier(skill) == "web-ai:console"
    assert skills_mod.target(skill) == "web_ai.console"
    assert skills_mod.web_ai.console.name == "console"


def test_registry_gate_matches_across_the_dash_underscore_fold(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install-registry gate compares through the fold, so a registry row
    written before the rename still enables the renamed directory — otherwise
    every skill would silently vanish between the code upgrade and the next
    converge."""
    _write_skill(fake_skills_dir, "ava-goal", "name: ava-goal\ndescription: d")
    monkeypatch.setattr(install_registry, "loadable_skill_names", lambda: {"ava-goal"})
    assert [skills_mod.identifier(s) for s in skills_mod.names()] == ["ava-goal"]


def test_registry_gate_still_hides_an_unlisted_skill(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normalized gate must not turn into a pass-through."""
    _write_skill(fake_skills_dir, "ava-goal", "name: ava-goal\ndescription: d")
    monkeypatch.setattr(install_registry, "loadable_skill_names", lambda: {"something-else"})
    assert skills_mod.names() == []


def test_colliding_dash_and_underscore_dirs_are_refused(fake_skills_dir: Path) -> None:
    """`foo-bar/` beside `foo_bar/` fold to one attribute path. The tree can
    hold one, so the loader refuses rather than silently dropping a skill."""
    _write_skill(fake_skills_dir, "foo-bar", "name: foo-bar\ndescription: dash one")
    _write_skill(fake_skills_dir, "foo_bar", "name: foo_bar\ndescription: underscore one")
    with pytest.raises(skills_mod.SkillNameCollision) as e:
        skills_mod.names()
    assert "foo_bar" in str(e.value)


def test_colliding_namespace_folders_are_refused(fake_skills_dir: Path) -> None:
    """The collision guard covers namespace segments, not just leaf names."""
    (fake_skills_dir / "web-ai").mkdir()
    (fake_skills_dir / "web_ai").mkdir()
    _write_skill(fake_skills_dir / "web-ai", "console", "name: console\ndescription: a")
    _write_skill(fake_skills_dir / "web_ai", "media", "name: media\ndescription: b")
    with pytest.raises(skills_mod.SkillNameCollision):
        skills_mod.names()


def test_two_skills_claiming_one_frontmatter_name_are_refused(fake_skills_dir: Path) -> None:
    """A directory claiming another directory's frontmatter name is refused
    at identity construction (design R2-B): the frontmatter name must fold to
    the directory's own name, so a `second/` dir claiming `first` is a
    mismatch — the old silent-winner collision is now unreachable because the
    identity check fires first."""
    _write_skill(fake_skills_dir, "first", "name: first\ndescription: a")
    _write_skill(fake_skills_dir, "second", "name: first\ndescription: b")
    from base.packages.skills.names import SkillIdentityMismatch

    with pytest.raises(SkillIdentityMismatch):
        skills_mod.names()


def test_a_provider_root_may_still_override_a_same_named_skill(
    fake_skills_dir: Path, tmp_path: Path
) -> None:
    """The collision guard is per mount root: a project-local skill overriding a
    converged one is the documented provider-root behaviour, not a collision."""
    _write_skill(fake_skills_dir, "tdd", "name: tdd\ndescription: converged")
    project = tmp_path / "project-skills"
    project.mkdir()
    _write_skill(project, "tdd", "name: tdd\ndescription: project-local")
    with skill_sources.scoped(lambda: [project]):
        (skill,) = skills_mod.names()
        assert skill["description"] == "project-local"


# ─── SkillIndexBuilder: merged single traversal (regressions) ─────────────


def test_index_gate_folds_dash_underscore(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The INDEX.md gate folds dash/underscore exactly like the SKILL.md gate:
    a legacy underscore directory enabled via its dash registry row keeps its
    namespace doc. Regression — the old two-loop scan compared raw names for
    INDEX.md (dropping the doc while the skill itself loaded) and folded for
    SKILL.md; the merged traversal must not keep that drift."""
    (fake_skills_dir / "foo_bar").mkdir()
    (fake_skills_dir / "foo_bar" / "INDEX.md").write_text("namespace doc", encoding="utf-8")
    monkeypatch.setattr(install_registry, "loadable_skill_names", lambda: {"foo-bar"})
    assert skills_mod.foo_bar.__doc__ == "namespace doc"


def test_index_gate_still_hides_unlisted_dirs(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folded gate must not turn into a pass-through: an unlisted
    directory's INDEX.md sets no doc."""
    _write_skill(fake_skills_dir, "foo_bar", "name: foo-bar\ndescription: s")
    (fake_skills_dir / "foo_bar" / "INDEX.md").write_text("namespace doc", encoding="utf-8")
    monkeypatch.setattr(install_registry, "loadable_skill_names", lambda: {"something-else"})
    assert skills_mod.names() == []


def test_root_index_md_is_ignored(fake_skills_dir: Path) -> None:
    """An INDEX.md at the mount point itself is unconditionally ignored — the
    load dir's own description is not a namespace label (explicit regression
    for the merged single traversal, which now visits the root folder)."""
    (fake_skills_dir / "INDEX.md").write_text("root doc", encoding="utf-8")
    (fake_skills_dir / "feeds").mkdir()
    _write_skill(fake_skills_dir / "feeds", "rss", "name: rss\ndescription: r")
    assert skills_mod.feeds.__doc__ == "Contains: rss"


def test_root_skill_md_still_loads(fake_skills_dir: Path) -> None:
    """A SKILL.md directly at the mount point (empty rel) is a bare root
    skill and loads unconditionally — the merged traversal must keep visiting
    the root folder even though rglob does not yield it."""
    (fake_skills_dir / "SKILL.md").write_text(
        "---\nname: root-skill\ndescription: at the mount point\n---\n\nbody",
        encoding="utf-8",
    )
    (fake_skills_dir / "feeds").mkdir()
    _write_skill(fake_skills_dir / "feeds", "rss", "name: rss\ndescription: r")
    assert skills_mod.root_skill.name == "root-skill"
    assert [s["name"] for s in skills_mod.names()] == ["rss", "root-skill"]


def test_frontmatter_name_not_folding_to_dir_is_refused(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design R2-B: the directory is the identity source, the frontmatter
    name is the display claim — they must fold to one key. A skill whose
    frontmatter says `wechat` inside a `wechat-ocr/` directory used to load
    under a name that was not its own; the loader refuses it now (same
    family as SkillNameCollision)."""
    _write_skill(fake_skills_dir, "wechat-ocr", "name: wechat\ndescription: read wechat")
    monkeypatch.setattr(install_registry, "loadable_skill_names", lambda: {"wechat-ocr"})
    from base.packages.skills.names import SkillIdentityMismatch

    with pytest.raises(SkillIdentityMismatch):
        skills_mod.names()


def test_namespaced_subskill_folds_against_its_leaf_dir(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity check compares the frontmatter name to the LEAF directory
    (the install point), not the namespace — `web_ai/console/` with
    `name: console` is consistent."""
    (fake_skills_dir / "web_ai").mkdir()
    _write_skill(fake_skills_dir / "web_ai", "console", "name: console\ndescription: d")
    monkeypatch.setattr(install_registry, "loadable_skill_names", lambda: {"web_ai"})
    (skill,) = skills_mod.names()
    assert skills_mod.identifier(skill) == "web-ai:console"
