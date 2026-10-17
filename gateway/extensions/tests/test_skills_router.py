from __future__ import annotations

from pathlib import Path

from base import paths
from base.packages.extensions import install_registry
from base.packages.extensions.install_registry import InstalledPackage, PackageOrigin, tree_hash
from gateway.extensions.skills import get_skills


def _mk_skill(name: str, body: str = "# skill\n") -> Path:
    d = paths.skills_dir(paths.meiva_home()) / name
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
    alpha = _mk_skill("alpha")
    _register("alpha", origin="repo", content_hash=tree_hash(alpha))
    _mk_skill("beta")
    _register("beta", origin="plugin", content_hash="stale-hash")
    _mk_skill("gamma")
    _register("gamma", origin="user", content_hash=None)
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
    _mk_skill("stray")
    (row,) = get_skills().skills
    assert row.name == "stray"
    assert row.layer == "untracked"
    assert row.enabled is False
    assert row.modified_locally is False


def test_non_skill_rows_and_missing_dir_excluded(unit_home: Path) -> None:
    _register("some-mcp", origin="user", content_hash=None, type="mcp")
    _register("bare-plugin", origin="plugin", content_hash=None, type="plugin")
    assert get_skills().skills == []


def test_empty_when_no_load_dir(unit_home: Path) -> None:
    assert not paths.skills_dir(paths.meiva_home()).exists()
    assert get_skills().skills == []


def test_put_toggles_enabled(unit_home: Path) -> None:
    from gateway.extensions.schemas import SkillEnableUpdate
    from gateway.extensions.skills import update_skill_enabled

    delta = _mk_skill("delta")
    _register("delta", origin="repo", content_hash=tree_hash(delta), enabled=True)

    result = update_skill_enabled(SkillEnableUpdate(name="delta", enabled=False))
    assert result.enabled is False

    pkg = install_registry.get("delta")
    assert pkg is not None
    assert pkg.enabled is False

    result = update_skill_enabled(SkillEnableUpdate(name="delta", enabled=True))
    assert result.enabled is True

    pkg = install_registry.get("delta")
    assert pkg is not None
    assert pkg.enabled is True


def test_put_accepts_the_canonical_dash_spelling_of_a_legacy_row(unit_home: Path) -> None:
    from gateway.extensions.schemas import SkillEnableUpdate
    from gateway.extensions.skills import update_skill_enabled

    legacy = _mk_skill("page_ocr")
    _register("page_ocr", origin="user", content_hash=tree_hash(legacy), enabled=True)

    result = update_skill_enabled(SkillEnableUpdate(name="page-ocr", enabled=False))
    assert result.name == "page-ocr"
    assert result.enabled is False
    assert result.layer == "machine"

    pkg = install_registry.get("page_ocr")
    assert pkg is not None and pkg.enabled is False
    assert [p.name for p in install_registry.load().packages] == ["page_ocr"]


def test_get_renders_legacy_underscore_dirs_in_canonical_spelling(unit_home: Path) -> None:
    _mk_skill("meiva_code")
    _mk_skill("auto_review.old")
    rows = {r.name: r for r in get_skills().skills}
    assert "meiva-code" in rows
    assert "auto-review.old" in rows
    assert "meiva_code" not in rows
    assert "auto_review.old" not in rows
    assert rows["meiva-code"].layer == "untracked"


def test_preserved_subtree_not_reported_as_drift(unit_home: Path) -> None:
    from gateway.extensions.skills import get_skills

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
    import pytest
    from fastapi import HTTPException

    from gateway.extensions.schemas import SkillEnableUpdate
    from gateway.extensions.skills import update_skill_enabled

    _mk_skill("stray")

    with pytest.raises(HTTPException) as exc:
        update_skill_enabled(SkillEnableUpdate(name="stray", enabled=True))
    assert exc.value.status_code == 404
    assert "not found in registry" in exc.value.detail


def test_dot_dirs_not_panel_rows(unit_home: Path) -> None:
    _mk_skill(".goal.new")
    _mk_skill(".web-sources.trash")
    assert get_skills().skills == []


def test_registry_row_correlates_across_the_dash_underscore_fold(
    unit_home: Path,
) -> None:
    d = _mk_skill("meiva_code")
    _register("meiva-code", origin="repo", content_hash=tree_hash(d))
    (row,) = get_skills().skills
    assert row.name == "meiva-code"
    assert row.layer == "core"
    assert row.modified_locally is False
