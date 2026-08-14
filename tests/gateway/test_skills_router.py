"""GET /api/skills — the read-only installed-skills panel.

Exercises the handler directly (a pure sync read of `$AVA_HOME/skills/` +
`installed.json`, isolated by the `unit_home` fixture): source-layer mapping
(repo→core / plugin→plugin / user→machine), the enabled flag, the
"modified locally" drift signal (live tree hash vs stored `content_hash`),
untracked dirs, and that non-skill registry rows never leak in.
"""

from __future__ import annotations

from pathlib import Path

from gateway.routers.skills import get_skills
from shared import install_registry, paths
from shared.install_registry import InstalledPackage, PackageOrigin, tree_hash


def _mk_skill(name: str, body: str = "# skill\n") -> Path:
    """Create `$AVA_HOME/skills/<name>/SKILL.md` and return the skill dir."""
    d = paths.skills_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


def _register(
    name: str,
    *,
    origin: PackageOrigin,
    content_hash: str | None,
    enabled: bool = True,
    type: install_registry.PackageType = "skill",
) -> None:
    install_registry.register(
        InstalledPackage(
            name=name,
            type=type,
            origin=origin,
            origin_path=f"/src/{name}",
            content_hash=content_hash,
            enabled=enabled,
        )
    )


def test_maps_layers_enabled_and_drift(unit_home: Path) -> None:
    # core (repo), in sync
    alpha = _mk_skill("alpha")
    _register("alpha", origin="repo", content_hash=tree_hash(alpha))
    # plugin, hand-edited since converge wrote it -> drifted
    _mk_skill("beta")
    _register("beta", origin="plugin", content_hash="stale-hash")
    # machine (user), no managed hash -> never "drifted"
    _mk_skill("gamma")
    _register("gamma", origin="user", content_hash=None)
    # disabled core skill
    delta = _mk_skill("delta")
    _register("delta", origin="repo", content_hash=tree_hash(delta), enabled=False)

    rows = {s.name: s for s in get_skills().skills}
    assert set(rows) == {"alpha", "beta", "gamma", "delta"}

    assert rows["alpha"].layer == "core"
    assert rows["alpha"].enabled is True
    assert rows["alpha"].modified_locally is False

    assert rows["beta"].layer == "plugin"
    assert rows["beta"].modified_locally is True

    assert rows["gamma"].layer == "machine"
    assert rows["gamma"].modified_locally is False

    assert rows["delta"].enabled is False


def test_untracked_dir_surfaced_not_loaded(unit_home: Path) -> None:
    _mk_skill("stray")  # dir on disk, no registry entry
    (row,) = get_skills().skills
    assert row.name == "stray"
    assert row.layer == "untracked"
    assert row.enabled is False
    assert row.modified_locally is False


def test_non_skill_rows_and_missing_dir_excluded(unit_home: Path) -> None:
    # An mcp registry row (lives under mcps/, not skills/) and a plugin row with
    # no skills/<name>/ dir must not appear — the load dir is the source of truth.
    _register("some-mcp", origin="user", content_hash=None, type="mcp")
    _register("bare-plugin", origin="plugin", content_hash=None, type="plugin")
    assert get_skills().skills == []


def test_empty_when_no_load_dir(unit_home: Path) -> None:
    assert not paths.skills_dir().exists()
    assert get_skills().skills == []


def test_put_toggles_enabled(unit_home: Path) -> None:
    """PUT /api/skills toggles the enabled flag on a registered skill."""
    from gateway.routers.skills import update_skill_enabled
    from gateway.schemas.skills import SkillEnableUpdate

    delta = _mk_skill("delta")
    _register("delta", origin="repo", content_hash=tree_hash(delta), enabled=True)

    # Toggle off
    result = update_skill_enabled(SkillEnableUpdate(name="delta", enabled=False))
    assert result.enabled is False

    # Verify persisted in registry
    pkg = install_registry.get("delta")
    assert pkg is not None
    assert pkg.enabled is False

    # Toggle back on
    result = update_skill_enabled(SkillEnableUpdate(name="delta", enabled=True))
    assert result.enabled is True

    # Verify persisted
    pkg = install_registry.get("delta")
    assert pkg is not None
    assert pkg.enabled is True


def test_put_accepts_the_canonical_dash_spelling_of_a_legacy_row(unit_home: Path) -> None:
    """The request name is inbound: a panel sending the canonical dash form must
    reach a registry row still written with underscores, and the response must
    carry the row's own spelling (which is what addresses the directory)."""
    from gateway.routers.skills import update_skill_enabled
    from gateway.schemas.skills import SkillEnableUpdate

    legacy = _mk_skill("wechat_ocr")
    _register("wechat_ocr", origin="user", content_hash=tree_hash(legacy), enabled=True)

    result = update_skill_enabled(SkillEnableUpdate(name="wechat-ocr", enabled=False))
    # The response renders the canonical display spelling (dash), like the
    # GET panel — the row's own spelling is internal.
    assert result.name == "wechat-ocr"
    assert result.enabled is False
    assert result.layer == "machine"  # the row was found, not treated as untracked

    pkg = install_registry.get("wechat_ocr")
    assert pkg is not None and pkg.enabled is False
    # and no duplicate row was created under the dash spelling
    assert [p.name for p in install_registry.load().packages] == ["wechat_ocr"]


def test_get_renders_legacy_underscore_dirs_in_canonical_spelling(unit_home: Path) -> None:
    """The panel must show `ava-code` / `auto-review.old`, not the raw
    on-disk `ava_code` / `auto_review.old` — dash is the canonical display
    form at every human-facing surface (P0 regression)."""
    _mk_skill("ava_code")
    _mk_skill("auto_review.old")
    rows = {r.name: r for r in get_skills().skills}
    assert "ava-code" in rows
    assert "auto-review.old" in rows
    assert "ava_code" not in rows
    assert "auto_review.old" not in rows
    # untracked rows keep their layer semantics
    assert rows["ava-code"].layer == "untracked"


def test_preserved_subtree_not_reported_as_drift(unit_home: Path) -> None:
    """A package carrying only preserved local adapters (web-sources/people)
    must not read as modified locally — it is in-sync by converge's own rule."""
    from gateway.routers.skills import get_skills

    ws = _mk_skill("web-sources")
    _register("web-sources", origin="repo", content_hash=tree_hash(ws), enabled=True)
    (ws / "people").mkdir(parents=True)
    (ws / "people" / "SKILL.md").write_text(
        "---\nname: people\ndescription: local\n---\n", encoding="utf-8"
    )
    (ws / "people" / ".preserved").touch()
    rows = {r.name: r for r in get_skills().skills}
    assert rows["web-sources"].modified_locally is False


def test_put_rejects_untracked_skill(unit_home: Path) -> None:
    """PUT /api/skills returns 404 for a skill not in the registry."""
    import pytest
    from fastapi import HTTPException

    from gateway.routers.skills import update_skill_enabled
    from gateway.schemas.skills import SkillEnableUpdate

    _mk_skill("stray")  # on disk, not registered

    with pytest.raises(HTTPException) as exc:
        update_skill_enabled(SkillEnableUpdate(name="stray", enabled=True))
    assert exc.value.status_code == 404
    assert "not found in registry" in exc.value.detail


def test_dot_dirs_not_panel_rows(unit_home: Path) -> None:
    """Converge's dot-prefixed staging dirs (`.name.new` / `.name.trash`) are
    transient internals — never surfaced as untracked panel rows."""
    _mk_skill(".goal.new")
    _mk_skill(".web-sources.trash")
    assert get_skills().skills == []


def test_registry_row_correlates_across_the_dash_underscore_fold(
    unit_home: Path,
) -> None:
    """A registry row written `ava-code` and a directory still spelled
    `ava_code/` are one skill — the GET panel must correlate them through the
    fold, not raw names (audit 02 #4 bare comparison in `by_name`)."""
    d = _mk_skill("ava_code")
    _register("ava-code", origin="repo", content_hash=tree_hash(d))
    (row,) = get_skills().skills
    assert row.name == "ava-code"  # canonical display
    assert row.layer == "core"  # correlated with the registry row, not untracked
    assert row.modified_locally is False
