"""Contract: every .ava.okf.md bundle of the repository parses identically through the adapter and the legacy parser."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml


def _legacy_parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """The pre-#2448 `okf_graph.parse_frontmatter` — reference implementation."""
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
    """Every `.ava.okf.md` bundle in the repo parses identically through the
    adapter and the legacy parser — frontmatter and body, field for field."""
    from base.packages.docs.okf_graph import find_files, parse_frontmatter

    repo_root = Path(__file__).resolve().parents[2]
    bundles = find_files(repo_root)
    assert bundles, "repo has no .ava.okf.md bundles — fixture assumption broken"

    for rel in bundles:
        text = (repo_root / rel).read_text(encoding="utf-8")
        legacy_fm, legacy_body = _legacy_parse_frontmatter(text)
        fm, body = parse_frontmatter(text)
        assert fm == legacy_fm, f"{rel}: frontmatter differs from legacy"
        assert body == legacy_body, f"{rel}: body differs from legacy"
