#!/usr/bin/env python3
"""lint_ava_okf.py — Lint all .ava.okf.md files against the Ava OKF format spec.

Rules (source of truth):
  1. File extension: must end with .ava.okf.md
  2. type: required, exactly "doc" or "memory"
  3. title: required, non-empty
  4. description: required, non-empty
  5. tags: if present, must be a list of strings
  6. Forbidden frontmatter keys: cluster, layer, process, owner, views, parent
  7. File size: a MAX_LINES / MAX_CHARS ceiling (forces hierarchy)
  8. Wikilinks: target .ava.okf.md files must exist (warn). Targets resolve
     via build_okf_data.resolve_wikilink against the whole repo, so linting a
     subtree does not false-positive on cross-domain links. A `[[wikilink]]` is
     the node-graph edge syntax, so its universe is the .ava.okf.md files and
     nothing else — a target that names a real file on one of the other three
     doc axes (decisions/, future/, conventions/) is reported
     with that as the reason, because the remedy is a normal markdown link
     rather than a new node.
  9. Overview position: a directory's overview node has exactly one
     position — <dir>/<dir>.ava.okf.md, inside the directory it describes
     (2026-08-12 user ruling: "put it inside the folder"). The sibling
     position <dir>.ava.okf.md at the parent level was retired 2026-08-13
     (last nested overviews moved inside; compute_parent no longer resolves
     it); E009 fires on any surviving sibling — merge or move it into the
     directory (one concept, one node).
 10. Headroom (warn): a node whose remaining room under the character ceiling is
     below WARN_MARGIN reports W010 — non-blocking, so the author sees the wall
     while there is still room to plan a split, instead of discovering it only
     once a commit is already refused. The constants below are the source of
     truth for all three thresholds; this list does not restate their values, so
     bumping one cannot leave the docstring lying.
 11. Wikilink paths (warn): a target that carries a directory component must
     denote the node it resolves to. resolve_wikilink falls back to a
     unique-basename match, which rescues a wrong path silently — and stops
     rescuing it the day a second node takes that basename, turning an
     untouched link into a W008. W011 reports the mismatch while the link still
     works, so the path is fixed rather than discovered broken later.

Usage:
    .venv/bin/python scripts/lint_ava_okf.py [--fix] [paths...]
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml
from build_okf_data import resolve_wikilink

# ── config ──────────────────────────────────────────────────────────
ALLOWED_TYPES = {"doc", "memory"}
FORBIDDEN_KEYS = {"cluster", "layer", "process", "owner", "views", "parent"}
MAX_LINES = 200
# The character ceiling is the one that binds in practice: at this repo's prose
# density no node has ever approached MAX_LINES. Raised from 6000 once the
# distribution showed nodes stacked in the last few characters below it — the
# fingerprint of trimming facts away to fit rather than of content that had
# genuinely outgrown one topic. Rationale + the rejected alternatives:
# decisions/2026-07-29-okf-node-ceiling.md.
MAX_CHARS = 8000
# Remaining room below MAX_CHARS at which W010 starts reporting. Sized at a
# couple of the corpus's larger paragraphs, so an author is told about the wall
# an addition or two before hitting it, not on the addition that hits it.
WARN_MARGIN = 800
# The ceiling exists to force hierarchy, so the message names the remedy and
# where it goes: children of the overview `<stem>/<stem>.ava.okf.md` are the
# files in `<stem>/`, because compute_parent derives the edge from the path.
_SPLIT_HINT = (
    "Split a section into its own node under '{stem}/' (the filesystem derives "
    "the parent edge) — see conventions/doc-maintenance.md."
)
# Same syntax as build_okf_data.WIKILINK_RE: [[target]] or [[target|label]]
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")

# Directories the axis-search rglob skips (mirrors okf_graph.find_files'
# hidden-dir policy plus the heavy trees a whole-repo walk would drag in).
_NON_NODE_EXCLUDES = frozenset(
    {"node_modules", "tmp", ".next", "runs", "logs", "outputs", ".pytest_cache", ".ruff_cache"}
)


class LintError:
    def __init__(self, path: str, line: int, code: str, msg: str):
        self.path = path
        self.line = line
        self.code = code
        self.msg = msg

    def __str__(self):
        return f"{self.path}:{self.line}: {self.code}: {self.msg}"


def find_files(paths: list[str]) -> list[Path]:
    """Return all .ava.okf.md files under the given paths."""
    if not paths:
        paths = ["."]
    files = []
    for p in paths:
        root = Path(p)
        if not root.exists():
            continue
        if root.is_file():
            files.append(root.resolve())
        else:
            root_resolved = root.resolve()
            for f in root.rglob("*.ava.okf.md"):
                rel_parts = f.resolve().relative_to(root_resolved).parts
                # Skip hidden dirs (.git, .venv, .claude/worktrees, ...) like
                # build_okf_data — except `.github/`, whose overview node lives
                # inside it (.github/.github.ava.okf.md).
                if any(part.startswith(".") and part != ".github" for part in rel_parts[:-1]):
                    continue
                files.append(f.resolve())
    return sorted(set(files))


def parse_frontmatter(text: str) -> tuple[dict, str, int]:
    """Parse YAML frontmatter. Returns (fm_dict, body, end_line_of_fm)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text, 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_text = "\n".join(lines[1:i])
            try:
                fm = yaml.safe_load(fm_text) or {}
            except yaml.YAMLError:
                return {}, text, i + 1  # parse error handled by caller
            if not isinstance(fm, dict):
                fm = {}
            return fm, "\n".join(lines[i + 1 :]).lstrip("\n"), i + 1
    return {}, text, 0


def collect_all_paths(repo_root: Path) -> set[str]:
    """All repo .ava.okf.md files as bundle-relative posix paths (build_okf_data's universe)."""
    return {f.relative_to(repo_root).as_posix() for f in find_files([str(repo_root)])}


def _dual_node_error(filepath: Path, repo_root: Path) -> LintError | None:
    """Rule 9: the two positions of a directory's overview node must not coexist.

    The canonical position is inside the directory — `<dir>/<dir>.ava.okf.md`,
    co-located with the code it describes (user ruling 2026-08-12: "put it
    inside the folder"). The sibling `<dir>.ava.okf.md` at the parent level
    was retired 2026-08-13: every nested overview moved inside, and
    compute_parent no longer resolves the sibling position, so a sibling
    file would be an orphan node, not a parent. E009 reports any surviving
    sibling — move it inside (or merge it, when the internal file already
    exists).

    Matching is done on the repo-relative path only, so the checkout
    directory's own name cannot misfire (CI checks out to .../ava/ava; the
    file `ava/ava.ava.okf.md` is the internal overview of `ava/`, not a
    duplicate of itself).
    """
    stem = filepath.name[: -len(".ava.okf.md")]
    try:
        rel = filepath.resolve().relative_to(repo_root)
    except ValueError:
        return None
    if len(rel.parts) < 2:
        # Repo-root file: the retired sibling position of directory `<stem>/`.
        # After the 2026-08-12 ruling the root level holds no domain
        # overviews; flag any file shaped like one (its directory exists).
        if not (repo_root / stem).is_dir():
            return None
        internal = f"{stem}/{stem}.ava.okf.md"
        return LintError(
            str(filepath),
            1,
            "E009",
            f"Misplaced overview: this root-level sibling must live at "
            f"'{internal}', the canonical position (a directory's overview "
            f"lives inside it; the sibling position was retired 2026-08-13). "
            f"Move the file inside — or merge it, if '{internal}' exists.",
        )
    # Sibling position inside a directory: `<dir>/<stem>.ava.okf.md` is the
    # retired overview position of `<dir>/<stem>/`.
    internal = f"{rel.parent.as_posix()}/{stem}/{stem}.ava.okf.md"
    if not (repo_root / rel.parent / stem).is_dir():
        return None
    return LintError(
        str(filepath),
        1,
        "E009",
        f"Misplaced overview: this sibling must live at '{internal}', the "
        f"canonical position (a directory's overview lives inside it; the "
        f"sibling position was retired 2026-08-13). Move the file inside — "
        f"or merge it, if '{internal}' exists.",
    )


def _non_node_target(target: str, repo_root: Path) -> str | None:
    """The repo-relative path of a real, non-node markdown file this wikilink target
    names, or None. Searched so W008 can say *why* a target that plainly exists on
    disk is not in the graph: the node universe is the .ava.okf.md files, and the
    other three doc axes are cited with markdown links instead. Looks at the target
    as a repo-root path, then by basename across the repo (skipping hidden
    and heavy dirs) — deliberately not a whole-tree walk over node_modules/.venv
    for a diagnostic."""
    if "://" in target:
        return None  # a URL is nothing on disk; resolve_wikilink rejects it outright
    name = Path(target).name
    if name.endswith(".ava.okf.md"):
        # A .ava.okf.md name that did not resolve is a missing node, not an axis mix-up.
        return None
    as_written = posixpath.normpath(target.lstrip("/"))
    if not as_written.startswith("..") and (repo_root / as_written).is_file():
        return as_written
    if not name.endswith(".md"):
        name += ".md"
    hits = [
        p
        for p in repo_root.rglob(name)
        if p.is_file()
        and not any(
            part in _NON_NODE_EXCLUDES or part.startswith(".")
            for part in p.relative_to(repo_root).parts
        )
    ]
    if len(hits) != 1:
        return None
    return hits[0].relative_to(repo_root).as_posix()


def _literal_denotations(rel_path: str, target: str) -> set[str]:
    """The repo-relative paths a wikilink target literally denotes — as written, and
    read relative to the citing node's directory. resolve_wikilink also accepts a
    unique-basename match, so a resolution *outside* this set means the target's
    directory component played no part (see rule 11)."""
    stem = target.lstrip("/")
    if not stem.endswith(".ava.okf.md"):
        stem += ".ava.okf.md"
    denotations = {stem}
    cur_dir = posixpath.dirname(rel_path)
    if cur_dir:
        denotations.add(posixpath.normpath(posixpath.join(cur_dir, stem)))
    return denotations


def _wikilink_error(
    path_str: str, rel_path: str, target: str, all_paths: set[str], repo_root: Path
) -> LintError | None:
    """Rules 8 + 11 for one `[[target]]`: the W008 miss (with the axis mix-up called
    out by name when that is what it is), the W011 wrong-path-that-still-resolves, or
    None when the link is clean."""
    resolved = resolve_wikilink(rel_path, target, all_paths)
    if resolved is None:
        non_node = _non_node_target(target, repo_root)
        if non_node is not None:
            return LintError(
                path_str,
                1,
                "W008",
                f"Wikilink target is not an OKF node: [[{target}]] is '{non_node}'. "
                f"[[wikilinks]] are node-graph edges and the graph holds only "
                f"*.ava.okf.md files; cite the why / plan / how axes "
                f"(decisions/, future/, conventions/) with a normal "
                f"markdown link instead — see conventions/doc-maintenance.md.",
            )
        return LintError(path_str, 1, "W008", f"Wikilink target not found: [[{target}]]")
    if "/" in target and resolved not in _literal_denotations(rel_path, target):
        return LintError(
            path_str,
            1,
            "W011",
            f"Wikilink path does not exist: [[{target}]] resolved to '{resolved}' only "
            f"by unique-basename fallback. Write the path the node actually has "
            f"('{resolved}') or drop it to the bare basename — as written the link "
            f"breaks the moment a second node takes that basename.",
        )
    return None


def lint_file(filepath: Path, all_paths: set[str], repo_root: Path) -> list[LintError]:
    errors = []
    path_str = str(filepath)

    # Rule 1: file extension
    if not filepath.name.endswith(".ava.okf.md"):
        errors.append(LintError(path_str, 1, "E001", "File must end with .ava.okf.md"))
        return errors

    # Rule 9: overview position (see _dual_node_error).
    dual = _dual_node_error(filepath, repo_root)
    if dual is not None:
        errors.append(dual)

    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception as e:
        errors.append(LintError(path_str, 1, "E000", f"Cannot read file: {e}"))
        return errors

    # Rule 7: file size
    line_count = text.count("\n") + 1
    char_count = len(text)
    hint = _SPLIT_HINT.format(stem=filepath.name[: -len(".ava.okf.md")])
    if line_count > MAX_LINES:
        errors.append(
            LintError(
                path_str,
                line_count,
                "E007",
                f"File too long: {line_count} lines (max {MAX_LINES}). {hint}",
            )
        )
    if char_count > MAX_CHARS:
        errors.append(
            LintError(
                path_str,
                1,
                "E007",
                f"File too large: {char_count} chars (max {MAX_CHARS}). {hint}",
            )
        )
    elif MAX_CHARS - char_count < WARN_MARGIN:
        # Rule 10: report the approach, not just the breach. Naming the room left
        # is the point — it tells the author whether the section they are about to
        # add fits, which is the moment the split decision is cheap.
        errors.append(
            LintError(
                path_str,
                1,
                "W010",
                f"Approaching the size ceiling: {char_count} chars, "
                f"{MAX_CHARS - char_count} left of {MAX_CHARS}. Plan the next "
                f"section as its own node rather than trimming this one. {hint}",
            )
        )

    # Parse frontmatter
    fm, body, _fm_end_line = parse_frontmatter(text)
    if not fm:
        errors.append(
            LintError(
                path_str, 1, "E002", "Missing or invalid YAML frontmatter (must start with ---)"
            )
        )
        return errors  # can't validate further

    # Rule 2: type
    fm_type = fm.get("type")
    if not fm_type:
        errors.append(LintError(path_str, 1, "E002", "frontmatter 'type' is required"))
    elif fm_type not in ALLOWED_TYPES:
        errors.append(
            LintError(path_str, 1, "E002", f"type must be one of {ALLOWED_TYPES}, got '{fm_type}'")
        )

    # Rule 3: title
    fm_title = fm.get("title")
    if not fm_title or not str(fm_title).strip():
        errors.append(
            LintError(path_str, 1, "E003", "frontmatter 'title' is required and must be non-empty")
        )

    # Rule 4: description
    fm_desc = fm.get("description")
    if not fm_desc or not str(fm_desc).strip():
        errors.append(
            LintError(
                path_str, 1, "E004", "frontmatter 'description' is required and must be non-empty"
            )
        )

    # Rule 5: tags format
    if "tags" in fm:
        tags = fm["tags"]
        if not isinstance(tags, list):
            errors.append(LintError(path_str, 1, "E005", "'tags' must be a list of strings"))
        else:
            for i, t in enumerate(tags):
                if not isinstance(t, str):
                    errors.append(
                        LintError(
                            path_str,
                            1,
                            "E005",
                            f"tags[{i}] must be a string, got {type(t).__name__}",
                        )
                    )

    # Rule 6: forbidden keys
    for key in FORBIDDEN_KEYS:
        if key in fm:
            errors.append(
                LintError(
                    path_str,
                    1,
                    "E006",
                    f"Forbidden frontmatter key: '{key}'. "
                    f"Use 'tags' for categorization; filesystem for hierarchy.",
                )
            )

    # Rules 8 + 11: wikilink targets (warn level) — same resolution as the graph builder
    try:
        rel_path = filepath.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        rel_path = filepath.name
    for m in WIKILINK_RE.finditer(body):
        err = _wikilink_error(path_str, rel_path, m.group(1).strip(), all_paths, repo_root)
        if err is not None:
            errors.append(err)

    return errors


def main():
    parser = argparse.ArgumentParser(description="Lint Ava OKF document format")
    parser.add_argument(
        "paths", nargs="*", help="Files or directories to lint (default: repo root)"
    )
    parser.add_argument(
        "--fix", action="store_true", help="Auto-fix where possible (not yet implemented)"
    )
    args = parser.parse_args()

    repo_root = Path.cwd().resolve()
    files = find_files(args.paths or [str(repo_root)])

    if not files:
        print("No .ava.okf.md files found.")
        sys.exit(0)

    all_paths = collect_all_paths(repo_root)

    all_errors: dict[str, list[LintError]] = defaultdict(list)
    for f in files:
        errs = lint_file(f, all_paths, repo_root)
        all_errors[str(f)].extend(errs)

    # Report
    error_count = sum(1 for errs in all_errors.values() for e in errs if e.code.startswith("E"))
    warn_count = sum(1 for errs in all_errors.values() for e in errs if e.code.startswith("W"))

    for path, errs in sorted(all_errors.items()):
        if not errs:
            continue
        rel = os.path.relpath(path, repo_root)
        print(f"\n{rel}:")
        for e in errs:
            prefix = "  [ERR]" if e.code.startswith("E") else "  [WARN]"
            print(f"{prefix} {e.code}: {e.msg}")

    print(f"\n───\n{len(files)} files checked, {error_count} error(s), {warn_count} warning(s)")

    if error_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
