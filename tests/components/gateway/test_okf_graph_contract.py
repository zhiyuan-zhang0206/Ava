from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml


def _legacy_parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                loaded = yaml.safe_load("\n".join(lines[1:i]))
            except yaml.YAMLError:
                loaded = None
            fm: dict[str, Any] = cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}
            return fm, "\n".join(lines[i + 1 :]).lstrip("\n")
    return {}, text


def test_parse_frontmatter_equivalent_to_legacy_on_repo_bundles() -> None:
    from base.packages.docs.okf_graph import find_files, parse_frontmatter

    repo_root = Path(__file__).resolve().parents[3]
    bundles = find_files(repo_root)
    assert bundles, "repo has no .meiva.okf.md bundles — fixture assumption broken"

    for rel in bundles:
        text = (repo_root / rel).read_text(encoding="utf-8")
        legacy_fm, legacy_body = _legacy_parse_frontmatter(text)
        fm, body = parse_frontmatter(text)
        assert fm == legacy_fm, f"{rel}: frontmatter differs from legacy"
        assert body == legacy_body, f"{rel}: body differs from legacy"
