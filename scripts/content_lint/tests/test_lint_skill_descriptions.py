from __future__ import annotations

from pathlib import Path

from scripts.content_lint import lint_skill_descriptions as lint


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


def test_three_deep_skill_with_bad_frontmatter_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "meiva_builtins/skills/practice/meiva-serious-research/practices/reproduce/SKILL.md",
        _BAD_FM,
    )
    assert lint.main(repo_root=tmp_path) == 1


def test_three_deep_skill_with_good_frontmatter_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "meiva_builtins/skills/practice/meiva-serious-research/principles/falsifiability/SKILL.md",
        _GOOD_FM,
    )
    assert lint.main(repo_root=tmp_path) == 0


def test_description_over_hard_ceiling_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".agents/skills/pkg/SKILL.md",
        f'---\nname: x\ndescription: "{"word " * 90}"\n---\n',
    )
    assert lint.main(repo_root=tmp_path) == 1


def test_description_at_the_ceiling_passes_and_one_unit_over_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".agents/skills/pkg/SKILL.md",
        f'---\nname: pkg\ndescription: "{"word " * 80}"\n---\n',
    )
    assert lint.main(repo_root=tmp_path) == 0
    _write(
        tmp_path,
        ".agents/skills/pkg/SKILL.md",
        f'---\nname: pkg\ndescription: "{"word " * 81}"\n---\n',
    )
    assert lint.main(repo_root=tmp_path) == 1


def test_missing_required_field_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".agents/skills/pkg/SKILL.md",
        '---\ndescription: "only description"\n---\n',
    )
    assert lint.main(repo_root=tmp_path) == 1


def test_frontmatter_name_not_folding_to_dir_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "meiva_builtins/skills/page-ocr/SKILL.md",
        "---\nname: page\ndescription: d\n---\n",
    )
    assert lint.main(repo_root=tmp_path) == 1


def test_plugin_root_skill_folds_against_plugin_name(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "meiva_builtins/plugins/meiva_memory/skills/SKILL.md",
        "---\nname: meiva-memory\ndescription: d\n---\n",
    )
    assert lint.main(repo_root=tmp_path) == 0


def test_dash_dir_and_dash_frontmatter_pass(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "meiva_builtins/skills/coordination/meiva-goal/SKILL.md",
        "---\nname: meiva-goal\ndescription: d\n---\n",
    )
    assert lint.main(repo_root=tmp_path) == 0
