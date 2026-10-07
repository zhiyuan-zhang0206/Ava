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
     via scripts.codegen.build_okf_data.resolve_wikilink against the whole repo, so linting a
     subtree does not false-positive on cross-domain links. A `[[wikilink]]` is
     the node-graph edge syntax, so its universe is the .ava.okf.md files and
     nothing else — a target that names a real file on one of the other three
     doc axes (docs/decisions/, future/, docs/conventions/) is reported
     with that as the reason, because the remedy is a normal markdown link
     rather than a new node. Inline code spans and fenced blocks are not links
     (the code-sample exemption from check_doc_references): a target quoted as
     code documents the syntax and is not scanned.
  9. Overview position: a directory's overview node has exactly one
     position — <dir>/<dir>.ava.okf.md, inside the directory it describes
     (user ruling: "put it inside the folder"), judged on the logical path,
     so a node in a package's docs/ layer counts as sitting where the layer
     sits. A directory counts as existing when it is on disk or when any
     node's logical path lies under it: the nodes of a doc-only directory
     sit in <package>/docs/<dir>/, where no <package>/<dir>/ exists.
     compute_parent does not resolve the sibling position <dir>.ava.okf.md
     at the parent level; E009 fires on any sibling — merge or move it
     into the directory (one concept, one node).
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
 12. Concatenation (block): a header or bullet marker glued directly onto the
     text before it, with no blank line / newline between them. This is the
     defect class found across the W010 okf-split campaign — replacing a
     section with "summary sentence + [[wikilink]]" dropped the blank line
     before the next header or bullet, so it renders as part of the previous
     line instead of its own block (e.g. "...write-path.ava.okf.md]].## Next
     Section" — the header never renders). Detection excludes fenced code and
     inline code spans and is anchored on punctuation immediately before the
     marker, so it does not fire on a header's own repeated '#' or on an
     inline mention like "C#".
 13. Duplicate consecutive headers (block): the same header line appearing
     twice in a row (blank lines between are fine) — the other shape the same
     campaign produced.
 14. Docs layer (block): every node sits in a `docs/` layer — a directory
     segment named `docs` in its path — beside the code it describes, so the
     source tree holds code and its `tests/` and `docs/`, nothing else. `okf/`
     (the index layer) and `.github/` are exempt. E014 names the location the
     node belongs in: `docs/` under the nearest directory above it that holds
     code, or under the node's own directory when none does.

Usage:
    .venv/bin/python scripts/content_lint/lint_ava_okf.py [--fix] [paths...]
    An explicit path that does not exist is an error (stderr + exit 1) rather
    than the "No .ava.okf.md files found." message with exit 0.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from base.host.env.dotenv_boot import enter_scratch_home  # noqa: E402

if __name__ == "__main__":
    enter_scratch_home()
from base.packages.docs.okf_graph import logical_path  # noqa: E402
from scripts.codegen.build_okf_data import resolve_wikilink  # noqa: E402

# ── config ──────────────────────────────────────────────────────────
ALLOWED_TYPES = {"doc", "memory"}
FORBIDDEN_KEYS = {"cluster", "layer", "process", "owner", "views", "parent"}
MAX_LINES = 200
# The character ceiling is the one that binds in practice: at this repo's prose
# density no node has ever approached MAX_LINES. Raised from 6000 once the
# distribution showed nodes stacked in the last few characters below it — the
# fingerprint of trimming facts away to fit rather than of content that had
# genuinely outgrown one topic. Rationale + the rejected alternatives:
# docs/decisions/engineering/design/simplification/2026-07-29-okf-node-ceiling.md.
MAX_CHARS = 8000
# Remaining room below MAX_CHARS at which W010 starts reporting. Sized at a
# couple of the corpus's larger paragraphs, so an author is told about the wall
# an addition or two before hitting it, not on the addition that hits it.
WARN_MARGIN = 800
# The ceiling exists to force hierarchy, so the message names the remedy and
# where it goes: children of the overview `<stem>/<stem>.ava.okf.md` are the
# files in `<stem>/`, because compute_parent derives the edge from the path.
# The docs/ layer counts as the package directory, so on disk that is `{home}`.
_SPLIT_HINT = (
    "Split a section into its own node under '{stem}/' (the filesystem derives "
    "the parent edge; the docs/ layer stands for its package directory, so here "
    "that is '{home}') — see docs/conventions/doc-maintenance.md."
)
# The index layer and the CI overview keep their nodes outside a docs/ layer.
_LAYER_EXEMPT = ("okf/", ".github/")
# A directory "holds code" (rule 14's anchor) when it directly contains one of these.
_CODE_SUFFIXES = frozenset({".py", ".pyi", ".ts", ".tsx"})
_CODE_NAMES = frozenset({"SKILL.md", "package.json"})
# Same syntax as build_okf_data.WIKILINK_RE: [[target]] or [[target|label]]
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")

# Rule 12 masking: fenced code blocks and inline code spans are blanked out
# (line-count-preserving) before the concatenation scan, so a Python "#
# comment" or a `# Capabilities` name quoted in prose never trips it.
_FENCE_RE = re.compile(r"^\s*```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# Rule 12: a header/bullet marker glued directly onto the preceding text with
# no line break between them — the missing-blank-line concatenation defect.
# Both lookbehinds are deliberately narrow to stay zero-false-positive:
#   - HEADER: the char right before the marker must be punctuation/bracket-
#     like — never a letter, digit, or another '#'. Excluding letters/digits
#     rules out inline mentions like "C#"/"F#"; excluding '#' rules out a
#     legitimate header's own repeated '#' run (matching mid-hash).
#   - BULLET: the char right before must be non-whitespace and not another
#     '-', so a horizontal rule / spaced em-dash-as-double-hyphen is not
#     flagged, but a bullet glued onto the end of a word still is (the
#     observed shape: "...must not import agent kernel- File line budget...").
_HEADER_GLUE_RE = re.compile(r"(?<=[^\sA-Za-z0-9#])#{1,6} (?=[A-Za-z0-9`(])")
_BULLET_GLUE_RE = re.compile(r"(?<=[^\s-])- (?=[A-Za-z0-9`(*])")

# Rule 13: two identical header lines back to back (blank lines allowed
# between, since that is the normal spacing between two real sections).
_HEADER_LINE_RE = re.compile(r"^#{1,6} ")

# Directories the axis-search rglob skips (mirrors okf_graph.find_files'
# hidden-dir policy plus the heavy trees a whole-repo walk would drag in).
_NON_NODE_EXCLUDES = frozenset(
    {"node_modules", "tmp", ".next", "runs", "logs", "outputs", ".cache"}
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


def _dual_node_error(filepath: Path, repo_root: Path, all_paths: set[str]) -> LintError | None:
    """Rule 9: the two positions of a directory's overview node must not coexist.

    The canonical position is inside the directory — `<dir>/<dir>.ava.okf.md`
    (user ruling 2026-08-12: "put it inside the folder"), or in its `docs/`
    layer, which the hierarchy ignores (`logical_path`). compute_parent does
    not resolve the sibling `<dir>.ava.okf.md` at the parent level, so a
    sibling file would be an orphan node, not a parent. E009 reports any
    sibling, layered or not — move it inside (or merge it, when the internal
    file already exists).

    `<dir>` exists when it is a directory on disk or when any node's logical
    path lies under it: a doc-only directory keeps its nodes in
    `<package>/docs/<dir>/`, so no `<package>/<dir>/` exists, and a sibling
    `<package>/docs/<dir>.ava.okf.md` would otherwise slip through.

    Matching is done on the repo-relative path only, so the checkout
    directory's own name cannot misfire (CI checks out to .../ava/ava; the
    file `ava/docs/ava.ava.okf.md` is the internal overview of `ava/`, not a
    duplicate of itself).
    """
    stem = filepath.name[: -len(".ava.okf.md")]
    try:
        rel = filepath.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return None
    logical = logical_path(rel)
    # `<parent>/<stem>/` is the directory this file would be the overview of.
    parent = Path(logical).parent
    # A repo-root file's parent is `.`, whose prefix is empty.
    prefix = "" if parent == Path() else f"{parent.as_posix()}/"
    on_disk = (repo_root / parent / stem).is_dir()
    in_graph = any(logical_path(p).startswith(f"{prefix}{stem}/") for p in all_paths if p != rel)
    if not (on_disk or in_graph):
        return None
    if on_disk:
        layer = "docs/" if logical != rel else ""
        internal = f"{prefix}{stem}/{layer}{stem}.ava.okf.md"
    else:
        # The directory exists only in logical space, in this file's own layer.
        internal = posixpath.join(posixpath.dirname(rel), stem, f"{stem}.ava.okf.md")
    return LintError(
        str(filepath),
        1,
        "E009",
        f"Misplaced overview: this sibling must live at '{internal}', the "
        f"canonical position (a directory's overview lives inside it, in its "
        f"docs/ layer when layered). Move the file inside — or merge it, if "
        f"'{internal}' exists.",
    )


def _holds_code(directory: Path) -> bool:
    return any(
        entry.is_file() and (entry.suffix in _CODE_SUFFIXES or entry.name in _CODE_NAMES)
        for entry in directory.iterdir()
    )


def _layer_home(rel: str, repo_root: Path) -> str:
    """Where a node outside any docs/ layer belongs: `docs/` under the nearest
    directory above it that holds code, else under its own directory."""
    directories = posixpath.dirname(rel).split("/") if "/" in rel else []
    for depth in range(len(directories), 0, -1):
        anchor = "/".join(directories[:depth])
        if _holds_code(repo_root / anchor):
            return f"{anchor}/docs/{posixpath.relpath(rel, anchor)}"
    head, name = posixpath.split(rel)
    return posixpath.join(head, "docs", name)


def _layer_error(filepath: Path, repo_root: Path) -> LintError | None:
    """Rule 14: a node outside `okf/` and `.github/` sits in a `docs/` layer."""
    try:
        rel = filepath.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return None
    if rel.startswith(_LAYER_EXEMPT) or logical_path(rel) != rel:
        return None
    return LintError(
        str(filepath),
        1,
        "E014",
        f"Node outside a docs/ layer: OKF nodes live in a docs/ directory beside "
        f"the code they describe (okf/ and .github/ are exempt). Move it to "
        f"'{_layer_home(rel, repo_root)}'.",
    )


def _placement_errors(filepath: Path, repo_root: Path, all_paths: set[str]) -> list[LintError]:
    """Rule 9 (overview position) and rule 14 (docs layer) for one node."""
    found = (_dual_node_error(filepath, repo_root, all_paths), _layer_error(filepath, repo_root))
    return [error for error in found if error is not None]


def _repo_rel(filepath: Path, repo_root: Path) -> str:
    """The node's repo-relative posix path; its bare name outside the repo."""
    try:
        return filepath.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return filepath.name


def _split_home(rel: str, stem: str) -> str:
    """The directory, on disk, that a split-out child of `rel` belongs in: an
    overview's children sit beside it, any other node's in `<stem>/` next to it."""
    directory = posixpath.dirname(rel)
    if posixpath.basename(posixpath.dirname(logical_path(rel))) == stem:
        return f"{directory}/"
    return posixpath.join(directory, stem) + "/"


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
                f"(docs/decisions/, future/, docs/conventions/) with a normal "
                f"markdown link instead — see docs/conventions/doc-maintenance.md.",
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


def _tags_errors(path_str: str, fm: dict) -> list[LintError]:
    """Rule 5: 'tags', when present, must be a list of strings."""
    errors: list[LintError] = []
    if "tags" not in fm:
        return errors
    tags = fm["tags"]
    if not isinstance(tags, list):
        errors.append(LintError(path_str, 1, "E005", "'tags' must be a list of strings"))
        return errors
    for i, t in enumerate(tags):
        if not isinstance(t, str):
            errors.append(
                LintError(
                    path_str, 1, "E005", f"tags[{i}] must be a string, got {type(t).__name__}"
                )
            )
    return errors


def _forbidden_key_errors(path_str: str, fm: dict) -> list[LintError]:
    """Rule 6: none of the forbidden frontmatter keys may be present."""
    errors: list[LintError] = []
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
    return errors


def _mask_code(text: str) -> str:
    """Blank out fenced code blocks and inline code spans, preserving line count
    and column offsets, so rule 12's scan never fires inside code."""
    lines = text.split("\n")
    out = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        if in_fence:
            out.append("")
            continue
        out.append(_INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), line))
    return "\n".join(out)


def _concat_glue_errors(path_str: str, text: str, fm_end_line: int) -> list[LintError]:
    """Rule 12: header/bullet markers glued to the preceding text. Scans from
    `fm_end_line` on (skipping the YAML frontmatter) using real line numbers
    from the raw file, not the frontmatter-stripped/lstripped `body`."""
    errors: list[LintError] = []
    body_lines = text.split("\n")[fm_end_line:]
    masked = _mask_code("\n".join(body_lines))
    for i, line in enumerate(masked.split("\n")):
        line_no = fm_end_line + i + 1
        if _HEADER_GLUE_RE.search(line):
            errors.append(
                LintError(
                    path_str,
                    line_no,
                    "E012",
                    "Header glued to the preceding text with no blank line — "
                    "insert the missing blank line before the header.",
                )
            )
        if _BULLET_GLUE_RE.search(line):
            errors.append(
                LintError(
                    path_str,
                    line_no,
                    "E012",
                    "Bullet glued to the preceding text with no line break — "
                    "insert the missing newline before the bullet.",
                )
            )
    return errors


def _duplicate_header_errors(path_str: str, text: str, fm_end_line: int) -> list[LintError]:
    """Rule 13: the same header line twice in a row (blank lines between are
    fine — that is the normal spacing between two real sections)."""
    errors: list[LintError] = []
    body_lines = text.split("\n")[fm_end_line:]
    prev_header: str | None = None
    prev_line_no = 0
    for i, line in enumerate(body_lines):
        if _HEADER_LINE_RE.match(line):
            if line == prev_header:
                errors.append(
                    LintError(
                        path_str,
                        fm_end_line + i + 1,
                        "E013",
                        f"Duplicate consecutive header (also at line {prev_line_no}): "
                        f"'{line}'. Merge the two sections or give the second one its "
                        f"own heading.",
                    )
                )
            prev_header = line
            prev_line_no = fm_end_line + i + 1
        elif line.strip() != "":
            prev_header = None
    return errors


def _size_errors(path_str: str, filepath: Path, text: str, rel_path: str) -> list[LintError]:
    """Rules 7 + 10: the node is under the size ceiling, with a warning on approach."""
    errors: list[LintError] = []
    line_count = text.count("\n") + 1
    char_count = len(text)
    stem = filepath.name[: -len(".ava.okf.md")]
    hint = _SPLIT_HINT.format(stem=stem, home=_split_home(rel_path, stem))
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
    return errors


def _required_field_errors(path_str: str, fm: dict[str, Any]) -> list[LintError]:
    """Rules 2 + 3 + 4: frontmatter type, title and description."""
    errors: list[LintError] = []
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
    return errors


def lint_file(filepath: Path, all_paths: set[str], repo_root: Path) -> list[LintError]:
    errors = []
    path_str = str(filepath)

    # Rule 1: file extension
    if not filepath.name.endswith(".ava.okf.md"):
        errors.append(LintError(path_str, 1, "E001", "File must end with .ava.okf.md"))
        return errors

    # Rules 9 + 14: where the node sits.
    errors.extend(_placement_errors(filepath, repo_root, all_paths))

    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception as e:
        errors.append(LintError(path_str, 1, "E000", f"Cannot read file: {e}"))
        return errors

    rel_path = _repo_rel(filepath, repo_root)
    errors.extend(_size_errors(path_str, filepath, text, rel_path))

    # Parse frontmatter
    fm, body, fm_end_line = parse_frontmatter(text)
    if not fm:
        errors.append(
            LintError(
                path_str, 1, "E002", "Missing or invalid YAML frontmatter (must start with ---)"
            )
        )
        return errors  # can't validate further

    errors.extend(_required_field_errors(path_str, fm))

    # Rules 5 + 6: tags format, forbidden keys
    errors.extend(_tags_errors(path_str, fm))
    errors.extend(_forbidden_key_errors(path_str, fm))

    # Rules 8 + 11: wikilink targets (warn level) — same resolution as the graph builder.
    # Code samples are not links (check_doc_references' exemption, applied here with
    # the same line-preserving mask rule 12 uses): a `[[wikilink]]` quoted in an
    # inline span or a fenced block documents the syntax; it is not an edge.
    for m in WIKILINK_RE.finditer(_mask_code(body)):
        err = _wikilink_error(path_str, rel_path, m.group(1).strip(), all_paths, repo_root)
        if err is not None:
            errors.append(err)

    # Rules 12 + 13: concatenation defects (block) — operate on the raw text so
    # line numbers are exact, not on `body` (which parse_frontmatter lstrips).
    errors.extend(_concat_glue_errors(path_str, text, fm_end_line))
    errors.extend(_duplicate_header_errors(path_str, text, fm_end_line))

    return errors


def _report(all_errors: dict[str, list[LintError]], repo_root: Path, n_files: int) -> int:
    """Print every finding, per file; the error count (warnings excluded)."""
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

    print(f"\n───\n{n_files} files checked, {error_count} error(s), {warn_count} warning(s)")
    return error_count


def main():
    parser = argparse.ArgumentParser(description="Lint Ava OKF document format")
    parser.add_argument(
        "paths", nargs="*", help="Files or directories to lint (default: repo root)"
    )
    parser.add_argument(
        "--fix", action="store_true", help="Auto-fix where possible (not yet implemented)"
    )
    args = parser.parse_args()

    missing = [p for p in args.paths if not Path(p).exists()]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

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

    error_count = _report(all_errors, repo_root, len(files))

    if error_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
