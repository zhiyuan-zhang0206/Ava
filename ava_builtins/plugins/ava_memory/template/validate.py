#!/usr/bin/env python3
"""OKF v0.2-core validator for the AvaMemory bundle (Ava extensions documented in AGENTS.md).

Validates every .md concept file against the OKF spec.
Usage: python validate.py [--strict]

Template seed copy: kept in sync with the live pool validator so a fresh pool
never passes locally while the pool's pre-commit hook would block. Last sync:
2026-09-10 (live copy: the pool checkout's validate.py). Re-sync this seed
whenever the pool validator gains rules.
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
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LEGACY_OFFSETLESS_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
_OFFSET_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$")


_FM_KEY_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*): (.*)$")
_MISREAD_STRING_KEYS = ("title", "description", "name", "type", "ava_machine", "timestamp")


def _misread_frontmatter_keys(fm_raw: str) -> list[str]:
    """Return keys whose raw line text differs from the YAML-parsed string value.

    An unquoted ' #' or ': ' inside a value silently truncates it (comment /
    nested mapping), so the parsed value drops the tail (2026-09-10: 143 pool
    files affected). Writer-side quoting is the real fix; this is the backstop.
    """
    bad: list[str] = []
    for line in fm_raw.splitlines():
        m = _FM_KEY_LINE_RE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if key not in _MISREAD_STRING_KEYS or not raw or raw.startswith(('"', "'", "|", ">")):
            continue
        try:
            parsed = yaml.safe_load(line)
        except yaml.YAMLError:
            continue
        value = parsed.get(key) if isinstance(parsed, dict) else None
        if isinstance(value, str) and value != raw:
            bad.append(key)
    return bad


def validate_file(file_path: Path) -> list[str]:  # noqa: PLR0915
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

    for key in _misread_frontmatter_keys(parts[1]):
        errors.append(
            f"Field '{key}' is YAML-misread: raw text differs from the parsed value "
            f"(an unquoted ' #' or ': ' truncates it) — wrap the value in double quotes"
        )

    if "timestamp" in fm:
        ts = fm["timestamp"]
        if isinstance(ts, str):
            if _LEGACY_OFFSETLESS_DATETIME_RE.fullmatch(ts):
                print(
                    f"WARNING: {fname}: legacy timestamp without offset — add one",
                    file=sys.stderr,
                )
            elif not (_DATE_ONLY_RE.fullmatch(ts) or _OFFSET_DATETIME_RE.fullmatch(ts)):
                errors.append(f"Field 'timestamp' does not look like ISO 8601: {ts!r}")

    if "generated" in fm:
        generated = fm["generated"]
        if not isinstance(generated, dict):
            errors.append("Field 'generated' must be a mapping")
        else:
            generated_by = generated.get("by")
            if not isinstance(generated_by, str) or not generated_by.strip():
                errors.append("Field 'generated.by' must be a non-empty string")
            if "at" in generated:
                generated_at = generated["at"]
                if not (
                    isinstance(generated_at, str) and _OFFSET_DATETIME_RE.fullmatch(generated_at)
                ):
                    errors.append(
                        f"Field 'generated.at' does not look like ISO 8601: {generated_at!r}"
                    )

    return errors


# --- Directory structure limits (hard rules, no skip) ---
# Every directory: at most MAX_FILES_PER_DIR markdown notes and at most
# MAX_SUBDIRS_PER_DIR subdirectories. Depth is deliberately UNLIMITED — deep
# structures (e.g. school -> term -> course -> notes) are legitimate. When a
# directory exceeds a limit, restructure it (split into topical subdirs) rather
# than skirting the rule; the memory steward consolidates such refactors.

MAX_FILES_PER_DIR = 20
MAX_SUBDIRS_PER_DIR = 20


def validate_type_tag(bundle: Path) -> list[str]:
    """Tag discipline (user ruling 2026-08-30): every note must carry exactly one
    type/<x> tag, and no free-form junk tags (project/role/status/repo/archived)."""
    errors: list[str] = []
    junk = {"project", "role", "status", "repo", "archived"}
    for fp in bundle.rglob("*.md"):
        if fp.name in _RESERVED or any(
            part.startswith(".") for part in fp.relative_to(bundle).parts
        ):
            continue
        try:
            content = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not content.startswith("---\n"):
            continue
        parts = content.split("---\n", 2)
        if len(parts) < 3:
            continue
        try:
            fm = yaml.safe_load(parts[1])
        except yaml.YAMLError:
            continue
        if not isinstance(fm, dict):
            continue
        tags = fm.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list):
            tags = []
        tags_l = [str(t) for t in tags]
        rel = str(fp.relative_to(bundle))
        types = [t for t in tags_l if t.startswith("type/")]
        valid = {
            "type/user",
            "type/feedback",
            "type/project",
            "type/reference",
            "type/env",
            "type/role",
        }
        for t in types:
            if t not in valid:
                errors.append(f"{rel}: invalid type tag '{t}' — use one of {sorted(valid)}")
        if not types:
            errors.append(f"{rel}: missing type/<x> tag (user ruling 2026-08-30 tag discipline)")
        if len(types) > 1:
            errors.append(f"{rel}: multiple type tags {types} — keep exactly one")
        for j in junk:
            if j in tags_l:
                errors.append(f"{rel}: junk tag '{j}' — drop it")
    return errors


def validate_project_home(bundle: Path) -> list[str]:
    """Organizational rule (user ruling 2026-08-30): notes tagged with a single
    project word must live inside that project's tree (projects/<p>/).
    Exempt: multi-project tags, cross-domain tags (ava / ava-internal /
    shared-tech / open-source)."""
    errors: list[str] = []
    projects_dir = bundle / "projects"
    project_words = (
        sorted(d.name for d in projects_dir.iterdir() if d.is_dir() and not d.name.startswith("."))
        if projects_dir.is_dir()
        else []
    )
    if not project_words:
        return errors
    cross_words = {"ava", "ava-internal", "shared-tech", "open-source"}
    for fp in bundle.rglob("*.md"):
        if fp.name in _RESERVED or any(
            part.startswith(".") for part in fp.relative_to(bundle).parts
        ):
            continue
        try:
            content = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not content.startswith("---\n"):
            continue
        parts = content.split("---\n", 2)
        if len(parts) < 3:
            continue
        try:
            fm = yaml.safe_load(parts[1])
        except yaml.YAMLError:
            continue
        if not isinstance(fm, dict):
            continue
        tags = fm.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list):
            continue
        tags_l = [str(t).lower() for t in tags]
        hits = [w for w in project_words if any(w in t for t in tags_l)]
        if not hits:
            continue
        rel = str(fp.relative_to(bundle))
        if rel.startswith("projects/"):
            continue
        if len(hits) >= 2:
            continue
        if any(w in tags_l for w in cross_words):
            continue
        errors.append(
            f"{rel}: tag contains project '{hits[0]}' but the note is not under "
            f"projects/{hits[0]}/ — project-specific notes belong in the project tree "
            "(user ruling 2026-08-30); drop the project tag for cross-project knowledge"
        )
    return errors


def validate_structure(bundle: Path) -> list[str]:
    """Directory-shape limits. Returns list of error strings."""
    errors: list[str] = []
    for dirpath, dirnames, filenames in os.walk(bundle):
        dp = Path(dirpath)
        if any(part.startswith(".") for part in dp.relative_to(bundle).parts):
            continue
        md_files = [f for f in filenames if f.endswith(".md") and f not in ("index.md", "log.md")]
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


# --- Fact-check declarations (Setup section truthiness checks) ---
# Any note (the MEMORY.md Setup section in particular) can declare a self-verifying fact
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


_POINTER_LINE_RE = re.compile(r"\[([^\]]+)\]\(([^)#]+?\.md)\)")


def _pointer_lines(content: str) -> list[tuple[str, str]]:
    """Return (title, target) pairs from markdown pointer lines."""
    return [(m.group(1), m.group(2)) for m in _POINTER_LINE_RE.finditer(content)]


def _note_frontmatter_title(fp: Path) -> str | None:
    """Frontmatter `title` of a note file; None when absent/unreadable."""
    try:
        content = fp.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not content.startswith("---\n"):
        return None
    parts = content.split("---\n", 2)
    if len(parts) < 3:
        return None
    try:
        fm = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    if isinstance(fm, dict) and fm.get("title"):
        return str(fm["title"]).strip()
    return None


def _expected_index_title(fp: Path) -> str | None:
    """Title an index.md pointer must carry for this note.

    gen_indexes convention: frontmatter title when present, else filename stem.
    """
    ft = _note_frontmatter_title(fp)
    if ft is not None:
        return ft
    return fp.name[:-3] if fp.name.endswith(".md") else None


def _norm_title(s: str) -> str:
    return re.sub(r"\s+", "", s)


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

    # Title consistency: the bracketed title must equal the note's frontmatter
    # title (MEMORY.md is injected into every agent's context; a stale or
    # wrong title misleads exactly like a wrong target).
    for title, tgt in _pointer_lines(content):
        if tgt.startswith(("http", "https", "#")):
            continue
        target = bundle / tgt
        if not target.is_file() or target.name in _RESERVED:
            continue
        ft = _note_frontmatter_title(target)
        if ft is None:
            continue
        if _norm_title(title) != _norm_title(ft):
            errors.append(f"MEMORY.md: pointer title mismatch for {tgt}: {title!r} != {ft!r}")

    # Root-level notes: file-level coverage. Everything under a directory is
    # covered by that directory's pointer (progressive disclosure via index.md).
    for fp in sorted(bundle.glob("*.md")):
        if fp.name in _RESERVED:
            continue
        rel = fp.name
        if rel not in seen:
            errors.append(f"orphan note (no pointer in MEMORY.md): {rel}")

    # Directories holding notes need a pointer line to them (or their index.md).
    dirs_with_notes = set()
    for fp in bundle.rglob("*.md"):
        if fp.name in _RESERVED or fp.parent == bundle:
            continue
        rel_dir = fp.relative_to(bundle).parent.as_posix()
        dirs_with_notes.add(rel_dir)
        while "/" in rel_dir:
            rel_dir = rel_dir.rsplit("/", 1)[0]
            dirs_with_notes.add(rel_dir)
    for d in sorted(dirs_with_notes):
        if "/" in d:
            continue  # subdirectories are enumerated by their parent's index.md
        pointed = any(
            t == d + "/" or t == d + "/index.md" or t.startswith(d + "/index.md") for t in seen
        )
        if not pointed:
            errors.append(f"directory without pointer: {d}/")

    return errors


def validate_indexes(bundle: Path) -> list[str]:
    """OKF §8: any directory holding notes needs an index.md (no frontmatter)."""
    errors: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(bundle):
        dp = Path(dirpath)
        if any(part.startswith(".") for part in dp.relative_to(bundle).parts):
            continue
        mds = [
            f
            for f in filenames
            if f.endswith(".md") and f not in ("index.md", "log.md", "MEMORY.md", "AGENTS.md")
        ]
        if not mds:
            continue
        idx = dp / "index.md"
        if not idx.is_file():
            errors.append(
                f"{dp.relative_to(bundle)}: has {len(mds)} notes but no index.md (OKF §8)"
            )
            continue
        ic = idx.read_text(encoding="utf-8", errors="replace")
        if ic.startswith("---"):
            errors.append(
                f"{dp.relative_to(bundle)}/index.md: index files must have no frontmatter (OKF §8)"
            )
        # Index ↔ entry consistency (audit 3249 / task #1440): pointer targets
        # exist, titles follow the generator convention, no orphan entries,
        # no duplicate pointers. One wrong index line = one wrong line at
        # cold start for every agent that reads it.
        pointed: dict[str, int] = {}
        for title, tgt in _pointer_lines(ic):
            if tgt.startswith(("http", "https", "#")):
                continue
            target = dp / tgt
            if not target.exists():
                errors.append(f"{dp.relative_to(bundle)}/index.md: pointer target missing: {tgt}")
                continue
            if target.name in ("index.md", "log.md"):
                continue  # directory pointer, no title check
            pointed[target.name] = pointed.get(target.name, 0) + 1
            expected = _expected_index_title(target)
            if expected is None:
                continue
            if _norm_title(title) != _norm_title(expected):
                errors.append(
                    f"{dp.relative_to(bundle)}/index.md: pointer title mismatch "
                    f"for {tgt}: {title!r} != {expected!r}"
                )
        for fname, n in pointed.items():
            if n > 1:
                errors.append(
                    f"{dp.relative_to(bundle)}/index.md: duplicate pointer {fname} ({n}x)"
                )
        for f in mds:
            if f not in pointed:
                errors.append(f"{dp.relative_to(bundle)}/index.md: orphan entry (no pointer): {f}")
    return errors


def main() -> int:
    errors: list[str] = []
    errors.extend(validate_structure(_BUNDLE))
    errors.extend(validate_project_home(_BUNDLE))
    errors.extend(validate_type_tag(_BUNDLE))
    errors.extend(validate_indexes(_BUNDLE))
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
