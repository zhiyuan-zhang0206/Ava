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
from typing import Any, cast

from meizel.contracts.debt import (
    PATCH_CODES,
    TABLE_KEY,
    DebtTable,
    parse_debt,
    target_of,
    target_under,
    total,
)
from meizel.contracts.manifest import manifest_path
from meizel.graph.model import Code, MeizelError
from meizel.graph.universe import MANIFEST, Universe, load_universe, parent_of

__all__ = ["by_target", "changed_outside", "changed_under", "diff", "summarize"]

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


def _totals_in(text: str) -> Counter[str]:
    """Recorded counts per code in one manifest text, read leniently.

    A ref is history: it may record debt under a code the tool has since retired, and that must
    still count toward its total instead of stopping the comparison.
    """
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return Counter()
    table = document.get(TABLE_KEY)
    counts: Counter[str] = Counter()
    if isinstance(table, dict):
        for code, rows in cast("dict[str, object]", table).items():
            if isinstance(rows, dict):
                counts[code] += sum(
                    n for n in cast("dict[str, object]", rows).values() if isinstance(n, int)
                )
    return counts


def _manifest_texts_at(root: Path, ref: str) -> list[str]:
    """The text of every `meizel.toml` at a git ref."""
    if ref.startswith("-"):
        raise MeizelError(f"{ref}: not a git ref")
    listing = _git(root, "ls-tree", "-r", "-z", "--name-only", ref).decode().split("\0")
    paths = [p for p in listing if p.rsplit("/", 1)[-1] == MANIFEST]
    texts: list[str] = []
    if not paths:
        return texts
    request = "".join(f"{ref}:{p}\n" for p in paths).encode()
    stream = _git(root, "cat-file", "--batch", stdin=request)
    position = 0
    for path in paths:
        end = stream.index(b"\n", position)
        header = stream[position:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise MeizelError(f"{ref}:{path}: cannot be read")
        size = int(header[2])
        texts.append(stream[end + 1 : end + 1 + size].decode())
        position = end + 1 + size + 1
    return texts


def _totals_at(root: Path, ref: str) -> Counter[str]:
    totals: Counter[str] = Counter()
    for text in _manifest_texts_at(root, ref):
        totals.update(_totals_in(text))
    return totals


def changed_under(root: Path, ref: str, pathspecs: list[str]) -> list[str]:
    """The files that differ between `ref` and the working tree under the given git pathspecs.

    No pathspecs lists every changed file.
    """
    if ref.startswith("-"):
        raise MeizelError(f"{ref}: not a git ref")
    listing = _git(root, "diff", "--name-only", "--no-renames", "-z", ref, "--", *pathspecs)
    return [p for p in listing.decode().split("\0") if p]


def changed_outside(root: Path, ref: str, pathspecs: list[str]) -> list[str]:
    """The changed files that match none of the pathspecs."""
    inside = set(changed_under(root, ref, pathspecs))
    return [p for p in changed_under(root, ref, []) if p not in inside]


def diff(root: Path, ref: str) -> tuple[list[str], bool]:
    """Per-code totals against `ref`, and whether any code grew.

    Totals, not directories, are compared: debt that moves with a file between directories
    leaves them unchanged.
    """
    before = _totals_at(root, ref)
    after = Counter(
        {code.value: n for code, n in _by_code(read_baselines(load_universe(root))).items()}
    )
    codes = sorted(set(before) | set(after))
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


Targets = Counter[tuple[str, str]]
_OTHER = "(other)"


def _targets(tables: list[dict[str, dict[str, object]]]) -> Targets:
    """Recorded patch counts per `(code, target)`, read leniently from `code -> key -> count`."""
    counts: Targets = Counter()
    patch_codes = {code.value for code in PATCH_CODES}
    for table in tables:
        for code, rows in table.items():
            if code in patch_codes:
                for key, count in rows.items():
                    if isinstance(count, int):
                        counts[(code, target_of(key))] += count
    return counts


def _tables_in(text: str) -> list[dict[str, dict[str, object]]]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    table = document.get(TABLE_KEY)
    if not isinstance(table, dict):
        return []
    rows = cast("dict[str, object]", table)
    return [{c: cast("dict[str, object]", r) for c, r in rows.items() if isinstance(r, dict)}]


def _group(target: str, prefixes: list[str], depth: int) -> str:
    """The row a target is counted under: its longest listed prefix, or its first `depth` names."""
    if prefixes:
        inside = [p for p in prefixes if target_under(target, p)]
        return max(inside, key=len) if inside else _OTHER
    return ".".join(target.split(".")[:depth])


def _grouped(
    before: Targets, after: Targets, prefixes: list[str], depth: int
) -> dict[tuple[str, str], list[int]]:
    """`(code, group) -> [before, after]`, with a row for every listed prefix."""
    groups: dict[tuple[str, str], list[int]] = {}
    for side, counts in enumerate((before, after)):
        for (code, target), n in counts.items():
            groups.setdefault((code, _group(target, prefixes, depth)), [0, 0])[side] += n
    for prefix in prefixes:
        if not any(group == prefix for _, group in groups):
            groups[("-", prefix)] = [0, 0]
    return groups


def by_target(root: Path, ref: str, prefixes: list[str], depth: int) -> list[str]:
    """Recorded patch debt per target group against `ref`: what a change paid down or added.

    Counts come from the baselines, keyed by the target of each entry, so debt that moves with
    its file stays where it was. Without prefixes the groups are the first `depth` dotted names
    of a target; with them, a target counts under its longest prefix and the rest under `(other)`.
    Unchanged groups are left out, except the listed prefixes, which always show.
    """
    before = _targets([t for text in _manifest_texts_at(root, ref) for t in _tables_in(text)])
    tree = read_baselines(load_universe(root))
    after = _targets(
        [{c.value: dict(rows) for c, rows in table.items()} for table in tree.values()]
    )
    groups = _grouped(before, after, prefixes, depth)
    rows = sorted(
        (item for item in groups.items() if item[1][0] != item[1][1] or item[0][1] in prefixes),
        key=lambda item: (item[1][1] - item[1][0], item[0][1], item[0][0]),
    )
    width = max([len("target"), *(len(group) for (_, group), _ in rows)])
    lines = [f"{'target':<{width}}  {'code':<18}{ref[:12]:>12}{'tree':>8}{'change':>8}"]
    lines.extend(
        f"{group:<{width}}  {code:<18}{old:>12}{new:>8}{new - old:>+8}"
        for (code, group), (old, new) in rows
    )
    old_total, new_total = sum(before.values()), sum(after.values())
    lines.append(
        f"{'all patch debt':<{width}}  {'':<18}{old_total:>12}{new_total:>8}{new_total - old_total:>+8}"
    )
    return lines
