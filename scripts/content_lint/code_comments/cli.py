import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from meizel.graph.universe import load_universe, toplevel
from scripts.content_lint.code_comments.changes import incremental_notes
from scripts.content_lint.code_comments.check import Repository, Result, check
from scripts.content_lint.code_comments.render import render
from scripts.content_lint.code_comments.typescript_index import SUFFIXES, load_index


def selected_paths(repo: Repository, raw: list[str]) -> frozenset[str]:
    if not raw:
        return frozenset(path for path in repo.files if path.endswith((".py", *SUFFIXES)))
    selected: set[str] = set()
    for item in raw:
        path = (repo.root / item).resolve().relative_to(repo.root.resolve()).as_posix()
        candidates = {file for file in repo.files if file == path or file.startswith(f"{path}/")}
        if not candidates:
            raise ValueError(f"no tracked files under {item}")
        if path in repo.files and not path.endswith((".py", *SUFFIXES)):
            raise ValueError(
                f"no source parser for {path}; select supported source files or a directory"
            )
        selected.update(file for file in candidates if file.endswith((".py", *SUFFIXES)))
    if not selected:
        raise ValueError("source check selected no supported source files")
    return frozenset(selected)


def report_result(
    result: Result,
    command: str,
    paths: frozenset[str] | None,
    notes: dict[str, frozenset[int]] | None,
    *,
    json_output: bool,
) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "mode": command,
                    "contracts": len(result.contracts),
                    "strict_source_files": len(paths) if paths is not None else 0,
                    "new_or_rewritten_notes": sum(map(len, notes.values()))
                    if notes is not None
                    else None,
                    "findings": [asdict(item) for item in result.findings],
                },
                indent=2,
            )
        )
    else:
        for finding in result.findings:
            print(finding.render())
        scope = (
            "typed references only; free-comment migration is not checked"
            if paths is None
            else f"{sum(map(len, notes.values()))} new or rewritten notes in {len(paths)} changed sources; unchanged prose is not migrated"
            if notes is not None
            else f"{len(paths)} source files checked for free comments"
        )
        counts = dict(sorted(Counter(item.code for item in result.findings).items()))
        print(f"code comments: {len(result.contracts)} contracts; {scope}; {counts}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Typed source references and colocated documentation contracts"
    )
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--typescript-index", type=str, help="parser output path, or - for stdin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "validate",
        help="validate every typed contract and reference; does not certify free-comment migration",
    )
    strict = commands.add_parser(
        "check",
        help="also reject every free comment, docstring and unused string in the selected source",
    )
    strict.add_argument("paths", nargs="*")
    commands.add_parser(
        "inventory",
        help="report outstanding migration findings; exit nonzero while any remain",
    )
    changed = commands.add_parser(
        "changed", help="check new or rewritten notes against Git while validating every reference"
    )
    changed.add_argument("base", nargs="?")
    changed.add_argument("--from-ref")
    changed.add_argument("--to-ref")
    commands.add_parser(
        "render-api", help="regenerate SDK docstrings from their typed documentation owners"
    )
    args = parser.parse_args(argv)
    try:
        root = args.repo.resolve() if args.repo is not None else toplevel(Path.cwd())
        repo = Repository(root, load_universe(root).files)
        if args.typescript_index is not None:
            content = (
                sys.stdin.read()
                if args.typescript_index == "-"
                else Path(args.typescript_index).read_text(encoding="utf-8")
            )
            load_index(repo, content)
        if args.command == "render-api":
            for path in render(repo):
                print(path)
            return 0
        notes = (
            incremental_notes(repo, args.base, from_ref=args.from_ref, to_ref=args.to_ref)
            if args.command == "changed"
            else None
        )
        paths = (
            frozenset(notes)
            if notes is not None
            else selected_paths(repo, getattr(args, "paths", []))
            if args.command in {"check", "inventory"}
            else None
        )
        result = check(repo, strict_paths=paths, strict_notes=notes)
        report_result(result, args.command, paths, notes, json_output=args.json)
        return int(bool(result.findings))
    except (OSError, ValueError, TypeError, SyntaxError) as error:
        print(f"code comments: cannot complete check: {error}", file=sys.stderr)
        return 2
