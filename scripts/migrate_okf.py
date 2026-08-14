#!/usr/bin/env python3
"""migrate_okf.py — Convert legacy .okf.md files to Ava OKF format (.ava.okf.md).

Actions:
  1. Parse YAML frontmatter
  2. Strip forbidden keys: cluster, layer, process, owner, views, parent
  3. Set type: doc (or memory if under memory/ dir)
  4. Extract title from first H1 heading (or filename)
  5. Extract description from "What Is It" section (or first paragraph)
  6. Rename .okf.md → .ava.okf.md
  7. Delete old files

Usage:
    .venv/bin/python scripts/migrate_okf.py [--dry-run] [paths...]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

FORBIDDEN_KEYS = {"cluster", "layer", "process", "owner", "views", "parent"}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_text = "\n".join(lines[1:i])
            try:
                fm = yaml.safe_load(fm_text) or {}
            except yaml.YAMLError:
                return {}, text
            if not isinstance(fm, dict):
                fm = {}
            return fm, "\n".join(lines[i + 1 :]).lstrip("\n")
    return {}, text


def extract_title(body: str, filename: str) -> str:
    """Extract title from first H1, or derive from filename."""
    m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    if m:
        return m.group(1).strip()
    # Derive from filename: "agent-runtime.ava.okf.md" → "Agent Runtime"
    stem = Path(filename).stem.replace(".ava.okf", "").replace(".okf", "")
    return stem.replace("-", " ").replace("_", " ").title()


def extract_description(body: str) -> str:
    """Extract description from 'What Is It' section, or first non-empty paragraph after H1."""
    # Try "What Is It" section first
    m = re.search(
        r"^##\s+what\s+is\s+it\s*$\n+(.+?)(?=\n##\s|\n#\s|\Z)",
        body,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if m:
        lines = [ln.strip() for ln in m.group(1).strip().split("\n") if ln.strip()]
        if lines:
            return " ".join(lines)[:300]  # cap at 300 chars

    # Fall back to first paragraph after H1
    lines = body.split("\n")
    _in_para = False
    para = []
    for line in lines:
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            if para:
                break
            continue
        if line.strip() == "":
            if para:
                break
            continue
        para.append(line.strip())

    if para:
        return " ".join(para)[:300]

    return ""


def build_frontmatter(fm: dict, body: str, filename: str, *, is_memory: bool = False) -> str:
    """Build new frontmatter YAML block."""
    new_fm = {}

    # type
    new_fm["type"] = "memory" if is_memory else "doc"

    # title
    new_fm["title"] = fm.get("title") or extract_title(body, filename)

    # description
    new_fm["description"] = fm.get("description") or extract_description(body)

    # tags: keep existing, ensure it's a list
    if "tags" in fm:
        tags = fm["tags"]
        if isinstance(tags, list):
            new_fm["tags"] = [str(t) for t in tags]
        elif isinstance(tags, str):
            new_fm["tags"] = [tags]
    # Optionally derive tags from old cluster/layer/process
    else:
        derived = []
        for k in ("cluster", "layer", "process"):
            if k in fm and fm[k] not in ("unknown", "overview", None, ""):
                derived.append(str(fm[k]))
        if derived:
            new_fm["tags"] = derived

    # Preserve timestamp if present
    if "timestamp" in fm:
        new_fm["timestamp"] = fm["timestamp"]

    # Build YAML
    lines = ["---"]
    for k, v in new_fm.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        elif isinstance(v, str) and ("'" in v or '"' in v or ":" in v):
            lines.append(f'{k}: "{v}"')
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def migrate_file(filepath: Path, *, dry_run: bool = False) -> str | None:
    """Migrate one .okf.md file. Returns new path or None if skipped."""
    if not filepath.name.endswith(".okf.md"):
        return None

    text = filepath.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)

    # Determine if this is a memory file
    is_memory = "memory" in str(filepath).lower() or fm.get("type") == "Memory"

    # Build new frontmatter
    new_fm_block = build_frontmatter(fm, body, filepath.name, is_memory=is_memory)

    # Build new content: frontmatter + body (H1 stripped from body since title is in FM)
    # Actually, keep H1 in body - the viz can render it. Frontmatter title is for listing.
    new_content = new_fm_block + "\n\n" + body

    # New filename
    new_name = filepath.name.replace(".okf.md", ".ava.okf.md")
    new_path = filepath.parent / new_name

    if dry_run:
        print(f"  [DRY RUN] {filepath.name} → {new_name}")
        print(f"    title: {extract_title(body, filepath.name)}")
        print(f"    desc:  {extract_description(body)[:80]}...")
        return str(new_path)

    # Write new file
    new_path.write_text(new_content, encoding="utf-8")

    # Delete old file
    filepath.unlink()

    return str(new_path)


def main():
    parser = argparse.ArgumentParser(description="Migrate .okf.md files to .ava.okf.md format")
    parser.add_argument("paths", nargs="*", help="Files or directories to migrate")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change")
    args = parser.parse_args()

    if not args.paths:
        args.paths = ["."]

    files = []
    for p in args.paths:
        root = Path(p)
        if not root.exists():
            print(f"Path not found: {p}", file=sys.stderr)
            continue
        if root.is_file() and root.suffix == ".md":
            files.append(root.resolve())
        else:
            for f in root.rglob("*.okf.md"):
                if ".git" not in str(f) and ".venv" not in str(f):
                    files.append(f.resolve())

    files = sorted(set(files))
    if not files:
        print("No .okf.md files found.")
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Found {len(files)} .okf.md file(s)\n")

    migrated = 0
    for f in files:
        new_path = migrate_file(f, dry_run=args.dry_run)
        if new_path:
            migrated += 1

    print(f"\n{'Would migrate' if args.dry_run else 'Migrated'} {migrated} file(s).")


if __name__ == "__main__":
    main()
