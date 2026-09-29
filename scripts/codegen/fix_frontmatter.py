#!/usr/bin/env python3
"""fix_frontmatter.py — Fix broken YAML frontmatter in .ava.okf.md files.

Handles the case where the migration script produced invalid YAML
(e.g. unescaped quotes in description values).
Parses frontmatter manually, rebuilds with yaml.dump.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml


def parse_frontmatter_manual(text: str) -> tuple[dict, str]:
    """Parse frontmatter using simple key:value extraction (robust to YAML errors)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    # Find end of frontmatter
    fm_end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_end = i
            break

    if fm_end == -1:
        return {}, text

    body = "\n".join(lines[fm_end + 1 :]).lstrip("\n")
    fm_lines = lines[1:fm_end]

    fm = {}
    current_list = None

    for line in fm_lines:
        # Skip empty lines
        if not line.strip():
            continue
        # List continuation
        if line.startswith("  - "):
            if current_list is not None:
                current_list.append(line[4:].strip())
            continue
        m = re.match(r"^([a-zA-Z_]+):\s*(.*)$", line)
        if m:
            key = m.group(1)
            val = m.group(2).strip()
            current_list = None
            if val == "":
                # Could be start of a list
                fm[key] = []
                current_list = fm[key]
            elif val.startswith("[") and val.endswith("]"):
                # Inline list
                inner = val[1:-1]
                items = [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
                fm[key] = items
            else:
                # Strip surrounding quotes
                if (val.startswith('"') and val.endswith('"')) or (
                    val.startswith("'") and val.endswith("'")
                ):
                    val = val[1:-1]
                fm[key] = val

    return fm, body


def rebuild_file(filepath: Path) -> bool:
    text = filepath.read_text(encoding="utf-8")
    fm, body = parse_frontmatter_manual(text)

    if not fm:
        return False

    # Normalize: ensure type, title, description are present
    changed = False

    if "type" not in fm:
        fm["type"] = "doc"
        changed = True

    if "tags" in fm and not isinstance(fm["tags"], list):
        fm["tags"] = [str(fm["tags"])]
        changed = True

    # Build proper YAML frontmatter
    yaml_lines = ["---"]
    yaml_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    yaml_lines.append(yaml_str.rstrip("\n"))
    yaml_lines.append("---")

    new_content = "\n".join(yaml_lines) + "\n\n" + body

    if new_content == text and not changed:
        return False

    filepath.write_text(new_content, encoding="utf-8")
    return True


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
    fixed = 0
    for f in files:
        if rebuild_file(f):
            fixed += 1

    print(f"Fixed {fixed}/{len(files)} file(s).")


if __name__ == "__main__":
    main()
