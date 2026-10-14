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
from meizel.contracts.manifest import first, manifest_path, within
from meizel.contracts.manifest_text import add_exports, add_uses
from meizel.contracts.rule import Projection, is_private, relative
from meizel.graph.graphs import reachable
from meizel.graph.model import Fact, MeizelError
from meizel.graph.universe import is_private_module

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
        if is_private_module(child) and not (child == "__init__.py" and position == last):
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


Pairs = dict[str, list[tuple[str, str]]]


def _edge_action(pairs: list[tuple[str, str]], proj: Projection) -> tuple[str, str] | str | None:
    """What a fact needs declared: None (covered), `(source, target)` to write, or a reason not to.

    A child pair that has no declaration is declared plainly. When the edge exists only for
    narrowed endpoints, a fact from a source those declarations already authorise gets its target
    added to that source; a fact from any other source is left for a person to judge, so narrowing
    is never widened silently.
    """
    src, dst = relative(proj)
    if any(within(src, s) and within(dst, d) for s, d in pairs):
        return None
    narrowed = [(s, d) for s, d in pairs if (first(s), first(d)) == (proj.cf, proj.ct)]
    if not narrowed:
        return (proj.cf, proj.ct)
    authorised = [s for s, _ in narrowed if within(src, s)]
    if not authorised:
        listing = ", ".join(f"{s} -> {d}" for s, d in narrowed)
        return (
            f"is declared only for narrowed endpoints ({listing}) and its source is outside them: "
            "decide whether to widen the declaration"
        )
    source = max(authorised, key=len)
    depth = max(len(d.split("/")) for s, d in narrowed if s == source)
    return (source, "/".join(dst.split("/")[:depth]))


def _accept(
    plan: Plan,
    graphs: dict[str, dict[str, set[str]]],
    pairs: Pairs,
    proj: Projection,
    edge: tuple[str, str] | None,
    needs_exports: list[tuple[str, str]],
) -> None:
    if edge is not None:
        source, target = edge
        graphs[proj.lca].setdefault(proj.cf, set()).add(proj.ct)
        pairs[proj.lca].append(edge)
        plan.uses.setdefault(proj.lca, {}).setdefault(source, set()).add(target)
        plan.lines.append(f"{manifest_path(proj.lca)}: declared {source} -> {target}")
    for directory, child in needs_exports:
        plan.exports.setdefault(directory, set()).add(child)
        plan.lines.append(f"{manifest_path(directory)}: exported {child}")


def _judge(
    analysis: Analysis,
    plan: Plan,
    graphs: dict[str, dict[str, set[str]]],
    pairs: Pairs,
    proj: Projection,
) -> tuple[tuple[str, str] | None, list[tuple[str, str]], str | None] | None:
    """`(edge to declare, exports to add, why not)` for one fact edge, or None when it needs nothing."""
    action = _edge_action(pairs[proj.lca], proj)
    edge = action if isinstance(action, tuple) else None
    needs_exports = _missing_exports(analysis, plan, proj)
    if action is None and not needs_exports:
        return None
    problem = action if isinstance(action, str) else _unfixable(proj)
    if problem is None and edge is not None and edge == (proj.cf, proj.ct):
        problem = _cycle_problem(graphs[proj.lca], proj)
    return edge, needs_exports, problem


def _plan(analysis: Analysis, targets: list[str]) -> Plan:
    plan = Plan()
    graphs: dict[str, dict[str, set[str]]] = defaultdict(dict)
    pairs: Pairs = defaultdict(list)
    for directory, manifest in analysis.manifests.items():
        graphs[directory] = {cf: set(cts) for cf, cts in manifest.graph().items()}
        pairs[directory] = manifest.pairs()
    ordered = sorted(
        (p for p in analysis.projections if not p.exempt and _within(p.fact.src, targets)),
        key=lambda p: (p.lca, p.cf, p.ct, p.fact.src, p.fact.line),
    )
    for proj in ordered:
        judged = _judge(analysis, plan, graphs, pairs, proj)
        if judged is None:
            continue
        edge, needs_exports, problem = judged
        if problem is None:
            _accept(plan, graphs, pairs, proj, edge, needs_exports)
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
