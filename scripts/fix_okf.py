#!/usr/bin/env python3
"""fix_okf.py — Fix migrated .ava.okf.md files:
1. Rebuild frontmatter with proper YAML serialization (fix quote issues)
2. Update wikilinks in body from old .md paths to .ava.okf.md paths
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml


def parse_frontmatter(text: str) -> tuple[dict, str, int]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text, 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_text = "\n".join(lines[1:i])
            try:
                fm = yaml.safe_load(fm_text) or {}
            except yaml.YAMLError:
                return {}, text, i + 1
            if not isinstance(fm, dict):
                fm = {}
            return fm, "\n".join(lines[i + 1 :]).lstrip("\n"), i + 1
    return {}, text, 0


def build_frontmatter_yaml(fm: dict) -> str:
    """Build frontmatter using yaml.dump for proper escaping."""
    # Use yaml.dump with flow style for simple values
    lines = ["---"]
    yaml_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    lines.append(yaml_str.rstrip("\n"))
    lines.append("---")
    return "\n".join(lines)


def update_wikilinks(body: str, all_targets: set[str]) -> str:
    """Replace wikilinks like [[path/concept.md]] with [[path/concept.ava.okf.md]]
    when the .ava.okf.md target exists."""

    def replacer(m):
        target = m.group(1).strip()
        # Skip targets with URLs
        if "://" in target:
            return m.group(0)
        # Already has .ava.okf.md?
        if target.endswith(".ava.okf.md"):
            return m.group(0)
        # Try matching with .ava.okf.md
        if target.endswith(".md"):
            new_target = target[:-3] + ".ava.okf.md"
        else:
            new_target = target + ".ava.okf.md"
        # Check if target exists
        # Try exact match first
        if new_target in all_targets:
            return f"[[{new_target}]]"
        # Try basename match
        base = Path(new_target).name
        matches = [t for t in all_targets if Path(t).name == base]
        if len(matches) == 1:
            return f"[[{matches[0]}]]"
        # Keep original
        return m.group(0)

    return re.sub(r"\[\[([^\]]+)\]\]", replacer, body)


def fix_file(filepath: Path, all_targets: set[str]) -> bool:
    """Fix one file. Returns True if changed."""
    text = filepath.read_text(encoding="utf-8")
    fm, body, _fm_end = parse_frontmatter(text)

    if not fm:
        print(f"  SKIP {filepath.name}: no valid frontmatter")
        return False

    # Rebuild frontmatter with proper YAML
    new_fm_block = build_frontmatter_yaml(fm)
    new_fm_block_with_close = new_fm_block + "\n"

    # Update wikilinks
    new_body = update_wikilinks(body, all_targets)

    new_content = new_fm_block_with_close + "\n" + new_body

    if new_content == text:
        return False

    filepath.write_text(new_content, encoding="utf-8")
    return True


def collect_targets(files: list[Path]) -> set[str]:
    """Build set of known targets: full paths and basenames."""
    targets = set()
    for f in files:
        targets.add(f.name)
        targets.add(f.stem)  # without extension
        targets.add(str(f))
    return targets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", default=["."])
    args = parser.parse_args()

    files = []
    for p in args.paths:
        root = Path(p)
        if not root.exists():
            continue
        if root.is_file():
            files.append(root.resolve())
        else:
            for f in root.rglob("*.ava.okf.md"):
                if ".git" not in str(f) and ".venv" not in str(f):
                    files.append(f.resolve())

    files = sorted(set(files))
    all_targets = collect_targets(files)

    fixed = 0
    for f in files:
        if fix_file(f, all_targets):
            fixed += 1

    print(f"Fixed {fixed}/{len(files)} file(s).")


if __name__ == "__main__":
    main()
