"""`sync --prune`: drop declared edges that no fact edge needs."""

from __future__ import annotations

import re
from pathlib import Path

from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.manifest import manifest_path
from meizel.contracts.manifest_text import ENTRY, HEADER, entry_end, items, unquote
from meizel.graph.model import MeizelError

__all__ = ["prune", "remove_entries"]


def _rewrite_entry(lines: list[str], start: int, end: int, drop: set[str]) -> list[str]:
    match = ENTRY.match(lines[start])
    if match is None:
        raise MeizelError(f"line {start + 1}: not a manifest entry: {lines[start]!r}")
    block = "\n".join(lines[start : end + 1])
    kept = [item for item in items(block) if item not in drop]
    if not kept or "*" in drop:
        return []
    values = ", ".join(f'"{item}"' for item in kept)
    return [f"{match.group(1)}{match.group(2)} = [{values}]"]


_EXTERNAL_ENTRY = re.compile(r"""^\s*("[^"]*"|'[^']*'|[\w.-]+)\s*=\s*["']""")


def remove_entries(text: str, removals: dict[str, dict[str, set[str]]]) -> str:
    """Remove `targets` from entries of `[uses]`/`[loads]`; comments elsewhere survive."""
    lines = text.splitlines()
    out: list[str] = []
    section = ""
    index = 0
    while index < len(lines):
        header = HEADER.match(lines[index])
        if header:
            section = header.group(1)
        reason = _EXTERNAL_ENTRY.match(lines[index]) if section == "external" else None
        if reason and unquote(reason.group(1)) in removals.get("external", {}):
            index += 1
            continue
        entry = ENTRY.match(lines[index])
        if entry and section in removals:
            end = entry_end(lines, index)
            drop = removals[section].get(unquote(entry.group(2)))
            out.extend(_rewrite_entry(lines, index, end, drop) if drop else lines[index : end + 1])
            index = end + 1
            continue
        out.append(lines[index])
        index += 1
    return _drop_empty_sections(out)


def _drop_empty_sections(lines: list[str]) -> str:
    kept: list[str] = []
    for position, line in enumerate(lines):
        if HEADER.match(line):
            rest = lines[position + 1 :]
            following = next((x for x in rest if x.strip() and not x.strip().startswith("#")), "")
            if not following or HEADER.match(following):
                continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return (
        "\n".join(kept) + "\n"
        if any(x.strip() and not x.strip().startswith("#") for x in kept)
        else ""
    )


Removals = dict[str, dict[str, dict[str, set[str]]]]


def _collect(analysis: Analysis) -> tuple[Removals, list[str]]:
    """What to remove from each manifest, and a line for each removal."""
    removals: Removals = {}
    changes: list[str] = []
    for directory, cf, ct in analysis.stale_edges:
        removals.setdefault(directory, {}).setdefault("uses", {}).setdefault(cf, set()).add(ct)
        changes.append(f"{manifest_path(directory)}: removed {cf} -> {ct}")
    for directory, name in analysis.stale_loads:
        removals.setdefault(directory, {}).setdefault("loads", {})[name] = {"*"}
        changes.append(f"{manifest_path(directory)}: removed [loads] {name}")
    for directory, manifest in analysis.manifests.items():
        for section, key, target in manifest.dangling:
            table = removals.setdefault(directory, {}).setdefault(
                "" if section == "exports" else section, {}
            )
            table.setdefault(key or "exports", set()).add(target if target is not None else "*")
            arrow = " -> " if key and target else ""
            changes.append(
                f"{manifest_path(directory)}: removed [{section}] {key or ''}{arrow}{target or ''}"
                " (not a child any more)"
            )
    return removals, changes


def _apply(root: Path, removals: Removals) -> None:
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


def prune(root: Path, jobs: int | None = None) -> list[str]:
    """Remove stale declarations until none remain; returns a line per removal."""
    changes: list[str] = []
    while True:
        removals, found = _collect(analyze(root, jobs=jobs))
        if not removals:
            return changes
        changes.extend(found)
        _apply(root, removals)
