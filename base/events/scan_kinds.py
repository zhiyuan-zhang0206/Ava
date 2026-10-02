# ruff: noqa: T201  # CLI tool — printing the inventory is the point.
"""scan_kinds.py — Reproducible event-name inventory for the Ava event registry.

Regenerates the raw material behind base/events/registry.md:

  Terminology: `kind` is the legacy name — the unified event model names the
  field `event_name` (OTel `event.name`; base/events/registry.md). The scanner
  output feeds that registry; static `event=` literals remain the carrier
  (emit's `event_name` argument is positional / variable, not scanned).

  1. Static `event=` literals in production Python code (agent_events event names).
  2. Static `label=` literals on logger calls (label fallback -> agent_events
     event names; see base/log/__init__.py event resolution: event -> label -> "log").
  3. `prepare_event_log` event_type values (category=audit event names).
  4. SSE role discriminators in base/events/live/projection.py (real-time channel,
     not persisted).

Usage:
    python base/events/scan_kinds.py [--repo ~/Ava]

Stdlib-only. Output is a
de-duplicated event-name inventory grouped by mechanism. base/events/registry.md
is generated from the EVENTS registry (scripts/codegen/gen_event_registry.py), not from
this output; this tool remains useful to audit the event= literal distribution
across the codebase.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Directories never scanned (mirrors the repo hygiene excludes).
EXCLUDE_DIRS = {
    ".git",
    ".claude",
    ".worktrees",
    "node_modules",
    ".next",
    "logs",
    "runs",
    "outputs",
    "tmp",
    "work",
    "dist",
    ".venv",
    "venv",
    "__pycache__",
    ".cache",  # unified tool-cache root (pytest/ruff/import-linter/...)
    ".pyright",
    "demos",
    "web",
    "desktop",
    "dashboards",
}

# Top-level directories only: `deploy/` holds service configuration, while
# `base/deploy/` is a production package whose emissions must be scanned.
_ROOT_EXCLUDE_DIRS = {"deploy"}

EVENT_RE = re.compile(r"""\bevent\s*=\s*["']([^"']+)["']""")
LABEL_RE = re.compile(r"""\blabel\s*=\s*["']([^"']+)["']""")
EVENT_TYPE_RE = re.compile(r"""event_type\s*=\s*["']([^"']+)["']""")
SSE_ROLE_RE = re.compile(r'role: Literal\["([^"]+)"\]')


def iter_production_py(repo: Path) -> Iterator[tuple[str, Path]]:
    """Yield (relative_path, path) for every production .py file."""
    for dirpath, dirnames, filenames in os.walk(repo):
        excluded = EXCLUDE_DIRS | _ROOT_EXCLUDE_DIRS if Path(dirpath) == repo else EXCLUDE_DIRS
        dirnames[:] = [d for d in dirnames if d not in excluded]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = Path(dirpath) / fn
            rel = str(path.relative_to(repo))
            if "/tests/" in rel or rel.startswith("tests/"):
                continue
            yield rel, path


def walk_py(repo: Path) -> Iterator[tuple[str, list[str]]]:
    """Yield (relative_path, lines) for every production .py file."""
    for rel, path in iter_production_py(repo):
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        yield rel, lines


def scan_code(repo: Path) -> tuple[Counter[str], Counter[str], Counter[str], Counter[str]]:
    """Return (event_kinds, label_kinds, event_type_kinds, sse_roles)."""
    event_kinds: Counter[str] = Counter()
    label_kinds: Counter[str] = Counter()
    event_type_kinds: Counter[str] = Counter()
    sse_roles: Counter[str] = Counter()
    for _, lines in walk_py(repo):
        for line in lines:
            event_kinds.update(m.group(1) for m in EVENT_RE.finditer(line))
            label_kinds.update(m.group(1) for m in LABEL_RE.finditer(line))
            event_type_kinds.update(m.group(1) for m in EVENT_TYPE_RE.finditer(line))
    # The live-event projection declares the SSE roles; a missing file is a moved
    # module this scanner must follow, not an empty role set.
    with (repo / "base" / "events" / "live" / "projection.py").open(encoding="utf-8") as f:
        for line in f:
            sse_roles.update(m.group(1) for m in SSE_ROLE_RE.finditer(line))
    return event_kinds, label_kinds, event_type_kinds, sse_roles


# ── label-only loguru record calls (AST) ────────────────────────────────────


@dataclass(frozen=True)
class LabelOnlyCall:
    """One loguru record call carrying `label=` and no `event=` in its chain.

    `label` is the literal value when the keyword argument is a plain string
    constant; None marks a non-literal (dynamic) expression whose registeredness
    cannot be proven statically — the label-only gate in
    tests/test_lint_event_kinds.py fails that closed.
    """

    path: str
    lineno: int
    label: str | None


# loguru record-level methods — the call that writes the record. `log` takes
# the level name as its first positional argument.
_LEVEL_METHODS = frozenset(
    {"trace", "debug", "info", "success", "warning", "error", "critical", "exception", "log"}
)

# Methods that return a derived logger — the only chain links this scan
# follows. loguru also has `.patch()`; unused in this repo, and a chain
# through anything else is simply not matched.
_CHAIN_METHODS = frozenset({"bind", "opt"})


def _logger_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(names bound to a loguru logger, `base.log` module aliases).

    Both `from base.log import logger` and `from loguru import logger` bind
    the same singleton: base.log configures its sinks on loguru's global
    logger, so a record emitted through either spelling resolves
    event -> label -> "log" identically — and an unregistered label raises in
    the sink the same way.
    """
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in ("loguru", "base.log"):
                for alias in node.names:
                    if alias.name == "logger":
                        names.add(alias.asname or "logger")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("loguru", "base.log"):
                    modules.add(alias.asname or alias.name)
    return names, modules


def _is_logger_root(node: ast.expr, names: set[str], modules: set[str]) -> bool:
    """Whether `node` is a raw logger expression.

    Accepted roots: a name imported from `loguru` / `base.log` (incl. `as`
    renames), `<alias>.logger` for a `loguru` / `base.log` module alias, and
    the literal `base.log.logger` attribute chain.
    """
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute) and node.attr == "logger":
        receiver = node.value
        if isinstance(receiver, ast.Name) and receiver.id in modules:
            return True
        return (
            isinstance(receiver, ast.Attribute)
            and receiver.attr == "log"
            and isinstance(receiver.value, ast.Name)
            and receiver.value.id == "base"
        )
    return False


def _keyword_args(call: ast.Call) -> dict[str, ast.expr]:
    """The call's keyword arguments by name (`**spread` is not visible)."""
    return {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}


def _label_only_call(
    call: ast.Call, filename: str, names: set[str], modules: set[str]
) -> LabelOnlyCall | None:
    """The finding when one call node is a label-only loguru record call.

    Chain keywords merge in write order — inner `.bind(...)` first, each later
    link next, the record call's own keywords last. loguru applies them the
    same way (verified: a later `.bind` and the record call's kwargs override
    earlier bound values), so the merged `label` and `event` are what the sink
    would resolve.
    """
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in _LEVEL_METHODS):
        return None

    layers = [_keyword_args(call)]  # the record call itself — outermost, wins
    node: ast.expr = func.value
    while isinstance(node, ast.Call):
        inner = node.func
        if not (isinstance(inner, ast.Attribute) and inner.attr in _CHAIN_METHODS):
            return None  # not a bind/opt chain — not this call shape
        layers.append(_keyword_args(node))
        node = inner.value
    if not _is_logger_root(node, names, modules):
        return None

    merged: dict[str, ast.expr] = {}
    for layer in reversed(layers):
        merged.update(layer)
    if "event" in merged or "label" not in merged:
        return None
    label_node = merged["label"]
    label: str | None = None
    if isinstance(label_node, ast.Constant) and isinstance(label_node.value, str):
        label = label_node.value
    return LabelOnlyCall(path=filename, lineno=call.lineno, label=label)


def find_label_only_calls(source: str, *, filename: str = "<string>") -> list[LabelOnlyCall]:
    """Every label-only loguru record call in one source string.

    "Label-only" means the merged chain keywords carry `label=` and no
    `event=`: at runtime the sink resolves event -> label -> "log", so such a
    call derives its event name from the label. Purely static; a non-literal
    label is reported with label=None (dynamic).
    """
    tree = ast.parse(source, filename=filename)
    names, modules = _logger_bindings(tree)
    findings: list[LabelOnlyCall] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            found = _label_only_call(node, filename, names, modules)
            if found is not None:
                findings.append(found)
    return sorted(findings, key=lambda f: (f.path, f.lineno))


def scan_label_only_calls(repo: Path) -> list[LabelOnlyCall]:
    """Every label-only loguru record call across production .py files."""
    findings: list[LabelOnlyCall] = []
    for rel, path in iter_production_py(repo):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "logger" not in source:
            continue  # a record call cannot exist without the name
        findings.extend(find_label_only_calls(source, filename=rel))
    return sorted(findings, key=lambda f: (f.path, f.lineno))


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--repo", default=str(Path.home() / "Ava"))
    args = ap.parse_args()

    event_kinds, label_kinds, event_type_kinds, sse_roles = scan_code(Path(args.repo))

    print(f"===== agent_events event= literals (prod code): {len(event_kinds)} =====")
    for k, n in event_kinds.most_common():
        print(f"  {n:4d}  {k}")
    print(f"\n===== label= fallback literals (prod code): {len(label_kinds)} =====")
    for k, n in label_kinds.most_common():
        print(f"  {n:4d}  {k}")
    print(f"\n===== event_log event_type literals (prod code): {len(event_type_kinds)} =====")
    for k, n in event_type_kinds.most_common():
        print(f"  {n:4d}  {k}")
    print(
        f"\n===== SSE role discriminators (base/events/live/projection.py): {len(sse_roles)} ====="
    )
    for k in sorted(sse_roles):
        print(f"  {k}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
