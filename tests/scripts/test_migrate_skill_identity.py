"""scripts/migrate_skill_identity.py — the R2-B legacy-data migration tool.

Check (default, read-only) and apply (fix, dir name authoritative) over the
three identity surfaces: frontmatter-vs-dir mismatches in the skills load dir,
folding-duplicate rows in installed.json, and unresolved skill-list references
in the DB stores (the DB surface is exercised against a fake psql here — the
real rewrite SQL is covered by the psql invocation, not by hitting a DB).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import migrate_skill_identity as mig


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A scratch AVA_HOME: one consistent skill, one mismatched skill, and a
    registry with a folding-duplicate row pair."""
    skills = tmp_path / "skills"
    (skills / "good-skill").mkdir(parents=True)
    (skills / "good-skill" / "SKILL.md").write_text(
        "---\nname: good-skill\ndescription: d\n---\n", encoding="utf-8"
    )
    (skills / "wechat-ocr").mkdir()
    (skills / "wechat-ocr" / "SKILL.md").write_text(
        "---\nname: wechat\ndescription: d\n---\n", encoding="utf-8"
    )
    (tmp_path / "installed.json").write_text(
        json.dumps(
            {
                "packages": [
                    {"name": "ava-code", "type": "skill"},
                    {"name": "ava_code", "type": "skill"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_check_reports_both_local_findings(home: Path) -> None:
    out = mig.main(["--ava-home", str(home), "--no-db"])
    assert out == 1
    caps = _capture(mig, home)
    assert "does not fold to directory" in caps
    assert "DUPLICATE ROW" in caps


def test_apply_fixes_and_recheck_is_clean(home: Path) -> None:
    assert mig.main(["--ava-home", str(home), "--no-db", "--apply"]) == 0
    # frontmatter rewritten to the directory's display name (dir authoritative)
    fm = (home / "skills" / "wechat-ocr" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: wechat-ocr" in fm
    # registry deduped to one canonical row
    reg = json.loads((home / "installed.json").read_text(encoding="utf-8"))
    assert [p["name"] for p in reg["packages"]] == ["ava-code"]
    # a backup of the touched frontmatter exists
    backups = list(home.glob("skill-identity-backup-*/*.bak"))
    assert backups
    assert mig.main(["--ava-home", str(home), "--no-db"]) == 0


def test_clean_home_passes_check(home: Path) -> None:
    mig.main(["--ava-home", str(home), "--no-db", "--apply"])
    assert mig.main(["--ava-home", str(home), "--no-db"]) == 0


def test_catalog_applies_fixes_before_db_resolution(home: Path) -> None:
    """A reference like `wechat` resolves TODAY (frontmatter name) and only
    stops resolving once the frontmatter is renamed — the DB surface must be
    judged against the post-fix catalog, or the apply step would miss the
    reference it is supposed to rewrite."""
    fixes = {"wechat-ocr": "wechat-ocr"}
    by_key = mig.catalog(home / "skills", fixes)
    assert mig.match_key("wechat") not in by_key
    assert mig.match_key("wechat-ocr") in by_key


def _capture(module: Any, home: Path) -> str:
    """Run main() with stdout captured (the module prints its report)."""
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        module.main(["--ava-home", str(home), "--no-db"])
    return buf.getvalue()


# ─── decided transforms (405 ruling 2026-08-08) ─────────────────────────────


def test_transform_expands_bare_ava_code_and_drops_telegram() -> None:
    transformed = mig._transform_skill_list(
        ["ava_code", "gmail", "telegram", "ava-code:pr"], {"wechat": "wechat-ocr"}
    )
    assert transformed == [
        "ava-code:worktree",
        "ava-code:pr",
        "ava-code:testing",
        "ava-code:conventions",
        "gmail",
    ]  # telegram dropped; ava-code:pr deduped against the expansion


def test_transform_maps_wechat_and_is_idempotent() -> None:
    once = mig._transform_skill_list(["wechat"], {"wechat": "wechat-ocr"})
    assert once == ["wechat-ocr"]
    assert mig._transform_skill_list(once, {"wechat": "wechat-ocr"}) == once
    # bare dash spelling expands the same way
    assert mig._transform_skill_list(["ava-code"], {}) == list(mig._AVA_CODE_SUBSKILLS)


def test_apply_registry_contends_with_the_module_registry_lock(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool's own registry rewrite takes the SAME lock `install_registry`
    writers take — proven by holding that lock and watching the tool time out.

    Without it the tool is a fourth writer racing the other three, and worse
    than the usual lost update: it stages through the same fixed
    `installed.json.tmp` name as `install_registry.save`, so a concurrent save
    has its staged body overwritten and its rename left with nothing to rename.
    """
    from shared import install_registry as reg
    from shared.platform import LockTimeoutError

    registry_path = home / "installed.json"
    dups = mig.scan_registry(registry_path)
    assert dups, "the fixture registry should carry a folding-duplicate pair"

    monkeypatch.setattr(reg, "_REGISTRY_LOCK_TIMEOUT_S", 0.3)
    with reg.registry_lock(registry_path), pytest.raises(LockTimeoutError):
        mig.apply_registry(registry_path, dups)

    # The refused run wrote nothing: both rows are still there for a later pass.
    still = json.loads(registry_path.read_text(encoding="utf-8"))
    assert [p["name"] for p in still["packages"]] == ["ava-code", "ava_code"]
