#!/usr/bin/env python3
"""OKF v0.1 validator for the AvaMemory bundle.

Validates every .md concept file against the OKF spec.
Usage: python validate.py [--strict]
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

_RESERVED = {"MEMORY.md", "index.md", "log.md", "AGENTS.md"}
_BUNDLE = Path(__file__).resolve().parent


def validate_file(file_path: Path) -> list[str]:
    """Validate a single OKF concept file. Returns list of error messages."""
    errors: list[str] = []
    fname = file_path.name

    if not file_path.is_file():
        return [f"File not found: {file_path}"]

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ["File is not valid UTF-8"]

    if fname in _RESERVED:
        return []

    if not content.startswith("---\n"):
        return ["Missing YAML frontmatter (must start with ---)"]

    parts = content.split("---\n", 2)
    if len(parts) < 3:
        return ["Unclosed YAML frontmatter"]

    try:
        fm = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        return [f"Invalid YAML: {e}"]

    if not isinstance(fm, dict):
        return ["Frontmatter must be a YAML mapping"]

    if "type" not in fm:
        errors.append("Missing required field 'type'")
    elif not fm["type"] or not isinstance(fm["type"], str) or not fm["type"].strip():
        errors.append("Field 'type' must be a non-empty string")

    if "ava_agent" not in fm:
        errors.append("Missing required field 'ava_agent'")
    elif fm["ava_agent"] is None:
        errors.append("Field 'ava_agent' must not be null")

    if "tags" in fm:
        tags = fm["tags"]
        if not isinstance(tags, list):
            errors.append("Field 'tags' must be a list")
        else:
            for i, tag in enumerate(tags):
                if not isinstance(tag, str):
                    errors.append(f"Tag at index {i} must be a string")
            type_tags = [t for t in tags if t.startswith("type/")]
            if not type_tags:
                errors.append(
                    "Missing type tag — add exactly one of "
                    "['type/env', 'type/feedback', 'type/project', "
                    "'type/reference', 'type/role', 'type/user']"
                )
            elif len(type_tags) > 1:
                errors.append(f"{len(type_tags)} type tags ({', '.join(type_tags)}) — exactly one")

    if not fm.get("description"):
        errors.append(
            "Missing or empty 'description' — it is the only part of a note a "
            "pointer line and a search result show"
        )

    if "timestamp" in fm:
        ts = fm["timestamp"]
        if (
            isinstance(ts, str)
            and ts
            and not re.match(
                r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})?)?$",
                ts,
            )
        ):
            errors.append(f"Field 'timestamp' does not look like ISO 8601: {ts!r}")

    return errors


# --- Directory structure limits (hard rules, no skip) ---
# Every directory: at most MAX_FILES_PER_DIR markdown notes and at most
# MAX_SUBDIRS_PER_DIR subdirectories. Depth is deliberately UNLIMITED — deep
# structures (e.g. school -> term -> course -> notes) are legitimate. When a
# directory exceeds a limit, restructure it (split into topical subdirs) rather
# than skirting the rule; the memory steward consolidates such refactors.

MAX_FILES_PER_DIR = 20
MAX_SUBDIRS_PER_DIR = 20


def validate_structure(bundle: Path) -> list[str]:
    """Directory-shape limits. Returns list of error strings."""
    errors: list[str] = []
    for dirpath, dirnames, filenames in os.walk(bundle):
        dp = Path(dirpath)
        if any(part.startswith(".") for part in dp.relative_to(bundle).parts):
            continue
        md_files = [f for f in filenames if f.endswith(".md")]
        n_files = len(md_files)
        n_dirs = len(dirnames)
        rel = dp.relative_to(bundle)
        label = str(rel) if str(rel) != "." else "(root)"
        if n_files > MAX_FILES_PER_DIR:
            errors.append(
                f"{label}: {n_files} md files, over the {MAX_FILES_PER_DIR} cap — "
                "split into topical subdirectories"
            )
        if n_dirs > MAX_SUBDIRS_PER_DIR:
            errors.append(
                f"{label}: {n_dirs} subdirectories, over the {MAX_SUBDIRS_PER_DIR} cap — "
                "merge or re-group"
            )
    return errors


# --- Fact-check declarations (Setup section truthfulness check) ---
# Any note (MEMORY.md's Setup section in particular) can declare a self-verifying fact
# as an HTML comment; validate.py executes it and fails the check when the fact
# no longer holds. This is the backstop for stale claims like "develop branch
# integration" surviving for weeks after the branch was deleted (2026-08-05).
#
#   <!-- fact-check: test -d ~/Ava -->                       # command exit 0 = fact holds
#   <!-- fact-check: ! git -C ~/Ava rev-parse --verify develop -->   # ! negates: command exit 0 = fact FAILS
#
# Only a whitelist of read-only commands is allowed (git / test / ls), so a
# fact-check can never mutate anything. "~" is expanded. At most
# _FACTCHECK_MAX per file keeps runtime bounded.

_FACTCHECK_ALLOWED = {"git", "test", "ls"}
_FACTCHECK_MAX = 20
_FACTCHECK_RE = re.compile(r"<!--\s*fact-check:\s*(!)?\s*([^\n]+?)\s*-->")


def validate_factchecks(bundle: Path) -> list[str]:
    """Execute declared fact-checks; return list of error strings."""
    errors: list[str] = []
    for fp in sorted(bundle.rglob("*.md")):
        try:
            content = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        count = 0
        for m in _FACTCHECK_RE.finditer(content):
            count += 1
            if count > _FACTCHECK_MAX:
                errors.append(f"{fp.name}: over {_FACTCHECK_MAX} fact-checks — trim")
                break
            negate = bool(m.group(1))
            cmdline = m.group(2).strip()
            parts = cmdline.split()
            if not parts or parts[0] not in _FACTCHECK_ALLOWED:
                errors.append(
                    f"{fp.name}: fact-check command not whitelisted: {cmdline[:60]!r} "
                    f"(allowed: {sorted(_FACTCHECK_ALLOWED)})"
                )
                continue
            argv = [str(Path(a).expanduser()) for a in parts]
            try:
                # S603: whitelist above (git/test/ls only) keeps this read-only
                rc = subprocess.run(  # noqa: S603
                    argv, capture_output=True, timeout=15, check=False
                ).returncode
                ok = (rc == 0) != negate
            except Exception:
                ok = False
            if not ok:
                errors.append(f"{fp.name}: fact-check FAILED: {'!' if negate else ''}{cmdline}")
    return errors


# --- Pointer integrity (MEMORY.md index vs actual notes) ---
# The index must point at every note exactly once, and every pointer target
# must exist. Catches hand-edited pointers to renamed/missing files and
# duplicate lines (both observed in production, 2026-08).

_POINTER_RE = re.compile(r"\]\(([^)#]+?\.md)\)")


def validate_pointers(bundle: Path) -> list[str]:
    """MEMORY.md pointer targets exist, unique, and cover every note."""
    errors: list[str] = []
    mem = bundle / "MEMORY.md"
    if not mem.is_file():
        return errors
    try:
        content = mem.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["MEMORY.md unreadable"]

    seen: dict[str, int] = {}
    for m in _POINTER_RE.finditer(content):
        t = m.group(1)
        if t.startswith(("http", "https", "#")):
            continue
        seen[t] = seen.get(t, 0) + 1

    for t, n in seen.items():
        if n > 1:
            errors.append(f"MEMORY.md: duplicate pointer {t} ({n}x)")
        if not (bundle / t).exists():
            errors.append(f"MEMORY.md: pointer target missing: {t}")

    for fp in sorted(bundle.rglob("*.md")):
        if fp.name in _RESERVED:
            continue
        rel = fp.relative_to(bundle).as_posix()
        if rel not in seen:
            errors.append(f"orphan note (no pointer in MEMORY.md): {rel}")

    return errors


def main() -> int:
    errors: list[str] = []
    errors.extend(validate_structure(_BUNDLE))
    errors.extend(validate_factchecks(_BUNDLE))
    errors.extend(validate_pointers(_BUNDLE))
    md_files = sorted(_BUNDLE.rglob("*.md"))

    if not md_files:
        print("No .md files found.", file=sys.stderr)
        return 1

    for fp in md_files:
        file_errors = validate_file(fp)
        for e in file_errors:
            errors.append(f"{fp.name}: {e}")

    if errors:
        print(f"OKF validation failed ({len(errors)} errors):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    print(f"OKF validation passed ({len(md_files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
