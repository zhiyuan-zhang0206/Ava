"""Audit module moves without compatibility shims across repository content.

Run: `.venv/bin/python scripts/audit_module_moves.py OLD=NEW [OLD=NEW ...]`

## Why

Moving modules into packages reduces directory entries only if the old files
disappear. Unlike a module split with re-exports, a move must leave no callers
at the old path, and every name referenced at the new path must still exist.

## What it checks

For each pair, report old-path references with file:line locations, then import
the new module and check names from `from NEW import X`, `NEW.X`, and quoted
targets. Comments, documentation, and non-Python text are included. Wildcard
imports cannot enumerate their references and fail the completeness check.

The invariant is that committed repository content carries no reference to the
old path. Scan working-tree text for `git ls-files` entries, excluding the frozen
decisions/, postmortems/, and docs/history/ axes and this script itself. Untracked
environments, caches, and operator scaffolding are outside that universe. Stage
new files before auditing. Binary files are skipped. Any failed check exits 1.
"""

from __future__ import annotations

import argparse
import importlib
import re
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_FROZEN = ("decisions/", "postmortems/", "docs/history/")


def _tracked_text() -> list[tuple[str, str]]:
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=_REPO).decode().split("\0")
    texts: list[tuple[str, str]] = []
    for name in paths:
        if not name or name.startswith(_FROZEN):
            continue
        path = _REPO / name
        if path.resolve() == Path(__file__).resolve():
            continue
        # Git tracks a symlink's target path, not the target file's content.
        data = path.readlink().as_posix().encode() if path.is_symlink() else path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        texts.append((name, text))
    return texts


def _from_imports(module: str, text: str) -> list[tuple[int, set[str]]]:
    """Import names and offsets, including multiline imports in prose snippets."""
    if module not in text or "from" not in text:
        return []
    pattern = re.compile(
        rf"(?<![\w.])from\s+{re.escape(module)}\s+import\s+"
        r"(\((?:#[^\n]*|[^)#])*\)|(?:\\\n|[^\n;])+)"
    )
    imports: list[tuple[int, set[str]]] = []
    for match in pattern.finditer(text):
        body = re.sub(r"#[^\n]*", "", match.group(1)).strip("()")
        body = body.replace("\\\n", "")
        names = set(re.findall(r"(?:^|,)\s*([A-Za-z_]\w*|\*)", body))
        imports.append((match.start(), names))
    return imports


def _old_references(module: str, text: str) -> list[int]:
    pattern = re.compile(rf"(?<![\w.]){re.escape(module)}(?!\w)")
    offsets = [match.start() for match in pattern.finditer(text)] if module in text else []
    parent, _, leaf = module.rpartition(".")
    if parent:
        offsets.extend(offset for offset, names in _from_imports(parent, text) if leaf in names)
    slash = module.replace(".", "/")
    if slash in text:
        slash_pattern = re.compile(rf"(?<![\w.]){re.escape(slash)}\.py(?!\w)")
        offsets.extend(match.start() for match in slash_pattern.finditer(text))
    return sorted(text.count("\n", 0, offset) + 1 for offset in offsets)


def _referenced_names(module: str, text: str) -> set[str]:
    if module not in text:
        return set()
    refs = set(re.findall(rf"(?<![\w.]){re.escape(module)}\.([A-Za-z_]\w*)", text))
    for _, names in _from_imports(module, text):
        refs.update(names)
    return refs


def _missing(module: str, refs: set[str]) -> list[str]:
    loaded = importlib.import_module(module)
    return sorted(name for name in refs if not hasattr(loaded, name))


def _module_pair(value: str) -> tuple[str, str]:
    parts = value.split("=")
    if len(parts) != 2 or any(
        not all(part.isidentifier() for part in module.split(".")) for module in parts
    ):
        raise argparse.ArgumentTypeError("expected OLD=NEW with dotted Python module names")
    return parts[0], parts[1]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("## What it checks")[0])
    parser.add_argument("pairs", nargs="+", metavar="OLD=NEW", type=_module_pair)
    args = parser.parse_args(argv)
    texts = _tracked_text()
    failed = False
    for old, new in args.pairs:
        old_count = 0
        refs: set[str] = set()
        for path, text in texts:
            lines = _old_references(old, text)
            old_count += len(lines)
            for line in lines:
                print(f"{path}:{line}: old reference to {old}")
            refs.update(_referenced_names(new, text))
        import_error = ""
        missing: list[str] = []
        try:
            missing = _missing(new, refs)
        except Exception as exc:
            # An import failure is an audit failure; still report the remaining pairs.
            import_error = f"; import failed: {type(exc).__name__}: {exc}"
        print(
            f"{old} -> {new}: {old_count} old references; "
            f"{len(refs)} referenced names, missing={missing}{import_error}"
        )
        failed = failed or bool(old_count or missing or import_error)
    print("AUDIT:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
