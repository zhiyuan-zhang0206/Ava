"""Find every reference to a symbol, module or file before you change or move it.

Run: `.venv/bin/python scripts/audit/where_used.py TARGET [TARGET ...] [--all] [--json]`

## Why

The slow part of most changes is not finding the first file to edit, it is
enumerating the rest of the edit set: importers, tests, string targets, docs and
registries. This command answers that in one step, in seconds, instead of a chain
of greps. `module_moves.py` is the complementary check for AFTER a move (no
reference to the old path may remain); this one is the question BEFORE.

## Targets

- a symbol: `pkg.mod:name`, `pkg.mod.name` or `pkg/mod.py::name` (must be bound at
  the top level of that module; the name is checked, so a typo fails fast);
- a module or package: `pkg.mod`, `pkg/mod.py` or `pkg` (a package covers every
  module below it);
- any other tracked path: a file (`conventions/runbook.md`) or a directory.

Several targets may be given; the repository is read once.

## Groups

Every hit lands in exactly one group, shown as `path:line  [kind]  context`, one
entry per file, at most `--limit` files per group (`--all` lifts it; the header
always carries the full count):

- `IMPORTS`: import statements of production code (absolute, relative, deferred in
  a function, `from x import y`, wildcard) and, for a symbol, attribute use through
  an imported module. A symbol that a package `__init__` imports at its top level is
  also importable from that package: the package is named ("importable through") and
  its importers are included.
- `STRINGS`: dotted or path strings that name the target: patch targets
  (`monkeypatch.setattr("a.b.c", ...)`, `patch("a.b.c")` and, for a symbol, the object
  form `monkeypatch.setattr(mod, "name", ...)`), dynamic imports, `python -m a.b` entry
  points, registries and configuration. A patch in a test is listed under `TESTS`.
- `TESTS`: test files that reference it, plus tests named for the module that do not
  (`test_<module>.py`). A test beside the code (`<pkg>/tests/`) is told apart from
  the top-level `tests/`.
- `DOCS`: markdown (links are resolved relative to the file), docstrings, comments.
  A symbol also matches inside inline code in markdown (`` `name` ``); an ordinary
  lowercase word (`run`, `scan`) only in markdown that names its module or sits beside
  it. A file whose name is unique in the repository matches by that bare name.
- `STRUCTURE`: the structure baseline shards, `pyproject.toml`, hooks, workflows and
  the lint registries under `scripts/{structure,lint,content_lint}`.
- `FROZEN HISTORY`: `decisions/`, `postmortems/`, `docs/history/` and `CHANGELOG.md`.
  Historical references, never rewritten.

References from inside the target (its own file, or a package's own modules) are
counted and hidden: they move with it.

## Limits

Static references only. Names built at runtime (f-strings, `getattr`, joined path
pieces) and a symbol imported under another local name are invisible; a symbol is
matched by its bare name only inside markdown inline code, never in code comments;
members of a class are not tracked, name the class. Treat the output as the starting
edit set, then verify with the changed code's own tests. Untracked files that are not
ignored are scanned; `.test_durations` is not (it is regenerated). Exit status is 0
whenever the target resolves; 2 when it does not.
The reference finding itself lives in `where_used_scan.py`.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.audit.module_moves import is_frozen  # noqa: E402
from scripts.audit.where_used_scan import (  # noqa: E402
    Door,
    Hit,
    Repo,
    Target,
    build_matcher,
    file_hits,
    find_doors,
    is_doc_file,
    load_repo,
    resolve,
)

GROUPS = ("imports", "strings", "tests", "docs", "structure", "history")
_TITLES = {
    "imports": "IMPORTS (production code)",
    "strings": "STRINGS (patch targets, entry points, registries, config)",
    "tests": "TESTS",
    "docs": "DOCS (markdown, docstrings, comments)",
    "structure": "STRUCTURE (baseline, pyproject, hooks, workflows, lint registries)",
    "history": "FROZEN HISTORY (not rewritten)",
}
_DEFAULT_FILES_PER_GROUP = 10  # output layout only: the header always shows the full count
_OMITTED_DIRECTORIES = 6  # how many directories the "more files" line breaks the rest into

_HISTORY_FILES = frozenset({"CHANGELOG.md"})
_STRUCTURE_FILES = frozenset({"pyproject.toml", ".pre-commit-config.yaml"})
_STRUCTURE_PREFIXES = (
    "scripts/structure/",
    "scripts/lint/",
    "scripts/content_lint/",
    ".github/",
    ".trunk/",
)
_IMPORT_KINDS = frozenset(
    {"import", "import in function", "wildcard import", "re-export", "attribute"}
)
_PROSE_KINDS = frozenset({"docstring", "comment"})
_PLAIN_KINDS = frozenset({"import", "mention", "reference"})  # not worth a bracketed label


@dataclass(frozen=True)
class Report:
    target: Target
    doors: tuple[Door, ...]
    groups: dict[str, list[Hit]]
    hidden: int  # references from inside the target itself


# ------------------------------------------------------------ grouping, report


def _is_test(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        "tests" in parts[:-1]
        or name == "conftest.py"
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def group_of(path: str, kind: str) -> str:
    """The one group a hit belongs to: the file decides first, then the kind of hit."""
    if is_frozen(path) or path in _HISTORY_FILES:
        return "history"
    if is_doc_file(path):
        return "docs"
    if _is_test(path):
        return "tests"
    if kind in _PROSE_KINDS:
        return "docs"
    if kind in _IMPORT_KINDS:
        return "imports"
    structure = path in _STRUCTURE_FILES or path.startswith(_STRUCTURE_PREFIXES)
    return "structure" if structure else "strings"


def _is_internal(target: Target, path: str, group: str) -> bool:
    """A reference from the target itself: it moves with the target."""
    if path == target.path:
        return True
    inside = target.is_dir and path.startswith(target.path + "/")
    return inside and path.endswith(".py") and group != "tests"


def _named_for(path: str, stem: str) -> bool:
    """`test_<module>.py` or `test_<module>_<what>.py`, but not `test_<module>x.py`."""
    name = posixpath.basename(path)
    return name.startswith(stem) and name[len(stem) :].startswith(("_", "."))


def _name_matches(repo: Repo, target: Target, present: set[str]) -> list[Hit]:
    """Tests named for the module that no hit led to: where its own tests would live."""
    if target.kind not in ("module", "symbol") or target.path.endswith("__init__.py"):
        return []  # a package's own name is too generic to say which tests are its own
    stem = f"test_{target.module.rsplit('.', 1)[-1]}"
    return [
        Hit(path, 0, "name match", "")
        for path in sorted(repo.texts)
        if path.endswith(".py")
        and path not in present
        and _is_test(path)
        and _named_for(path, stem)
    ]


def scan(repo: Repo, target: Target) -> Report:
    """Every reference to `target`, grouped."""
    doors = find_doors(repo, target.module, target.name) if target.name else []
    matcher = build_matcher(repo, target, doors)
    found: dict[tuple[str, int], Hit] = {}
    for path, text in repo.texts.items():
        for hit in file_hits(path, text, matcher, repo):
            found.setdefault((hit.path, hit.line), hit)
    groups: dict[str, list[Hit]] = {group: [] for group in GROUPS}
    hidden = 0
    for hit in sorted(found.values(), key=lambda h: (h.path, h.line)):
        group = group_of(hit.path, hit.kind)
        if _is_internal(target, hit.path, group):
            hidden += 1
        else:
            groups[group].append(hit)
    groups["tests"].extend(_name_matches(repo, target, {h.path for h in groups["tests"]}))
    return Report(target, tuple(doors), groups, hidden)


# ------------------------------------------------------------------- output


def _by_file(hits: Sequence[Hit]) -> dict[str, list[Hit]]:
    files: dict[str, list[Hit]] = {}
    for hit in hits:
        files.setdefault(hit.path, []).append(hit)
    return files


def _entry(path: str, hits: list[Hit]) -> str:
    first = hits[0]
    if first.kind == "name match":
        return f"  {path}  [named for the module, no reference]"
    label = "" if first.kind in _PLAIN_KINDS else f"  [{first.kind}]"
    more = f"  (+{len(hits) - 1} at {', '.join(str(h.line) for h in hits[1:])})" if hits[1:] else ""
    return f"  {path}:{first.line}{label}  {first.text}{more}"


def _omitted(paths: Sequence[str]) -> str:
    """`... N more files (--all lists every file): dir N, dir N`."""
    top = Counter(path.split("/")[0] for path in paths).most_common(_OMITTED_DIRECTORIES)
    where = ", ".join(f"{directory} {count}" for directory, count in top)
    return f"  ... {len(paths)} more files (--all lists every file): {where}"


def _group_lines(group: str, hits: Sequence[Hit], limit: int | None) -> list[str]:
    files = _by_file(hits)
    if not files:
        return [f"{_TITLES[group]}: none"]
    header = f"{_TITLES[group]}: {len(files)} files, {len(hits)} sites"
    if group == "tests":
        beside = sum(1 for path in files if not path.startswith("tests/"))
        header += f" ({beside} beside the code, {len(files) - beside} in top-level tests/)"
    entries = list(files.items())
    shown = entries if limit is None else entries[:limit]
    lines = [header, *(_entry(path, found) for path, found in shown)]
    if len(shown) < len(entries):
        lines.append(_omitted([path for path, _ in entries[len(shown) :]]))
    return lines


def _describe(target: Target) -> str:
    if target.kind == "symbol":
        return f"symbol, defined at {target.defined_at}"
    return f"{target.kind} {target.path}"


def render(report: Report, limit: int | None) -> str:
    """The report as text; `limit` caps the files shown per group (None shows all)."""
    target = report.target
    lines = [f"where-used {target.raw}  [{_describe(target)}]"]
    if report.doors:
        doors = ", ".join(f"{d.module}:{d.name} ({d.where})" for d in report.doors)
        lines.append(f"also importable through: {doors}")
    counts = " | ".join(f"{group} {len(_by_file(report.groups[group]))}" for group in GROUPS)
    lines.append(f"files per group: {counts}")
    for group in GROUPS:
        lines.extend(_group_lines(group, report.groups[group], limit))
    hidden = (
        f"{report.hidden} references from inside the target are hidden. " if report.hidden else ""
    )
    lines.append(f"{hidden}Static references only: runtime-built names are not seen.")
    return "\n".join(lines)


def as_json(report: Report) -> dict[str, object]:
    """The complete report (no per-group cap) for scripts."""
    target = report.target
    return {
        "target": {
            "raw": target.raw,
            "kind": target.kind,
            "path": target.path,
            "module": target.module or None,
            "name": target.name or None,
            "defined_at": target.defined_at or None,
            "importable_through": [asdict(door) for door in report.doors],
        },
        "groups": {
            group: {
                "files": len(_by_file(hits)),
                "sites": len(hits),
                "hits": [asdict(hit) for hit in hits],
            }
            for group, hits in report.groups.items()
        },
        "hidden_internal": report.hidden,
    }


def _relative(raw: str) -> str:
    """`/abs/path/in/this/checkout.py:name` -> `path/in/this/checkout.py:name`."""
    spec, sep, rest = raw.partition(":")
    path = Path(spec)
    if path.is_absolute() and path.resolve().is_relative_to(_REPO):
        spec = path.resolve().relative_to(_REPO).as_posix()
    return spec + sep + rest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("## Targets")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("targets", nargs="+", metavar="TARGET", help="symbol, module or path")
    parser.add_argument("--all", action="store_true", dest="every", help="list every file")
    parser.add_argument(
        "--limit", type=int, default=_DEFAULT_FILES_PER_GROUP, help="files per group"
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="complete, for scripts")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    repo = load_repo(_REPO)
    reports: list[Report] = []
    for raw in args.targets:
        try:
            reports.append(scan(repo, resolve(_relative(raw), repo)))
        except ValueError as error:
            parser.error(str(error))
    if args.as_json:
        print(json.dumps([as_json(report) for report in reports], indent=2))
    else:
        limit = None if args.every else args.limit
        print("\n\n".join(render(report, limit) for report in reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
