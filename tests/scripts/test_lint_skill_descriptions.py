"""scripts/lint_skill_descriptions.py: the frontmatter gates reach deep skill trees.

The 2026-08 malformed-frontmatter incident (ava-serious-research
practices/reproduce/SKILL.md with an unquoted `: ` in description) shipped
green because `_skill_md_files()` globbed only one and two levels deep and
silently skipped the three-deep tree. These tests pin the depth fix and the
existing hard gates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import lint_skill_descriptions as lint


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the lint at a scratch repo root so the real checkout is untouched."""
    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    return tmp_path


def _write(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


_GOOD_FM = """---
name: falsifiability
description: "A description: with a colon, quoted properly."
---

# Body
"""

_BAD_FM = """---
name: test-skill
description: A description: with an unquoted colon.
---

# Body
"""


def test_three_deep_skill_with_bad_frontmatter_is_rejected(repo: Path) -> None:
    _write(
        repo,
        ".agents/skills/ava-serious-research/practices/reproduce/SKILL.md",
        _BAD_FM,
    )
    assert lint.main() == 1


def test_three_deep_skill_with_good_frontmatter_passes(repo: Path) -> None:
    _write(
        repo,
        ".agents/skills/ava-serious-research/principles/falsifiability/SKILL.md",
        _GOOD_FM,
    )
    assert lint.main() == 0


def test_description_over_hard_ceiling_is_rejected(repo: Path) -> None:
    _write(
        repo,
        ".agents/skills/pkg/SKILL.md",
        f'---\nname: x\ndescription: "{"word " * 90}"\n---\n',
    )
    assert lint.main() == 1


def test_missing_required_field_is_rejected(repo: Path) -> None:
    _write(
        repo,
        ".agents/skills/pkg/SKILL.md",
        '---\ndescription: "only description"\n---\n',
    )
    assert lint.main() == 1


# ─── gate 3: identity consistency (design R2-B) ────────────────────────────


def test_frontmatter_name_not_folding_to_dir_rejected(repo: Path) -> None:
    _write(
        repo,
        "ava_builtins/skills/wechat-ocr/SKILL.md",
        "---\nname: wechat\ndescription: d\n---\n",
    )
    assert lint.main() == 1


def test_plugin_root_skill_folds_against_plugin_name(repo: Path) -> None:
    """A plugin's root SKILL.md sits at plugins/<p>/skills/SKILL.md in the
    source tree but converges to skills/<p>/SKILL.md — the load-dir leaf is
    the plugin name, so `ava_memory` + `name: ava-memory` is consistent."""
    _write(
        repo,
        "ava_builtins/plugins/ava_memory/skills/SKILL.md",
        "---\nname: ava-memory\ndescription: d\n---\n",
    )
    assert lint.main() == 0


def test_dash_dir_and_dash_frontmatter_pass(repo: Path) -> None:
    _write(
        repo,
        "ava_builtins/skills/ava-goal/SKILL.md",
        "---\nname: ava-goal\ndescription: d\n---\n",
    )
    assert lint.main() == 0
