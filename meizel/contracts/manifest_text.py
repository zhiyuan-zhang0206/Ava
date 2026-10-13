"""Line-level reading and editing of `meizel.toml` text, so comments and layout survive."""

from __future__ import annotations

import json
import re

__all__ = [
    "ENTRY",
    "HEADER",
    "add_exports",
    "add_uses",
    "entry_end",
    "items",
    "unquote",
]

HEADER = re.compile(r"^\s*\[([\w.-]+)\]\s*(?:#.*)?$")
ENTRY = re.compile(r"""^(\s*)("[^"]*"|'[^']*'|[\w.-]+)\s*=\s*\[""")
QUOTED = re.compile(r""""([^"]*)"|'([^']*)'""")


def unquote(key: str) -> str:
    return key[1:-1] if key[:1] in {'"', "'"} else key


def entry_end(lines: list[str], start: int) -> int:
    """Index of the line holding the `]` that closes the list opened on `start`."""
    depth = 0
    for index in range(start, len(lines)):
        text = re.sub(r"""("[^"]*"|'[^']*')""", "", lines[index].split("#", 1)[0])
        depth += text.count("[") - text.count("]")
        if depth <= 0:
            return index
    return len(lines) - 1


def items(block: str) -> list[str]:
    body = block[block.index("[") + 1 : block.rindex("]")]
    uncommented = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    return [a or b for a, b in QUOTED.findall(uncommented)]


def _line(prefix: str, key: str | None, values: list[str]) -> str:
    listing = "[" + ", ".join(json.dumps(v) for v in values) + "]"
    return (
        f"{prefix}{json.dumps(key)} = {listing}"
        if key is not None
        else f"{prefix}exports = {listing}"
    )


def _section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    """Indexes of the lines after the `[section]` header up to the next header."""
    for index, text in enumerate(lines):
        match = HEADER.match(text)
        if match and match.group(1) == section:
            end = next(
                (i for i in range(index + 1, len(lines)) if HEADER.match(lines[i])), len(lines)
            )
            return index + 1, end
    return None


def _entries(lines: list[str], start: int, end: int) -> list[tuple[str, int, int]]:
    found: list[tuple[str, int, int]] = []
    index = start
    while index < end:
        entry = ENTRY.match(lines[index])
        if entry:
            last = entry_end(lines, index)
            found.append((unquote(entry.group(2)), index, last))
            index = last + 1
        else:
            index += 1
    return found


def _new_table(section: str, additions: dict[str, set[str]]) -> list[str]:
    return [f"[{section}]", *(_line("", k, sorted(additions[k])) for k in sorted(additions))]


def _append_table(lines: list[str], block: list[str]) -> str:
    """`block` placed before the first `[debt...]` table, or at the end."""
    debt = next((i for i, x in enumerate(lines) if re.match(r"^\s*\[\s*debt\b", x)), len(lines))
    before = lines[:debt]
    while before and not before[-1].strip():
        before.pop()
    after = lines[debt:]
    return (
        "\n".join([*before, *([""] if before else []), *block, *(["", *after] if after else [])])
        + "\n"
    )


def _merge_existing(lines: list[str], pending: dict[str, set[str]]) -> None:
    """Rewrite the `[uses]` entries named in `pending` (last first, so indexes stay valid)."""
    bounds = _section_bounds(lines, "uses")
    if bounds is None:
        return
    for key, first, last in reversed(_entries(lines, *bounds)):
        if key in pending:
            merged = sorted(set(items("\n".join(lines[first : last + 1]))) | pending.pop(key))
            indent = ENTRY.match(lines[first])
            lines[first : last + 1] = [_line(indent.group(1) if indent else "", key, merged)]


def _insert_new(lines: list[str], pending: dict[str, set[str]]) -> None:
    """Insert the keys left in `pending` in key order among the `[uses]` entries."""
    for key in sorted(pending):
        start, end = _section_bounds(lines, "uses") or (len(lines), len(lines))
        entries = _entries(lines, start, end)
        later = next((first for k, first, _ in entries if k > key), None)
        position = later if later is not None else (entries[-1][2] + 1 if entries else start)
        lines.insert(position, _line("", key, sorted(pending[key])))


def add_uses(text: str, additions: dict[str, set[str]]) -> str:
    """`text` with `additions` (source child -> target children) merged into `[uses]`."""
    lines = text.splitlines()
    if _section_bounds(lines, "uses") is None:
        return _append_table(lines, _new_table("uses", additions))
    pending = {k: set(v) for k, v in additions.items()}
    _merge_existing(lines, pending)
    _insert_new(lines, pending)
    return "\n".join(lines) + "\n"


def add_exports(text: str, names: set[str]) -> str:
    """`text` with `names` merged into the top-level `exports` list."""
    lines = text.splitlines()
    top = next((i for i, x in enumerate(lines) if HEADER.match(x)), len(lines))
    for index in range(top):
        if re.match(r"^exports\s*=\s*\[", lines[index]):
            last = entry_end(lines, index)
            merged = sorted(set(items("\n".join(lines[index : last + 1]))) | names)
            lines[index : last + 1] = [_line("", None, merged)]
            return "\n".join(lines) + "\n"
    row = _line("", None, sorted(names))
    return "\n".join([row, "", *lines] if lines else [row]) + "\n"
