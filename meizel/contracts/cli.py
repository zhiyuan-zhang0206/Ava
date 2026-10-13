"""Command line: check, explain, sync --prune, bootstrap, debt and roles."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from meizel import roles as role_api
from meizel.contracts import enumerations, generated
from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.bootstrap import bootstrap
from meizel.contracts.debt_carry import carry as carry_debt
from meizel.contracts.debt_edit import prune as prune_debt
from meizel.contracts.debt_edit import record
from meizel.contracts.debt_report import changed_outside, changed_under, diff, summarize
from meizel.contracts.declare import declare
from meizel.contracts.explain import explain
from meizel.contracts.ledger import debts_in
from meizel.contracts.policy import changes as policy_changes
from meizel.contracts.policy import load_policy, policy_at
from meizel.contracts.sync import prune
from meizel.graph.model import Diagnostic, MeizelError
from meizel.graph.universe import load_universe, toplevel

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
    unique = {(d.path, d.line, d.code, d.message): d for d in analysis.verdict}
    chosen = [d for d in unique.values() if not targets or _within(d.path, targets)]
    return sorted(chosen, key=lambda d: (d.path, d.line, d.code, d.message))


def _print_verdict(diagnostics: list[Diagnostic], limit: int | None) -> None:
    shown = diagnostics if limit is None else diagnostics[:limit]
    for diagnostic in shown:
        print(diagnostic.render())
    counts = Counter(d.code for d in diagnostics)
    detail = ", ".join(f"{code}: {n}" for code, n in sorted(counts.items()))
    omitted = (
        f" ({len(diagnostics) - len(shown)} not shown)" if len(shown) < len(diagnostics) else ""
    )
    print(f"meizel: {len(diagnostics)} violations{omitted} [{detail}]", file=sys.stderr)


def run_check(
    root: Path,
    base: Path,
    raw_paths: list[str],
    limit: int | None,
    jobs: int | None,
    changed: list[str] | None = None,
) -> int:
    if changed is not None and raw_paths:
        raise MeizelError("--changed judges the files it names; it cannot be combined with paths")
    targets = [relative_target(root, base, p) for p in raw_paths]
    only = None if changed is None else frozenset(relative_target(root, base, p) for p in changed)
    analysis = analyze(root, jobs=jobs, changed=only)
    known = analysis.universe
    for raw, target in zip(raw_paths, targets, strict=True):
        if target and target not in known.files and target not in known.dirs:
            raise MeizelError(f"{raw}: not a tracked file or directory")
    diagnostics = select(analysis, targets)
    if diagnostics:
        _print_verdict(diagnostics, limit)
        return 1
    print(_ok_line(analysis))
    return 0


def _ok_line(analysis: Analysis) -> str:
    debts = debts_in(analysis.manifests)
    scope = "" if analysis.changed is None else f"{len(analysis.changed)} changed paths, "
    carried = (
        ""
        if not debts
        else f"; {debts} recorded debts remain in "
        f"{sum(1 for m in analysis.manifests.values() if m.debt)} strict-to-be directories"
    )
    return (
        f"meizel: ok ({scope}{len(analysis.facts)} fact edges, "
        f"{len(analysis.manifests)} manifests{carried})"
    )


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
    check.add_argument(
        "--changed",
        nargs="+",
        metavar="file",
        help="judge only these files' fact edges, plus the declared graphs; skips stale checks",
    )
    show = commands.add_parser("explain", help="show a node's view of the graph")
    show.add_argument("path")
    sync = commands.add_parser("sync", help="maintain manifests")
    sync.add_argument("--prune", action="store_true", help="remove stale declared edges")
    sync.add_argument(
        "--declare",
        action="store_true",
        help="declare the legal new dependencies of the paths (default: the changed files); also prunes",
    )
    sync.add_argument("paths", nargs="*", help="with --declare: only dependencies of these paths")
    seed = commands.add_parser("bootstrap", help="generate manifests from current facts")
    seed.add_argument("--out", type=Path, required=True, help="directory to mirror manifests into")
    add_debt_parser(commands)
    policy = commands.add_parser("policy", help="the allowance lists in meizel.policy.toml")
    policy_modes = policy.add_subparsers(dest="policy_mode", required=True)
    policy_diff = policy_modes.add_parser("diff", help="list the entries changed against a git ref")
    policy_diff.add_argument("--against", required=True, metavar="ref")
    declared = commands.add_parser("roles", help="the declared top-level packages and their copies")
    modes = declared.add_subparsers(dest="mode", required=True)
    listing = modes.add_parser("list", help="print the top-level directories of the given roles")
    listing.add_argument("role_names", nargs="+", metavar="role")
    modes.add_parser("check", help="fail on a stale generated region or a copy of a role's list")
    modes.add_parser("write", help="regenerate every generated region")
    return parser


def add_debt_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    debt = commands.add_parser(
        "debt",
        help="recorded locality debt (alone: a read-only summary)",
        description="Without a subcommand, summarise the baselines in the meizel.toml files.",
    )
    debt.add_argument("--json", action="store_true", help="print the summary as JSON")
    modes = debt.add_subparsers(dest="debt_mode")
    recording = modes.add_parser(
        "record", help="write the current violations under paths into baselines"
    )
    recording.add_argument("paths", nargs="+")
    modes.add_parser("prune", help="remove recorded debt that no longer occurs")
    moving = modes.add_parser(
        "carry", help="move recorded debt to where renamed files, targets and symbols now are"
    )
    moving.add_argument(
        "--rename-target",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="a patch target that was renamed (a symbol, not a file); repeatable",
    )
    moving.add_argument(
        "--move-symbol",
        action="append",
        default=[],
        metavar="OLD_FILE::NAME=NEW_FILE",
        help="a top-level function or class that moved to another file; repeatable",
    )
    moving.add_argument(
        "--base",
        metavar="ref",
        help="compare with this ref (default: the branch point with origin/main)",
    )
    growth = modes.add_parser("diff", help="compare recorded totals with a git ref")
    growth.add_argument("--against", required=True, metavar="ref")
    growth.add_argument("--on-growth", required=True, choices=["fail", "warn"])
    growth.add_argument(
        "--waive-if-changed",
        nargs="+",
        default=[],
        metavar="pathspec",
        help="a waiver needs the change to touch these git pathspecs (the rule code)",
    )
    growth.add_argument(
        "--waive-only-within",
        nargs="+",
        default=[],
        metavar="pathspec",
        help="a waiver needs every changed file to match these git pathspecs (a pure rule change)",
    )


def _waiver(root: Path, ref: str, touches: list[str], within: list[str]) -> tuple[bool, str]:
    """Whether the change is a pure rule change, and the sentence that says why not or why."""
    if not (touches or within):
        return False, ""
    rules = changed_under(root, ref, touches) if touches else []
    if touches and not rules:
        return False, ""
    outside = changed_outside(root, ref, within) if within else []
    if outside:
        shown = ", ".join(outside[:3]) + (" ..." if len(outside) > 3 else "")
        return False, f"not waived: the change also edits files outside the rules ({shown})"
    return True, f"this change edits only the rules ({', '.join(rules[:3])})"


def _run_diff(root: Path, ref: str, on_growth: str, args: argparse.Namespace) -> int:
    lines, grew = diff(root, ref)
    print("\n".join(lines))
    if not grew:
        print(f"meizel: no growth against {ref}")
        return 0
    waived, why = _waiver(root, ref, args.waive_if_changed, args.waive_only_within)
    if waived and on_growth == "fail":
        print(f"meizel: growth is reported, not failed: {why}")
        on_growth = "warn"
    elif why:
        print(f"meizel: growth {why}")
    warning = "" if on_growth == "fail" else " warning:"
    print(f"meizel:{warning} recorded debt grew against {ref}", file=sys.stderr)
    return 1 if on_growth == "fail" else 0


def _run_record(root: Path, targets: list[str], jobs: int | None) -> int:
    written, created, unrecordable = record(root, targets, jobs)
    for path in written:
        print(f"meizel: wrote {path}")
    if created:
        print(
            f"meizel: {len(created)} manifests created; stage them with git add, "
            "because check reads tracked files only"
        )
    for diagnostic in unrecordable:
        print(diagnostic.render())
    if unrecordable:
        codes = ", ".join(sorted({d.code.value for d in unrecordable}))
        print(
            f"meizel: {len(unrecordable)} violations cannot be recorded [{codes}]", file=sys.stderr
        )
        return 1
    return 0


def run_debt(root: Path, base: Path, args: argparse.Namespace, jobs: int | None) -> int:
    mode = args.debt_mode
    if mode is None:
        print(summarize(root, as_json=args.json))
        return 0
    if args.json:
        raise MeizelError("--json belongs to the summary: `meizel debt --json`")
    if mode == "prune":
        for line in prune_debt(root, jobs):
            print(line)
        return 0
    if mode == "diff":
        return _run_diff(root, args.against, args.on_growth, args)
    if mode == "carry":
        lines = carry_debt(root, args.base, args.rename_target, args.move_symbol)
        for line in lines:
            print(f"meizel: carried {line}")
        if not lines:
            print(
                "meizel: nothing to carry: no renamed file, target or moved symbol has recorded debt"
            )
        return 0
    return _run_record(root, [relative_target(root, base, p) for p in args.paths], jobs)


def run_sync(root: Path, base: Path, args: argparse.Namespace) -> int:
    if not (args.prune or args.declare):
        raise MeizelError("sync needs --prune or --declare")
    if args.paths and not args.declare:
        raise MeizelError("paths narrow --declare only")
    for line in prune(root, args.jobs):
        print(line)
    left: list[str] = []
    if args.declare:
        for line in prune_debt(root, args.jobs):
            print(line)
        targets = [relative_target(root, base, p) for p in args.paths] if args.paths else None
        lines, left = declare(root, targets, args.jobs)
        for line in lines:
            print(line)
        for line in left:
            print(line)
    if left:
        print(
            f"meizel: {len(left)} dependencies need a code change, not a declaration",
            file=sys.stderr,
        )
        return 1
    return 0


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
            return run_check(root, base, args.paths, args.limit, args.jobs, args.changed)
        if args.command == "debt":
            return run_debt(root, base, args, args.jobs)
        if args.command == "policy":
            for line in policy_changes(
                policy_at(root, args.against), load_policy(load_universe(root))[0]
            ):
                print(line)
            return 0
        if args.command == "explain":
            print(explain(analyze(root, jobs=args.jobs), relative_target(root, base, args.path)))
            return 0
        if args.command == "roles":
            return run_roles(root, args)
        if args.command == "sync":
            return run_sync(root, base, args)
        print(bootstrap(root, args.out, args.jobs))
        return 0
    except MeizelError as error:
        print(f"meizel: {error}", file=sys.stderr)
        return 2
