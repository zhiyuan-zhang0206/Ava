import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from meizel.graph.model import MeizelError
from meizel.graph.universe import load_universe, toplevel
from scripts.content_lint.code_comments import corpus as corpus_module
from scripts.content_lint.code_comments.changes import incremental_notes
from scripts.content_lint.code_comments.check import SOURCE_SUFFIXES, Repository, Result, check
from scripts.content_lint.code_comments.typescript_index import load_index


def selected_paths(repo: Repository, raw: list[str]) -> frozenset[str]:
    if not raw:
        return frozenset(path for path in repo.files if path.endswith(SOURCE_SUFFIXES))
    selected: set[str] = set()
    for item in raw:
        path = (repo.root / item).resolve().relative_to(repo.root.resolve()).as_posix()
        candidates = {file for file in repo.files if file == path or file.startswith(f"{path}/")}
        if not candidates:
            raise ValueError(f"no tracked files under {item}")
        if path in repo.files and not path.endswith(SOURCE_SUFFIXES):
            raise ValueError(
                f"no source parser for {path}; select supported source files or a directory"
            )
        selected.update(file for file in candidates if file.endswith(SOURCE_SUFFIXES))
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
                    "notes": result.notes,
                    "strict_source_files": len(paths) if paths is not None else 0,
                    "new_or_rewritten_notes": sum(map(len, notes.values()))
                    if notes is not None
                    else None,
                    "findings": [asdict(item) for item in result.findings],
                },
                indent=2,
            )
        )
        return
    for finding in result.findings:
        print(finding.render())
    scope = (
        "links only; free-comment migration is not checked"
        if paths is None
        else f"{sum(map(len, notes.values()))} new or rewritten notes in {len(paths)} changed sources; unchanged prose is not migrated"
        if notes is not None
        else f"{len(paths)} source files checked for free comments"
    )
    counts = dict(sorted(Counter(item.code for item in result.findings).items()))
    print(f"code comments: {result.notes} notes; {scope}; {counts}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parseable source notes with links to tracked repository files"
    )
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--typescript-index", type=str, help="parser output path, or - for stdin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="validate every source note link; does not check prose")
    strict = commands.add_parser(
        "check", help="also reject every free comment, docstring and unused string in the selection"
    )
    strict.add_argument("paths", nargs="*")
    commands.add_parser(
        "inventory",
        help="report outstanding migration findings; exit nonzero while any remain",
    )
    changed = commands.add_parser(
        "changed", help="check new or rewritten notes against Git while validating every link"
    )
    changed.add_argument("base", nargs="?")
    changed.add_argument("--from-ref")
    changed.add_argument("--to-ref")
    dump = commands.add_parser("corpus", help="extract every source note for review")
    dump.add_argument("--format", choices=["jsonl", "markdown"], default="jsonl")
    dump.add_argument("--output", type=Path)
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
        if args.command == "corpus":
            rows = corpus_module.entries(repo)
            content = (
                corpus_module.jsonl(rows)
                if args.format == "jsonl"
                else corpus_module.markdown(rows)
            )
            if args.output is not None:
                corpus_module.write(args.output, content)
                print(f"code comments: {len(rows)} notes written to {args.output}", file=sys.stderr)
            else:
                print(content, end="")
            for finding in result.findings:
                print(finding.render(), file=sys.stderr)
            return int(bool(result.findings))
        report_result(result, args.command, paths, notes, json_output=args.json)
        return int(bool(result.findings))
    except (OSError, ValueError, TypeError, SyntaxError, MeizelError) as error:
        print(f"code comments: cannot complete check: {error}", file=sys.stderr)
        return 2
