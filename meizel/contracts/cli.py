"""Command line: check, explain, sync --prune, bootstrap and roles."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from meizel import roles as role_api
from meizel.contracts import enumerations, generated
from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.bootstrap import bootstrap
from meizel.contracts.explain import explain
from meizel.contracts.sync import prune
from meizel.graph.model import Diagnostic, MeizelError
from meizel.graph.universe import toplevel

__all__ = ["main", "relative_target"]


def relative_target(root: Path, base: Path, raw: str) -> str:
    """A repository-relative POSIX path for a path given relative to `base`."""
    resolved = (base / raw).resolve()
    try:
        rel = resolved.relative_to(root.resolve())
    except ValueError as error:
        raise MeizelError(f"{raw}: outside the repository {root}") from error
    return "" if str(rel) == "." else rel.as_posix()


def _within(path: str, targets: list[str]) -> bool:
    return any(not t or path == t or path.startswith(f"{t}/") for t in targets)


def select(analysis: Analysis, targets: list[str]) -> list[Diagnostic]:
    unique = {(d.path, d.line, d.code, d.message): d for d in analysis.diagnostics}
    chosen = [d for d in unique.values() if not targets or _within(d.path, targets)]
    return sorted(chosen, key=lambda d: (d.path, d.line, d.code, d.message))


def run_check(
    root: Path, base: Path, raw_paths: list[str], limit: int | None, jobs: int | None
) -> int:
    targets = [relative_target(root, base, p) for p in raw_paths]
    analysis = analyze(root, jobs=jobs)
    known = analysis.universe
    for raw, target in zip(raw_paths, targets, strict=True):
        if target and target not in known.files and target not in known.dirs:
            raise MeizelError(f"{raw}: not a tracked file or directory")
    diagnostics = select(analysis, targets)
    shown = diagnostics if limit is None else diagnostics[:limit]
    for diagnostic in shown:
        print(diagnostic.render())
    counts = Counter(d.code for d in diagnostics)
    if diagnostics:
        detail = ", ".join(f"{code}: {n}" for code, n in sorted(counts.items()))
        omitted = (
            f" ({len(diagnostics) - len(shown)} not shown)" if len(shown) < len(diagnostics) else ""
        )
        print(f"meizel: {len(diagnostics)} violations{omitted} [{detail}]", file=sys.stderr)
        return 1
    print(f"meizel: ok ({len(analysis.facts)} fact edges, {len(analysis.manifests)} manifests)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m meizel", description=__doc__)
    parser.add_argument(
        "--repo", type=Path, help="repository root (default: the cwd's git toplevel)"
    )
    parser.add_argument("--jobs", type=int, help="worker processes for parsing (default: up to 8)")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="gate: fail on any violation")
    check.add_argument("paths", nargs="*", help="report only diagnostics under these paths")
    check.add_argument("--limit", type=int, help="print at most this many diagnostics")
    show = commands.add_parser("explain", help="show a node's view of the graph")
    show.add_argument("path")
    sync = commands.add_parser("sync", help="maintain manifests")
    sync.add_argument(
        "--prune", action="store_true", required=True, help="remove stale declared edges"
    )
    seed = commands.add_parser("bootstrap", help="generate manifests from current facts")
    seed.add_argument("--out", type=Path, required=True, help="directory to mirror manifests into")
    declared = commands.add_parser("roles", help="the declared top-level packages and their copies")
    modes = declared.add_subparsers(dest="mode", required=True)
    listing = modes.add_parser("list", help="print the top-level directories of the given roles")
    listing.add_argument("role_names", nargs="+", metavar="role")
    modes.add_parser("check", help="fail on a stale generated region or a copy of a role's list")
    modes.add_parser("write", help="regenerate every generated region")
    return parser


def run_roles(root: Path, args: argparse.Namespace) -> int:
    if args.mode == "list":
        for name in role_api.packages(*args.role_names, root=root):
            print(name)
        return 0
    if args.mode == "write":
        for path in generated.write(root):
            print(f"meizel: regenerated {path}")
        return 0
    problems = [*generated.stale(root), *enumerations.violations(root)]
    for problem in problems:
        print(problem)
    if problems:
        print(f"meizel: {len(problems)} role problems", file=sys.stderr)
        return 1
    print("meizel: roles ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        base = args.repo if args.repo is not None else Path.cwd()
        root = args.repo if args.repo is not None else toplevel(base)
        if args.command == "check":
            return run_check(root, base, args.paths, args.limit, args.jobs)
        if args.command == "explain":
            print(explain(analyze(root, jobs=args.jobs), relative_target(root, base, args.path)))
            return 0
        if args.command == "roles":
            return run_roles(root, args)
        if args.command == "sync":
            for line in prune(root, args.jobs):
                print(line)
            return 0
        print(bootstrap(root, args.out, args.jobs))
        return 0
    except MeizelError as error:
        print(f"meizel: {error}", file=sys.stderr)
        return 2
