"""Shared fixtures and helpers for the ava.skills test files; split from ava/tests/skills/test_skills.py (task #4922)."""

from pathlib import Path

import pytest

from base.packages.extensions import install_registry
from base.paths import skills_dir


@pytest.fixture(autouse=True)
def _overlay_all_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: treat every directory in the per-test load dir (`<home>/skills`)
    as a tracked+enabled skill, so the parse/merge tests below stay
    focused on scanning rather than the install-registry reservation.

    The reservation behavior gets its own tests that re-patch `loadable_skill_names` to a
    controlled set (a per-test setattr overrides this autouse one)."""

    def _all_enabled() -> set[str]:
        d = skills_dir()
        return {p.name for p in d.iterdir() if p.is_dir()} if d.is_dir() else set()

    monkeypatch.setattr(install_registry, "loadable_skill_names", _all_enabled)


@pytest.fixture
def fake_skills_dir(unit_home: Path) -> Path:
    d = skills_dir()
    d.mkdir()
    return d


def _write_skill(root: Path, dirname: str, frontmatter: str, body: str = "") -> None:
    skill_dir = root / dirname
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
