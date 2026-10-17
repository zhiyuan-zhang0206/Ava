#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from base.host.env.dotenv_boot import enter_scratch_home

if __name__ == "__main__":
    enter_scratch_home()

from base.packages.docs.okf_graph import logical_path
from base.packages.text_budget import LengthBudget, Level, Unit, evaluate
from scripts.codegen.build_okf_data import resolve_wikilink

# meiva:link scripts/content_lint/docs/lint-meiva-okf.meiva.okf.md
ALLOWED_TYPES = {"doc", "memory"}
FORBIDDEN_KEYS = {"cluster", "layer", "process", "owner", "views", "parent"}
# meiva:link scripts/content_lint/docs/lint-meiva-okf-size-and-links.meiva.okf.md
MAX_LINES = 200
MAX_CHARS = 8000
WARN_MARGIN = 800
_SPLIT_HINT = (
    "Split a section into its own node under '{stem}/' (the filesystem derives "
    "the parent edge; the docs/ layer stands for its package directory, so here "
    "that is '{home}') — see docs/conventions/doc-maintenance.md."
)
# meiva:link scripts/content_lint/docs/lint-meiva-okf.meiva.okf.md
_LAYER_EXEMPT = (".github/",)
_CODE_SUFFIXES = frozenset({".py", ".pyi", ".ts", ".tsx"})
_CODE_NAMES = frozenset({"SKILL.md", "package.json"})
# meiva:link scripts/content_lint/docs/lint-meiva-okf-size-and-links.meiva.okf.md
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")

# meiva:link scripts/content_lint/docs/lint-meiva-okf-size-and-links.meiva.okf.md
_FENCE_RE = re.compile(r"^\s*```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# meiva:link scripts/content_lint/docs/lint-meiva-okf-size-and-links.meiva.okf.md
_HEADER_GLUE_RE = re.compile(r"(?<=[^\sA-Za-z0-9#])#{1,6} (?=[A-Za-z0-9`(])")
_BULLET_GLUE_RE = re.compile(r"(?<=[^\s-])- (?=[A-Za-z0-9`(*])")
_HEADER_LINE_RE = re.compile(r"^#{1,6} ")

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


# meiva:link scripts/content_lint/docs/lint-meiva-okf.meiva.okf.md
def find_files(paths: list[str]) -> list[Path]:
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
            for f in root.rglob("*.meiva.okf.md"):
                rel_parts = f.resolve().relative_to(root_resolved).parts
                if any(part.startswith(".") and part != ".github" for part in rel_parts[:-1]):
                    continue
                files.append(f.resolve())
    return sorted(set(files))


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


# meiva:link scripts/content_lint/docs/lint-meiva-okf.meiva.okf.md
def collect_all_paths(repo_root: Path) -> set[str]:
    return {f.relative_to(repo_root).as_posix() for f in find_files([str(repo_root)])}


# meiva:link scripts/content_lint/docs/lint-meiva-okf.meiva.okf.md
def _dual_node_error(filepath: Path, repo_root: Path, all_paths: set[str]) -> LintError | None:
    stem = filepath.name[: -len(".meiva.okf.md")]
    try:
        rel = filepath.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return None
    logical = logical_path(rel)
    parent = Path(logical).parent
    prefix = "" if parent == Path() else f"{parent.as_posix()}/"
    on_disk = (repo_root / parent / stem).is_dir()
    in_graph = any(logical_path(p).startswith(f"{prefix}{stem}/") for p in all_paths if p != rel)
    if not (on_disk or in_graph):
        return None
    if on_disk:
        layer = "docs/" if logical != rel else ""
        internal = f"{prefix}{stem}/{layer}{stem}.meiva.okf.md"
    else:
        internal = posixpath.join(posixpath.dirname(rel), stem, f"{stem}.meiva.okf.md")
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


# meiva:link scripts/content_lint/docs/lint-meiva-okf.meiva.okf.md
def _layer_home(rel: str, repo_root: Path) -> str:
    directories = posixpath.dirname(rel).split("/") if "/" in rel else []
    for depth in range(len(directories), 0, -1):
        anchor = "/".join(directories[:depth])
        if _holds_code(repo_root / anchor):
            return f"{anchor}/docs/{posixpath.relpath(rel, anchor)}"
    head, name = posixpath.split(rel)
    return posixpath.join(head, "docs", name)


# meiva:link scripts/content_lint/docs/lint-meiva-okf.meiva.okf.md
def layer_error(filepath: Path, repo_root: Path) -> LintError | None:
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
        f"the code they describe (.github/ is exempt). Move it to "
        f"'{_layer_home(rel, repo_root)}'.",
    )


def _placement_errors(filepath: Path, repo_root: Path, all_paths: set[str]) -> list[LintError]:
    found = (_dual_node_error(filepath, repo_root, all_paths), layer_error(filepath, repo_root))
    return [error for error in found if error is not None]


def _repo_rel(filepath: Path, repo_root: Path) -> str:
    try:
        return filepath.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return filepath.name


# meiva:link scripts/content_lint/docs/lint-meiva-okf-size-and-links.meiva.okf.md
def _split_home(rel: str, stem: str) -> str:
    directory = posixpath.dirname(rel)
    if posixpath.basename(posixpath.dirname(logical_path(rel))) == stem:
        return f"{directory}/"
    return posixpath.join(directory, stem) + "/"


# meiva:link scripts/content_lint/docs/lint-meiva-okf-size-and-links.meiva.okf.md
def _non_node_target(target: str, repo_root: Path) -> str | None:
    if "://" in target:
        return None
    name = Path(target).name
    if name.endswith(".meiva.okf.md"):
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


# meiva:link scripts/content_lint/docs/lint-meiva-okf-size-and-links.meiva.okf.md
def _literal_denotations(rel_path: str, target: str) -> set[str]:
    stem = target.lstrip("/")
    if not stem.endswith(".meiva.okf.md"):
        stem += ".meiva.okf.md"
    denotations = {stem}
    cur_dir = posixpath.dirname(rel_path)
    if cur_dir:
        denotations.add(posixpath.normpath(posixpath.join(cur_dir, stem)))
    return denotations


def _wikilink_error(
    path_str: str, rel_path: str, target: str, all_paths: set[str], repo_root: Path
) -> LintError | None:
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
                f"*.meiva.okf.md files; cite the why / plan / how axes "
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


# meiva:link scripts/content_lint/docs/lint-meiva-okf.meiva.okf.md
def _tags_errors(path_str: str, fm: dict) -> list[LintError]:
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


# meiva:link scripts/content_lint/docs/lint-meiva-okf.meiva.okf.md
def _forbidden_key_errors(path_str: str, fm: dict) -> list[LintError]:
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


# meiva:link scripts/content_lint/docs/lint-meiva-okf-size-and-links.meiva.okf.md
def _mask_code(text: str) -> str:
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


# meiva:link scripts/content_lint/docs/lint-meiva-okf-size-and-links.meiva.okf.md
def _concat_glue_errors(path_str: str, text: str, fm_end_line: int) -> list[LintError]:
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


# meiva:link scripts/content_lint/docs/lint-meiva-okf-size-and-links.meiva.okf.md
def _size_errors(path_str: str, filepath: Path, text: str, rel_path: str) -> list[LintError]:
    errors: list[LintError] = []
    line_verdict = evaluate(text, LengthBudget(Unit.LINES, hard=MAX_LINES))
    line_count = line_verdict.measured
    verdict = evaluate(text, LengthBudget(Unit.CHARS, hard=MAX_CHARS, soft=MAX_CHARS - WARN_MARGIN))
    char_count = verdict.measured
    stem = filepath.name[: -len(".meiva.okf.md")]
    hint = _SPLIT_HINT.format(stem=stem, home=_split_home(rel_path, stem))
    if line_verdict.level is Level.OVER:
        errors.append(
            LintError(
                path_str,
                line_count,
                "E007",
                f"File too long: {line_count} lines (max {MAX_LINES}). {hint}",
            )
        )
    if verdict.level is Level.OVER:
        errors.append(
            LintError(
                path_str,
                1,
                "E007",
                f"File too large: {char_count} chars (max {MAX_CHARS}). {hint}",
            )
        )
    elif verdict.level is Level.SOFT:
        errors.append(
            LintError(
                path_str,
                1,
                "W010",
                f"Approaching the size ceiling: {char_count} chars, "
                f"{verdict.remaining} left of {MAX_CHARS}. Plan the next "
                f"section as its own node rather than trimming this one. {hint}",
            )
        )
    return errors


# meiva:link scripts/content_lint/docs/lint-meiva-okf.meiva.okf.md
def _required_field_errors(path_str: str, fm: dict[str, Any]) -> list[LintError]:
    errors: list[LintError] = []
    fm_type = fm.get("type")
    if not fm_type:
        errors.append(LintError(path_str, 1, "E002", "frontmatter 'type' is required"))
    elif fm_type not in ALLOWED_TYPES:
        errors.append(
            LintError(
                path_str, 1, "E002", f"type must be one of {sorted(ALLOWED_TYPES)}, got '{fm_type}'"
            )
        )

    fm_title = fm.get("title")
    if not fm_title or not str(fm_title).strip():
        errors.append(
            LintError(path_str, 1, "E003", "frontmatter 'title' is required and must be non-empty")
        )

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

    if not filepath.name.endswith(".meiva.okf.md"):
        errors.append(LintError(path_str, 1, "E001", "File must end with .meiva.okf.md"))
        return errors

    errors.extend(_placement_errors(filepath, repo_root, all_paths))

    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception as e:
        errors.append(LintError(path_str, 1, "E000", f"Cannot read file: {e}"))
        return errors

    rel_path = _repo_rel(filepath, repo_root)
    errors.extend(_size_errors(path_str, filepath, text, rel_path))

    fm, body, fm_end_line = parse_frontmatter(text)
    if not fm:
        errors.append(
            LintError(
                path_str, 1, "E002", "Missing or invalid YAML frontmatter (must start with ---)"
            )
        )
        return errors

    errors.extend(_required_field_errors(path_str, fm))

    errors.extend(_tags_errors(path_str, fm))
    errors.extend(_forbidden_key_errors(path_str, fm))

    for m in WIKILINK_RE.finditer(_mask_code(body)):
        err = _wikilink_error(path_str, rel_path, m.group(1).strip(), all_paths, repo_root)
        if err is not None:
            errors.append(err)

    errors.extend(_concat_glue_errors(path_str, text, fm_end_line))
    errors.extend(_duplicate_header_errors(path_str, text, fm_end_line))

    return errors


def _report(all_errors: dict[str, list[LintError]], repo_root: Path, n_files: int) -> int:
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


def main(argv: Sequence[str] | None = None):
    parser = argparse.ArgumentParser(description="Lint Meiva OKF document format")
    parser.add_argument(
        "paths", nargs="*", help="Files or directories to lint (default: repo root)"
    )
    parser.add_argument(
        "--fix", action="store_true", help="Auto-fix where possible (not yet implemented)"
    )
    args = parser.parse_args(argv)

    missing = [p for p in args.paths if not Path(p).exists()]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    repo_root = Path.cwd().resolve()
    files = find_files(args.paths or [str(repo_root)])

    if not files:
        print("No .meiva.okf.md files found.")
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
