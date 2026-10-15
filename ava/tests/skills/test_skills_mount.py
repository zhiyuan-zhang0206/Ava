from pathlib import Path

import pytest

import ava.skills as skills_mod
from ava.sdk_surface import skill_sources
from ava.skills.skill_proxies import Namespace, SkillProxy
from ava.tests.skills._skills_helpers import fake_skills_dir as fake_skills_dir
from ava.tests.skills._skills_helpers import overlay_all_enabled as overlay_all_enabled
from ava.tests.skills._skills_helpers import write_skill
from base.packages.extensions import install_registry
from base.packages.skills.tree import SkillNameCollision, identifier, target

pytestmark = pytest.mark.usefixtures("unit_home")


def test_mount_dedup_by_content_hash_across_roots(fake_skills_dir: Path, tmp_path: Path) -> None:
    root_a = tmp_path / "root_a"
    skill_a = root_a / "my-skill"
    skill_a.mkdir(parents=True)
    (skill_a / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Shared skill content\n---\n\n# Body\n",
        encoding="utf-8",
    )

    root_b = tmp_path / "root_b"
    skill_b = root_b / "my-skill"
    skill_b.mkdir(parents=True)
    (skill_b / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Shared skill content\n---\n\n# Body\n",
        encoding="utf-8",
    )

    skills = skills_mod.skills_in([root_a, root_b])
    assert len(skills) == 1
    assert skills[0]["name"] == "my-skill"


def test_mount_hash_dedup_respects_different_content(tmp_path: Path) -> None:
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
    assert len(skills) == 2
    names = {s["name"] for s in skills}
    assert names == {"skill-a", "skill-b"}


def test_mount_hash_dedup_does_not_affect_single_root(fake_skills_dir: Path) -> None:
    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")
    write_skill(fake_skills_dir, "beta", "name: beta\ndescription: b")
    skills = skills_mod.skills_in([fake_skills_dir])
    assert len(skills) == 2
    names = {s["name"] for s in skills}
    assert names == {"alpha", "beta"}


def test_mount_hash_dedup_skips_third_identical_copy(tmp_path: Path) -> None:
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
    content_a = "---\nname: shared\ndescription: Shared skill\n---\n\n# A\n"
    content_b = "---\nname: unique\ndescription: Unique skill\n---\n\n# B\n"

    root_a = tmp_path / "root_a"
    skill_a1 = root_a / "shared"
    skill_a1.mkdir(parents=True)
    (skill_a1 / "SKILL.md").write_text(content_a, encoding="utf-8")

    root_b = tmp_path / "root_b"
    skill_a2 = root_b / "shared"
    skill_a2.mkdir(parents=True)
    (skill_a2 / "SKILL.md").write_text(content_a, encoding="utf-8")
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
    content = "---\nname: dup-skill\ndescription: Same everywhere\n---\n\n# Dup\n"

    write_skill(
        fake_skills_dir, "dup-skill", "name: dup-skill\ndescription: Same everywhere", body="# Dup"
    )

    provider_root = tmp_path / "provider"
    prov_skill = provider_root / "dup-skill"
    prov_skill.mkdir(parents=True)
    (prov_skill / "SKILL.md").write_text(content, encoding="utf-8")

    (fake_skills_dir / "dup-skill" / "SKILL.md").write_text(content, encoding="utf-8")

    with skill_sources.scoped(lambda: [provider_root]):
        names = skills_mod.names()
        dup_count = sum(1 for s in names if s["name"] == "dup-skill")
        assert dup_count == 1


def _redundant_skill_structure(root: Path) -> None:
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


def test_auto_promote_same_named_child_to_root(db_url: str, fake_skills_dir: Path) -> None:
    _redundant_skill_structure(fake_skills_dir)

    node = skills_mod.ava_fleet
    assert isinstance(node, Namespace)
    assert node.name == "ava-fleet"
    assert node._description == "Fleet coordination patterns"
    assert "Fleet" in (node.__doc__ or "")

    assert "sub_skill" in dir(node)
    proxy = node.sub_skill
    assert isinstance(proxy, SkillProxy)
    assert proxy.name == "sub-skill"


def test_auto_promote_backward_compat_child_still_accessible(
    db_url: str,
    fake_skills_dir: Path,
) -> None:
    _redundant_skill_structure(fake_skills_dir)

    proxy = skills_mod.ava_fleet.ava_fleet
    assert isinstance(proxy, SkillProxy)
    assert proxy.name == "ava-fleet"
    assert "Fleet" in (proxy.__doc__ or "")


def test_auto_promote_names_only_emits_root_not_duplicate(
    fake_skills_dir: Path,
) -> None:
    _redundant_skill_structure(fake_skills_dir)

    by_id = {identifier(s): s for s in skills_mod.names()}
    assert "ava-fleet" in by_id
    assert by_id["ava-fleet"]["namespace"] == ()
    assert "ava-fleet:ava-fleet" not in by_id
    assert "ava-fleet:sub-skill" in by_id


def test_auto_promote_does_not_override_existing_root_skill(
    db_url: str,
    fake_skills_dir: Path,
) -> None:
    (fake_skills_dir / "sources").mkdir()
    (fake_skills_dir / "sources" / "SKILL.md").write_text(
        "---\nname: sources\ndescription: Natural root\n---\n\n# Router\n",
        encoding="utf-8",
    )
    (fake_skills_dir / "sources" / "sources").mkdir()
    (fake_skills_dir / "sources" / "sources" / "SKILL.md").write_text(
        "---\nname: sources\ndescription: Redundant child\n---\n\n# Child\n",
        encoding="utf-8",
    )

    node = skills_mod.sources
    assert isinstance(node, Namespace)
    assert node._description == "Natural root"
    assert "Router" in (node.__doc__ or "")


def test_auto_promote_deep_nesting(fake_skills_dir: Path) -> None:
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

    b_node = skills_mod.a.b
    assert isinstance(b_node, Namespace)
    assert b_node.name == "b"
    assert b_node._description == "Deep redundant skill"

    assert b_node.c.name == "c"

    assert b_node.b.name == "b"

    by_id = {identifier(s): s for s in skills_mod.names()}
    assert "a:b" in by_id
    assert "a:b:b" not in by_id
    assert "a:b:c" in by_id


@pytest.mark.usefixtures("sdk_model_owner")
def test_auto_promote_help_renders_root_skill(
    db_url: str, fake_skills_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import ava

    _redundant_skill_structure(fake_skills_dir)

    ava.help(ava.skills.ava_fleet)
    out = capsys.readouterr().out
    assert "### ava.skills.ava_fleet" in out
    assert "Fleet" in out
    assert "#### ava.skills.ava_fleet.sub_skill\n\nA sub skill" in out


def test_dash_dir_renders_dash_identifier_and_underscore_target(fake_skills_dir: Path) -> None:
    write_skill(
        fake_skills_dir, "write-a-pr-description", "name: write-a-pr-description\ndescription: d"
    )
    (skill,) = skills_mod.names()
    assert identifier(skill) == "write-a-pr-description"
    assert target(skill) == "write_a_pr_description"
    assert skills_mod.write_a_pr_description.name == "write-a-pr-description"


def test_legacy_underscore_dir_still_loads_and_displays_dash(fake_skills_dir: Path) -> None:
    write_skill(fake_skills_dir, "page_ocr", "name: page_ocr\ndescription: read page")
    (skill,) = skills_mod.names()
    assert identifier(skill) == "page-ocr"
    assert target(skill) == "page_ocr"
    assert skills_mod.page_ocr.name == "page_ocr"


def test_legacy_underscore_namespace_dir_still_loads(fake_skills_dir: Path) -> None:
    (fake_skills_dir / "web_ai").mkdir()
    write_skill(fake_skills_dir / "web_ai", "console", "name: console\ndescription: d")
    (skill,) = skills_mod.names()
    assert identifier(skill) == "web-ai:console"
    assert target(skill) == "web_ai.console"
    assert skills_mod.web_ai.console.name == "console"


def test_registry_gate_matches_across_the_dash_underscore_fold(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_skill(fake_skills_dir, "ava-goal", "name: ava-goal\ndescription: d")
    monkeypatch.setattr(install_registry, "loadable_skill_names", lambda: {"ava-goal"})
    assert [identifier(s) for s in skills_mod.names()] == ["ava-goal"]


def test_registry_gate_still_hides_an_unlisted_skill(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_skill(fake_skills_dir, "ava-goal", "name: ava-goal\ndescription: d")
    monkeypatch.setattr(install_registry, "loadable_skill_names", lambda: {"something-else"})
    assert skills_mod.names() == []


def test_colliding_dash_and_underscore_dirs_are_refused(fake_skills_dir: Path) -> None:
    write_skill(fake_skills_dir, "foo-bar", "name: foo-bar\ndescription: dash one")
    write_skill(fake_skills_dir, "foo_bar", "name: foo_bar\ndescription: underscore one")
    with pytest.raises(SkillNameCollision) as e:
        skills_mod.names()
    assert "foo_bar" in str(e.value)


def test_colliding_namespace_folders_are_refused(fake_skills_dir: Path) -> None:
    (fake_skills_dir / "web-ai").mkdir()
    (fake_skills_dir / "web_ai").mkdir()
    write_skill(fake_skills_dir / "web-ai", "console", "name: console\ndescription: a")
    write_skill(fake_skills_dir / "web_ai", "media", "name: media\ndescription: b")
    with pytest.raises(SkillNameCollision):
        skills_mod.names()


def test_two_skills_claiming_one_frontmatter_name_are_refused(fake_skills_dir: Path) -> None:
    write_skill(fake_skills_dir, "first", "name: first\ndescription: a")
    write_skill(fake_skills_dir, "second", "name: first\ndescription: b")
    from base.packages.skills.names import SkillIdentityMismatch

    with pytest.raises(SkillIdentityMismatch):
        skills_mod.names()


def test_a_provider_root_may_still_override_a_same_named_skill(
    fake_skills_dir: Path, tmp_path: Path
) -> None:
    write_skill(fake_skills_dir, "tdd", "name: tdd\ndescription: converged")
    project = tmp_path / "project-skills"
    project.mkdir()
    write_skill(project, "tdd", "name: tdd\ndescription: project-local")
    with skill_sources.scoped(lambda: [project]):
        (skill,) = skills_mod.names()
        assert skill["description"] == "project-local"


def test_index_gate_folds_dash_underscore(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_skills_dir / "foo_bar").mkdir()
    (fake_skills_dir / "foo_bar" / "INDEX.md").write_text("namespace doc", encoding="utf-8")
    monkeypatch.setattr(install_registry, "loadable_skill_names", lambda: {"foo-bar"})
    assert skills_mod.foo_bar.__doc__ == "namespace doc"


def test_index_gate_still_hides_unlisted_dirs(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_skill(fake_skills_dir, "foo_bar", "name: foo-bar\ndescription: s")
    (fake_skills_dir / "foo_bar" / "INDEX.md").write_text("namespace doc", encoding="utf-8")
    monkeypatch.setattr(install_registry, "loadable_skill_names", lambda: {"something-else"})
    assert skills_mod.names() == []


def test_root_index_md_is_ignored(fake_skills_dir: Path) -> None:
    (fake_skills_dir / "INDEX.md").write_text("root doc", encoding="utf-8")
    (fake_skills_dir / "feeds").mkdir()
    write_skill(fake_skills_dir / "feeds", "rss", "name: rss\ndescription: r")
    assert skills_mod.feeds.__doc__ == "Contains: rss"


def test_root_skill_md_still_loads(fake_skills_dir: Path) -> None:
    (fake_skills_dir / "SKILL.md").write_text(
        "---\nname: root-skill\ndescription: at the mount point\n---\n\nbody",
        encoding="utf-8",
    )
    (fake_skills_dir / "feeds").mkdir()
    write_skill(fake_skills_dir / "feeds", "rss", "name: rss\ndescription: r")
    assert skills_mod.root_skill.name == "root-skill"
    assert [s["name"] for s in skills_mod.names()] == ["rss", "root-skill"]


def test_frontmatter_name_not_folding_to_dir_is_refused(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_skill(fake_skills_dir, "page-ocr", "name: page\ndescription: read page")
    monkeypatch.setattr(install_registry, "loadable_skill_names", lambda: {"page-ocr"})
    from base.packages.skills.names import SkillIdentityMismatch

    with pytest.raises(SkillIdentityMismatch):
        skills_mod.names()


def test_namespaced_subskill_folds_against_its_leaf_dir(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_skills_dir / "web_ai").mkdir()
    write_skill(fake_skills_dir / "web_ai", "console", "name: console\ndescription: d")
    monkeypatch.setattr(install_registry, "loadable_skill_names", lambda: {"web_ai"})
    (skill,) = skills_mod.names()
    assert identifier(skill) == "web-ai:console"
