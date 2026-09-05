"""Git commit identity facts used by failure-routing producers."""

from __future__ import annotations

import re

_AVA_COAUTHOR_PREFIX = "Co-authored-by: Ava"
_AVA_COAUTHOR = re.compile(r"^Co-authored-by: Ava #([1-9][0-9]*)$")


def parse_ava_coauthor_agent_ids(commit_message: str) -> list[int] | None:
    """Return valid Ava co-author ids in trailer order.

    ``None`` means the commit contains no Ava co-author assertion. An empty
    list means it contains one or more Ava-prefixed trailer lines but every
    one is malformed. Malformed lines are ignored when valid trailers coexist,
    preserving the full ordered list of usable authors.
    """
    lines = commit_message.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    trailer_start = len(lines)
    while trailer_start > 0 and lines[trailer_start - 1].strip():
        trailer_start -= 1

    saw_ava_trailer = False
    agent_ids: list[int] = []
    for line in lines[trailer_start:]:
        if not line.startswith(_AVA_COAUTHOR_PREFIX):
            continue
        saw_ava_trailer = True
        match = _AVA_COAUTHOR.fullmatch(line)
        if match is not None:
            agent_ids.append(int(match.group(1)))
    return agent_ids if saw_ava_trailer else None
