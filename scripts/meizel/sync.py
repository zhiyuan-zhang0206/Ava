"""`sync --prune`: drop declared edges that no fact edge needs."""

from __future__ import annotations

import re
from pathlib import Path

from scripts.meizel.analysis import analyze
from scripts.meizel.manifest import manifest_path
from scripts.meizel.model import MeizelError

__all__ = ["prune", "remove_entries"]

_HEADER = re.compile(r"^\s*\[(\w+)\]\s*(?:#.*)?$")
_ENTRY = re.compile(r"""^(\s*)("[^"]*"|'[^']*'|[\w.-]+)\s*=\s*\[""")
_QUOTED = re.compile(r""""([^"]*)"|'([^']*)'""")


def _unquote(key: str) -> str:
    return key[1:-1] if key[:1] in {'"', "'"} else key


def _entry_end(lines: list[str], start: int) -> int:
    """Index of the line holding the `]` that closes the list opened on `start`."""
    depth = 0
    for index in range(start, len(lines)):
        text = re.sub(r"""("[^"]*"|'[^']*')""", "", lines[index].split("#", 1)[0])
        depth += text.count("[") - text.count("]")
        if depth <= 0:
            return index
    return len(lines) - 1


def _items(block: str) -> list[str]:
    body = block[block.index("[") + 1 : block.rindex("]")]
    uncommented = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    return [a or b for a, b in _QUOTED.findall(uncommented)]


def _rewrite_entry(lines: list[str], start: int, end: int, drop: set[str]) -> list[str]:
    match = _ENTRY.match(lines[start])
    if match is None:
        raise MeizelError(f"line {start + 1}: not a manifest entry: {lines[start]!r}")
    block = "\n".join(lines[start : end + 1])
    kept = [item for item in _items(block) if item not in drop]
    if not kept or "*" in drop:
        return []
    values = ", ".join(f'"{item}"' for item in kept)
    return [f"{match.group(1)}{match.group(2)} = [{values}]"]


def remove_entries(text: str, removals: dict[str, dict[str, set[str]]]) -> str:
    """Remove `targets` from entries of `[uses]`/`[loads]`; comments elsewhere survive."""
    lines = text.splitlines()
    out: list[str] = []
    section = ""
    index = 0
    while index < len(lines):
        header = _HEADER.match(lines[index])
        if header:
            section = header.group(1)
        entry = _ENTRY.match(lines[index])
        if entry and section in removals:
            end = _entry_end(lines, index)
            drop = removals[section].get(_unquote(entry.group(2)))
            out.extend(_rewrite_entry(lines, index, end, drop) if drop else lines[index : end + 1])
            index = end + 1
            continue
        out.append(lines[index])
        index += 1
    return _drop_empty_sections(out)


def _drop_empty_sections(lines: list[str]) -> str:
    kept: list[str] = []
    for position, line in enumerate(lines):
        if _HEADER.match(line):
            rest = lines[position + 1 :]
            following = next((x for x in rest if x.strip() and not x.strip().startswith("#")), "")
            if not following or _HEADER.match(following):
                continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return (
        "\n".join(kept) + "\n"
        if any(x.strip() and not x.strip().startswith("#") for x in kept)
        else ""
    )


def prune(root: Path, jobs: int | None = None) -> list[str]:
    """Remove stale declarations until none remain; returns a line per removal."""
    changes: list[str] = []
    while True:
        analysis = analyze(root, jobs=jobs)
        removals: dict[str, dict[str, dict[str, set[str]]]] = {}
        for directory, cf, ct in analysis.stale_edges:
            removals.setdefault(directory, {}).setdefault("uses", {}).setdefault(cf, set()).add(ct)
            changes.append(f"{manifest_path(directory)}: removed {cf} -> {ct}")
        for directory, name in analysis.stale_loads:
            removals.setdefault(directory, {}).setdefault("loads", {})[name] = {"*"}
            changes.append(f"{manifest_path(directory)}: removed [loads] {name}")
        if not removals:
            return changes
        for directory, sections in removals.items():
            path = root / manifest_path(directory)
            before = path.read_text(encoding="utf-8")
            updated = remove_entries(before, sections)
            if updated == before:
                raise MeizelError(
                    f"{path.relative_to(root)}: cannot remove {sections}; edit it by hand"
                )
            if updated:
                path.write_text(updated, encoding="utf-8")
            else:
                path.unlink()
