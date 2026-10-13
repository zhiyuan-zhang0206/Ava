"""`explain <path>`: a node's view of the graph."""

from __future__ import annotations

from collections import defaultdict

from meizel.contracts.analysis import Analysis
from meizel.contracts.manifest import first
from meizel.graph.model import Fact, MeizelError
from meizel.graph.universe import MANIFEST, parent_of

__all__ = ["explain"]


def _inside(path: str, node: str) -> bool:
    return not node or path == node or path.startswith(f"{node}/")


def _group(facts: list[Fact], *, outgoing: bool) -> list[str]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for fact in facts:
        if outgoing:
            grouped[fact.dst].append(f"{fact.src}:{fact.line} {fact.kind}")
        else:
            grouped[fact.src].append(f"{fact.src}:{fact.line} {fact.kind} -> {fact.dst}")
    return [
        f"  {key}  <- {'; '.join(sorted(set(items))[:3])}" for key, items in sorted(grouped.items())
    ]


def _local_graph(analysis: Analysis, node: str) -> list[str]:
    parent = parent_of(node)
    name = node.rsplit("/", 1)[-1]
    manifest = analysis.manifests.get(parent)
    where = f"{parent or '.'}/{MANIFEST}"
    if manifest is None:
        return [f"parent {parent or '.'} has no {MANIFEST}: no local edges, exports only its door"]
    users = sorted(
        src for src, targets in manifest.uses.items() if any(first(t) == name for t in targets)
    )
    uses = sorted(
        t for src, targets in manifest.uses.items() if first(src) == name for t in targets
    )
    exported = "yes" if name in manifest.exports else "no"
    return [
        f"parent {parent or '.'} ({where})",
        f"  declared to use: {', '.join(uses) or '-'}",
        f"  declared to be used by: {', '.join(users) or '-'}",
        f"  exported by parent: {exported}",
    ]


def _directory_lines(analysis: Analysis, node: str) -> list[str]:
    manifest = analysis.manifests.get(node)
    exports = ", ".join(manifest.exports) if manifest and manifest.exports else "-"
    lines = [f"exports: {exports} (plus its __init__.py door)"]
    if manifest:
        lines.append("local graph:")
        lines.extend(f"  {src} -> {', '.join(t)}" for src, t in sorted(manifest.uses.items()))
    return lines


def explain(analysis: Analysis, node: str) -> str:
    universe = analysis.universe
    if node not in universe.files and node not in universe.dirs:
        raise MeizelError(f"{node or '.'}: not a tracked file or directory")
    is_dir = node in universe.dirs
    crossing = [f for f in analysis.facts if _inside(f.src, node) != _inside(f.dst, node)]
    uses = [f for f in crossing if _inside(f.src, node)]
    used_by = [f for f in crossing if _inside(f.dst, node)]
    lines = [f"{node or '.'} ({'directory' if is_dir else 'file'})"]
    if node:
        lines.extend(_local_graph(analysis, node))
    if is_dir:
        lines.extend(_directory_lines(analysis, node))
    lines.append(f"uses ({len(uses)} fact edges):")
    lines.extend(_group(uses, outgoing=True))
    lines.append(f"used by ({len(used_by)} fact edges):")
    lines.extend(_group(used_by, outgoing=False))
    return "\n".join(lines)
