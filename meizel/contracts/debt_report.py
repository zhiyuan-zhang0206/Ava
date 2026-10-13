"""`debt` and `debt diff`: read-only views of the recorded baselines.

Both read `meizel.toml` files and nothing else, so they never analyse the source and finish in
about a second. The summary reads the working tree; `diff` compares it with a git ref.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from meizel.contracts.debt import TABLE_KEY, DebtTable, parse_debt, total
from meizel.contracts.manifest import manifest_path
from meizel.graph.model import Code, MeizelError
from meizel.graph.universe import MANIFEST, Universe, load_universe, parent_of

__all__ = ["diff", "summarize"]

Baselines = dict[str, DebtTable]


def _parse(text: str, where: str) -> DebtTable:
    problems: list[str] = []
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise MeizelError(f"{where}: invalid TOML: {error}") from error
    table = parse_debt(document[TABLE_KEY], problems.append) if TABLE_KEY in document else {}
    if problems:
        raise MeizelError(f"{where}: " + "; ".join(problems))
    return table


def read_baselines(universe: Universe) -> Baselines:
    """The recorded debt of every directory of the working tree that has any."""
    found: Baselines = {}
    for directory in sorted(universe.manifest_dirs):
        path = manifest_path(directory)
        table = _parse((universe.root / path).read_text(encoding="utf-8"), path)
        if table:
            found[directory] = table
    return found


def _top(directory: str) -> str:
    return directory.split("/", 1)[0] if directory else "."


def _by_code(baselines: Baselines) -> Counter[Code]:
    counts: Counter[Code] = Counter()
    for table in baselines.values():
        for code, rows in table.items():
            counts[code] += sum(rows.values())
    return counts


def _judged_directories(universe: Universe) -> set[str]:
    """Directories with Python anywhere below them: the ones whose rules can be violated."""
    judged: set[str] = set()
    for path in universe.python_files:
        directory = parent_of(path)
        while True:
            judged.add(directory)
            if not directory:
                break
            directory = parent_of(directory)
    return judged


def report(universe: Universe) -> dict[str, Any]:
    baselines = read_baselines(universe)
    tops: dict[str, dict[str, Any]] = {}
    for directory, table in baselines.items():
        top = tops.setdefault(
            _top(directory), {"total": 0, "by_code": Counter(), "strict_to_be_directories": 0}
        )
        top["total"] += total(table)
        top["strict_to_be_directories"] += 1
        for code, rows in table.items():
            top["by_code"][code.value] += sum(rows.values())
    judged = _judged_directories(universe)
    return {
        "total": sum(total(t) for t in baselines.values()),
        "by_code": {c.value: n for c, n in sorted(_by_code(baselines).items(), key=str)},
        "by_top_level": {
            name: {**top, "by_code": dict(sorted(top["by_code"].items()))}
            for name, top in sorted(tops.items())
        },
        "directories": {
            "strict_to_be": len(baselines),
            "strict": len(judged - set(baselines)),
        },
    }


def _text(data: dict[str, Any]) -> str:
    modes = data["directories"]
    lines = [
        f"meizel debt: {data['total']} recorded locality debts in "
        f"{modes['strict_to_be']} strict-to-be directories "
        f"({modes['strict']} strict directories carry none)",
        "",
        "by code:",
        *(f"  {code:<20}{count:>8}" for code, count in data["by_code"].items()),
        "",
        "by top-level directory:",
    ]
    ranked = sorted(data["by_top_level"].items(), key=lambda item: (-item[1]["total"], item[0]))
    lines.extend(
        f"  {name:<20}{top['total']:>8}  in {top['strict_to_be_directories']} directories"
        for name, top in ranked
    )
    return "\n".join(lines)


def summarize(root: Path, *, as_json: bool) -> str:
    data = report(load_universe(root))
    return json.dumps(data, indent=2, sort_keys=True) if as_json else _text(data)


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(root), *args], input=stdin, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise MeizelError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def _baselines_at(root: Path, ref: str) -> Baselines:
    if ref.startswith("-"):
        raise MeizelError(f"{ref}: not a git ref")
    listing = _git(root, "ls-tree", "-r", "-z", "--name-only", ref).decode().split("\0")
    paths = [p for p in listing if p.rsplit("/", 1)[-1] == MANIFEST]
    if not paths:
        return {}
    request = "".join(f"{ref}:{p}\n" for p in paths).encode()
    stream = _git(root, "cat-file", "--batch", stdin=request)
    found: Baselines = {}
    position = 0
    for path in paths:
        end = stream.index(b"\n", position)
        header = stream[position:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise MeizelError(f"{ref}:{path}: cannot be read")
        size = int(header[2])
        body = stream[end + 1 : end + 1 + size].decode()
        position = end + 1 + size + 1
        table = _parse(body, f"{ref}:{path}")
        if table:
            found[parent_of(path)] = table
    return found


def diff(root: Path, ref: str) -> tuple[list[str], bool]:
    """Per-code totals against `ref`, and whether any code grew.

    Totals, not directories, are compared: debt that moves with a file between directories
    leaves them unchanged.
    """
    before = _by_code(_baselines_at(root, ref))
    after = _by_code(read_baselines(load_universe(root)))
    codes = sorted(set(before) | set(after), key=str)
    lines = [f"{'code':<20}{ref[:12]:>14}{'tree':>8}{'change':>8}"]
    grew = False
    for code in codes:
        delta = after[code] - before[code]
        grew = grew or delta > 0
        lines.append(f"{code:<20}{before[code]:>14}{after[code]:>8}{delta:>+8}")
    lines.append(
        f"{'total':<20}{sum(before.values()):>14}{sum(after.values()):>8}{sum(after.values()) - sum(before.values()):>+8}"
    )
    return lines, grew
