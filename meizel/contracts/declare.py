"""`sync --declare`: write the legal new dependencies of some files into the manifests.

Declaring a dependency is the mechanical half of a change: the code already uses the other
file, and the rule asks that the directory where the two sides meet say so. This does only that.
It never declares what the code must fix instead (an edge that would close a cycle, a private
name, an underscore module) and never touches a restricted form or the recorded debt.
"""

from __future__ import annotations

import subprocess
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.manifest import manifest_path
from meizel.contracts.manifest_text import add_exports, add_uses
from meizel.contracts.rule import Projection, is_private
from meizel.graph.graphs import reachable
from meizel.graph.model import Fact, MeizelError

__all__ = ["changed_files", "declare"]


@dataclass(slots=True)
class Plan:
    uses: dict[str, dict[str, set[str]]] = field(default_factory=dict[str, dict[str, set[str]]])
    exports: dict[str, set[str]] = field(default_factory=dict[str, set[str]])
    lines: list[str] = field(default_factory=list[str])
    left: list[str] = field(default_factory=list[str])


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise MeizelError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def changed_files(root: Path) -> list[str]:
    """Files that differ from the branch point with origin/main (HEAD when there is none)."""
    merge_base = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(root), "merge-base", "HEAD", "origin/main"],
        capture_output=True,
        text=True,
        check=False,
    )
    base = merge_base.stdout.strip() if merge_base.returncode == 0 else "HEAD"
    changed = _git(root, "diff", "--name-only", "--no-renames", "-z", base).split("\0")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted({p for p in [*changed, *untracked] if p})


def _within(path: str, targets: list[str]) -> bool:
    return any(not t or path == t or path.startswith(f"{t}/") for t in targets)


def _unfixable(proj: Projection) -> str | None:
    """Why a fact edge needs a code change rather than a declaration, or None."""
    names = sorted(n for n in proj.fact.names if is_private(n))
    if names:
        return f"uses private name {', '.join(names)}: use a public name"
    last = len(proj.steps) - 1
    for position, (directory, child) in enumerate(proj.steps):
        if child.startswith("_") and not (child == "__init__.py" and position == last):
            return f"goes through the underscore module {child!r} of {directory or '.'}: use its package door"
    return None


def _describe(fact: Fact) -> str:
    return f"{fact.src}:{fact.line} -> {fact.dst}"


def _missing_exports(analysis: Analysis, plan: Plan, proj: Projection) -> list[tuple[str, str]]:
    """The `(directory, child)` steps of the path that no `exports` list yet names."""
    last = len(proj.steps) - 1
    missing: list[tuple[str, str]] = []
    for position, (directory, child) in enumerate(proj.steps):
        if child == "__init__.py" and position == last:
            continue
        manifest = analysis.manifests.get(directory)
        listed: set[str] = set(manifest.exports) if manifest is not None else set()
        if child not in listed and child not in plan.exports.get(directory, set()):
            missing.append((directory, child))
    return missing


def _cycle_problem(graph: dict[str, set[str]], proj: Projection) -> str | None:
    if proj.cf not in reachable({k: list(v) for k, v in graph.items()}, proj.ct):
        return None
    return (
        f"closes a cycle in {manifest_path(proj.lca)} ({proj.ct} already reaches {proj.cf}): "
        "use a public door or inject the collaborator"
    )


def _accept(
    plan: Plan,
    graphs: dict[str, dict[str, set[str]]],
    proj: Projection,
    *,
    needs_edge: bool,
    needs_exports: list[tuple[str, str]],
) -> None:
    if needs_edge:
        graphs[proj.lca].setdefault(proj.cf, set()).add(proj.ct)
        plan.uses.setdefault(proj.lca, {}).setdefault(proj.cf, set()).add(proj.ct)
        plan.lines.append(f"{manifest_path(proj.lca)}: declared {proj.cf} -> {proj.ct}")
    for directory, child in needs_exports:
        plan.exports.setdefault(directory, set()).add(child)
        plan.lines.append(f"{manifest_path(directory)}: exported {child}")


def _plan(analysis: Analysis, targets: list[str]) -> Plan:
    plan = Plan()
    graphs: dict[str, dict[str, set[str]]] = defaultdict(dict)
    for directory, manifest in analysis.manifests.items():
        graphs[directory] = {cf: set(cts) for cf, cts in manifest.uses.items()}
    ordered = sorted(
        (p for p in analysis.projections if not p.exempt and _within(p.fact.src, targets)),
        key=lambda p: (p.lca, p.cf, p.ct, p.fact.src, p.fact.line),
    )
    for proj in ordered:
        needs_edge = proj.ct not in graphs[proj.lca].get(proj.cf, set())
        needs_exports = _missing_exports(analysis, plan, proj)
        if not needs_edge and not needs_exports:
            continue
        problem = _unfixable(proj) or (
            _cycle_problem(graphs[proj.lca], proj) if needs_edge else None
        )
        if problem is None:
            _accept(plan, graphs, proj, needs_edge=needs_edge, needs_exports=needs_exports)
        else:
            plan.left.append(f"{_describe(proj.fact)}: left as an error, it {problem}")
    return plan


def _write(root: Path, directory: str, uses: dict[str, set[str]], exports: set[str]) -> None:
    path = root / manifest_path(directory)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    after = before
    if uses:
        after = add_uses(after, uses)
    if exports:
        after = add_exports(after, exports)
    parsed = tomllib.loads(after)
    written = parsed.get("uses", {})
    if any(
        not set(cts) <= set(written.get(cf, ())) for cf, cts in uses.items()
    ) or not exports <= set(parsed.get("exports", ())):
        raise MeizelError(
            f"{manifest_path(directory)}: the declarations cannot be written; edit by hand"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after, encoding="utf-8")


def declare(
    root: Path, targets: list[str] | None, jobs: int | None = None
) -> tuple[list[str], list[str]]:
    """Declare the legal new dependencies under `targets` (default: the changed files).

    Returns a line per declaration and a line per dependency left as an error.
    """
    scope = targets if targets is not None else changed_files(root)
    plan = _plan(analyze(root, jobs=jobs), scope)
    for directory in sorted(set(plan.uses) | set(plan.exports)):
        _write(root, directory, plan.uses.get(directory, {}), plan.exports.get(directory, set()))
    return plan.lines, plan.left
