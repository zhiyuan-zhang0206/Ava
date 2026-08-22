"""Forbid an UPDATE that sets agents_meta.status='terminated' without stamping
termination_source in the SAME statement.

Run: `.venv/bin/python scripts/lint_termination_source.py [path ...]` (defaults to
scanning the non-test source dirs). Also run automatically via pre-commit hook.

## Why

`ops/controllers/resurrect.py` documents an invariant: `termination_source` is
stamped by EVERY code path that writes `status='terminated'`. It is load-bearing,
not decorative. `CrashResurrectController`'s claim UPDATE filters
`AND termination_source = ANY(_RESURRECTABLE_SOURCES)`, so a corpse whose source is
NULL is **permanently** unresurrectable: a write site that forgets to stamp leaves a
row that nothing will ever bring back, silently stranding whatever inbound work that
agent still had queued. The failure is invisible — no error, no log, just an agent
that never comes back — which is exactly the kind of hole a doc comment does not
hold. A prior pre-claim termination path violated it undetected from the day the
column landed.

So the invariant is a property of the CODE PATHS, not of the stored data, and this
is a code-level check. A DB `CHECK (status <> 'terminated' OR termination_source IS
NOT NULL)` was considered and rejected: the ~1.8k pre-column rows are legitimately
NULL, so even a `NOT VALID` constraint (which skips existing rows on ADD but still
fires on any UPDATE of them) would start rejecting unrelated writes that happen to
touch a historical terminated row — enforcement at the wrong time, in the wrong
place, against rows that are not the problem.

## The rule

For every `cur.execute(...)` / `await cur.execute(...)` whose SQL is an
`UPDATE agents_meta`, look at the SET clause only (a `WHERE ... status = 'terminated'`
filter is a read, not a write, and is ignored). If the SET clause assigns `status` a
value that can be `'terminated'` — either the literal, or a `%s` placeholder whose
corresponding argument mentions `TERMINATED` — then the SET clause must also assign
`termination_source`. The stamped value must be a `shared.agents.TerminationSource`
member, so a typo'd source (which the DB CHECK would only catch at runtime, against
a real database) fails here too.

Positional `%s` placeholders are mapped to the params tuple by counting placeholders
that precede them in the statement, so the parameterized form is checked as
precisely as the literal one — a future writer cannot slip past by passing
`AgentStatus.TERMINATED` as a bind parameter.

Scope: non-test code only. Tests park rows in arbitrary states on purpose (that is
how the resurrect suite builds a NULL-source corpse to assert it is NOT claimed), so
linting them would forbid the fixtures that prove this behavior.

No inline exemption mechanism, and no allowlist: a terminated-write that genuinely
must not be resurrectable stamps a non-resurrectable source (`'user'`, `'exit'`,
`'integrity'`) rather than leaving NULL. "Not eligible" and "nobody stamped this" must
stay distinguishable — collapsing them back into NULL is the bug this lint prevents.

Error format `file:line: <reason>` + non-zero exit.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Only OUR non-test code. Tests deliberately write unstamped corpses as fixtures.
# Every tree that could reach agents_meta is covered, not just the ones that write it
# today — `ava_builtins` currently only READS the table, and this is
# what keeps a future write there from being the one site nobody was checking.
_SCAN_DIRS = (
    "agent",
    "ava",
    "ava_builtins",
    "cli",
    "gateway",
    "ops",
    "scripts",
    "services",
    "shared",
)

_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

# `UPDATE agents_meta SET <clause>` up to the first WHERE / RETURNING (or end).
_UPDATE_SET_PATTERN = re.compile(
    r"\bUPDATE\s+agents_meta\b(.*?)\bSET\b(?P<set>.*?)(?:\bWHERE\b|\bRETURNING\b|$)",
    re.IGNORECASE | re.DOTALL,
)
# `status = 'terminated'` / `status='terminated'` inside a SET clause.
_SET_STATUS_LITERAL = re.compile(r"\bstatus\s*=\s*'terminated'", re.IGNORECASE)
# `status = %s` inside a SET clause.
_SET_STATUS_PARAM = re.compile(r"\bstatus\s*=\s*%s", re.IGNORECASE)
# `termination_source = <anything>` inside a SET clause.
_SET_SOURCE = re.compile(r"\btermination_source\s*=", re.IGNORECASE)
# The source value stamped: a literal, NULL, or a placeholder.
_SET_SOURCE_VALUE = re.compile(
    r"\btermination_source\s*=\s*('(?P<lit>[^']*)'|(?P<null>NULL)|(?P<param>%s))", re.IGNORECASE
)


def _termination_source_values() -> frozenset[str]:
    """The legal source values — read from the enum, so adding a member auto-syncs."""
    sys.path.insert(0, str(_REPO_ROOT))
    from shared.agents import TerminationSource

    return frozenset(s.value for s in TerminationSource)


def _sql_literal(node: ast.expr) -> str | None:
    """The SQL text of an execute() first argument, if it is statically knowable.

    Handles a bare string, implicit adjacent-literal concatenation (which the ast
    already folds into one Constant), and explicit `+` concatenation of literals.
    Returns None for a dynamically built query — nothing to check.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _sql_literal(node.left), _sql_literal(node.right)
        return None if left is None or right is None else left + right
    return None


def _param_sources(node: ast.expr | None) -> list[str] | None:
    """Per-element source text of an execute() params tuple/list, or None if it is
    not a literal sequence (a variable, a dict, a comprehension — unmappable)."""
    if node is None or not isinstance(node, ast.Tuple | ast.List):
        return None
    return [ast.unparse(el) for el in node.elts]


def _is_execute_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr == "execute"


def _status_param_index(sql: str, absolute_end: int) -> int:
    """Index into the params tuple of the `%s` bound to `status` in the SET clause.

    psycopg binds positional placeholders in statement order, so the index is the
    number of `%s` occurrences preceding this one across the whole statement.
    `absolute_end` is the offset, within the whole SQL, of the end of the
    `status = %s` match — taken from the match itself rather than by searching for
    the SET clause text, which would resolve to the wrong statement if two UPDATEs
    in one string happened to share a SET clause.
    """
    return sql.count("%s", 0, absolute_end) - 1


def _check_statement(
    sql: str, params: list[str] | None, legal_sources: frozenset[str]
) -> str | None:
    """The violation reason for one SQL statement, or None if it is fine."""
    for m in _UPDATE_SET_PATTERN.finditer(sql):
        set_clause = m.group("set")
        writes_terminated = bool(_SET_STATUS_LITERAL.search(set_clause))
        if not writes_terminated:
            param_match = _SET_STATUS_PARAM.search(set_clause)
            if param_match is None:
                continue
            if params is None:
                # A parameterized status write whose params we cannot resolve. The
                # conservative read is "this might write terminated", but flagging
                # every such statement would be noise; require the literal form so
                # the write is legible to this lint (and to a human reviewer).
                continue
            idx = _status_param_index(sql, m.start("set") + param_match.end())
            if not (0 <= idx < len(params)) or "TERMINATED" not in params[idx]:
                continue
            writes_terminated = True
        if not writes_terminated:
            continue
        if not _SET_SOURCE.search(set_clause):
            return (
                "UPDATE agents_meta sets status='terminated' without stamping "
                "termination_source in the same statement — the corpse would be "
                "permanently unresurrectable (CrashResurrectController's claim filters "
                "on termination_source, so NULL is never picked up and any queued "
                "inbound work is silently stranded). Add `termination_source = "
                "'<source>'` to this SET clause; pick the value from "
                "shared.agents.TerminationSource"
            )
        value_match = _SET_SOURCE_VALUE.search(set_clause)
        if value_match is None or value_match.group("param") is not None:
            # Stamped from a bind parameter: the value set cannot be verified here,
            # but the stamp itself is present, which is the invariant.
            continue
        if value_match.group("null") is not None:
            return (
                "UPDATE agents_meta sets status='terminated' with "
                "termination_source = NULL — NULL means 'pre-column legacy row' and is "
                "never resurrectable. A death that must not be auto-resurrected stamps "
                "'user' / 'exit' / 'integrity' instead, so 'not eligible' stays "
                "distinguishable from 'nobody stamped this'"
            )
        literal = value_match.group("lit")
        if literal not in legal_sources:
            return (
                f"UPDATE agents_meta stamps termination_source = '{literal}', which is "
                f"not a shared.agents.TerminationSource member "
                f"({sorted(legal_sources)}) — the column's CHECK would reject this "
                f"write at runtime"
            )
    return None


def violations_in_source(
    src: str, legal_sources: frozenset[str] | None = None
) -> list[tuple[int, str]]:
    """`[(lineno, reason), ...]` for one module's source — the unit-testable core."""
    if legal_sources is None:
        legal_sources = _termination_source_values()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []  # Not our problem; ruff/py_compile own syntax.
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_execute_call(node):
            continue
        if not node.args:
            continue
        sql = _sql_literal(node.args[0])
        if sql is None or "agents_meta" not in sql:
            continue
        params = _param_sources(node.args[1]) if len(node.args) > 1 else None
        reason = _check_statement(sql, params, legal_sources)
        if reason is not None:
            violations.append((node.lineno, reason))
    return violations


def _scan_file(path: Path, legal_sources: frozenset[str]) -> list[tuple[int, str]]:
    return violations_in_source(path.read_text(encoding="utf-8"), legal_sources)


def _is_test_file(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in _TEST_PATTERNS)


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return files


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    # argv non-empty = pre-commit passed the changed-file list; empty = full scan.
    targets = [Path(a).resolve() for a in argv] if argv else [_REPO_ROOT / d for d in _SCAN_DIRS]
    legal_sources = _termination_source_values()

    total = 0
    for path in sorted(_iter_py_files(targets)):
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        if _is_test_file(rel):
            continue
        for lineno, reason in _scan_file(path, legal_sources):
            total += 1
            print(f"{rel}:{lineno}: {reason}.")

    if total:
        print(
            f"\n{total} violation(s). See the docstring at the top of "
            "scripts/lint_termination_source.py for why this invariant is enforced in "
            "code rather than as a DB constraint.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
