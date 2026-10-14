"""`debt record` and `debt prune`: the only commands that change a baseline.

`record` is the only way a baseline grows. `check` and `bootstrap` never write one.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.debt import CYCLE_EDGE, TABLE_KEY, DebtTable, local_edge, set_debt, total
from meizel.contracts.ledger import tally
from meizel.contracts.manifest import Manifest, manifest_path
from meizel.contracts.rule import cycle_edge
from meizel.graph.graphs import feedback_edges, unneeded_back_edges
from meizel.graph.model import Code, Diagnostic, MeizelError

__all__ = ["prune", "record", "write_baseline"]


def _within(path: str, targets: list[str]) -> bool:
    return any(not t or path == t or path.startswith(f"{t}/") for t in targets)


def _refuse_malformed(analysis: Analysis, action: str) -> None:
    malformed = [d for d in analysis.diagnostics if d.code is Code.MANIFEST_DEBT]
    if malformed:
        shown = "\n".join(d.render() for d in malformed[:5])
        raise MeizelError(f"cannot {action} while a baseline is malformed:\n{shown}")


def _copy(table: DebtTable) -> DebtTable:
    return {code: dict(rows) for code, rows in table.items()}


def write_baseline(root: Path, owner: str, table: DebtTable) -> str | None:
    """Rewrite the `[debt]` tables of `owner`'s manifest; the path written, or None if unchanged."""
    path = root / manifest_path(owner)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    after = set_debt(before, table)
    if after == before:
        return None
    if tomllib.loads(after).get(TABLE_KEY, {}) != {c.value: r for c, r in table.items() if r}:
        raise MeizelError(
            f"{manifest_path(owner)}: the [debt] tables cannot be rewritten; edit by hand"
        )
    if after.strip():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after, encoding="utf-8")
    else:
        path.unlink()
    return manifest_path(owner)


def _new_back_edges(manifest: Manifest) -> set[tuple[str, str]]:
    """The fewest declared edges, beyond the recorded ones, to treat as back-edges so the graph is acyclic."""
    uses = manifest.graph()
    carried = set(manifest.back_edges())
    base = {src: [t for t in targets if (src, t) not in carried] for src, targets in uses.items()}
    chosen = carried | set(feedback_edges(base))
    return (chosen - set(unneeded_back_edges(uses, chosen))) - carried


def _cycle_facts(analysis: Analysis, manifest: Manifest) -> dict[str, int]:
    """Per `source -> target` key, the facts on the edges newly chosen as back-edges."""
    new = _new_back_edges(manifest)
    if not new:
        return {}
    seen: dict[tuple[str, int, str], str] = {}
    for proj in analysis.projections:
        if proj.exempt or proj.lca != manifest.directory or (proj.cf, proj.ct) not in new:
            continue
        diagnostic = cycle_edge(proj)
        if diagnostic.debt is not None:
            seen[(diagnostic.path, diagnostic.line, diagnostic.message)] = diagnostic.debt.key
    counts: dict[str, int] = {}
    for key in seen.values():
        counts[key] = counts.get(key, 0) + 1
    return counts


def _recordable(analysis: Analysis, targets: list[str]) -> list[Diagnostic]:
    return [d for d in analysis.diagnostics if d.debt is not None and _within(d.path, targets)]


def record(
    root: Path, targets: list[str], jobs: int | None = None
) -> tuple[list[str], list[str], list[Diagnostic]]:
    """Record the violations under `targets`.

    Returns the manifests written, the ones among them that did not exist before, and every
    violation under `targets` that no baseline can hold.

    A count never goes down here (that is `prune`), and a violation outside `targets` is not
    recorded. The second result is every violation under `targets` that no baseline can hold.
    """
    analysis = analyze(root, jobs=jobs)
    _refuse_malformed(analysis, "record")
    tables = {owner: _copy(m.debt) for owner, m in analysis.manifests.items()}
    for (owner, code, key), found in tally(_recordable(analysis, targets)).items():
        rows = tables.setdefault(owner, {}).setdefault(code, {})
        rows[key] = max(rows.get(key, 0), len(found))
    for owner, manifest in analysis.manifests.items():
        if _within(owner, targets):
            facts = _cycle_facts(analysis, manifest)
            if facts:
                tables[owner].setdefault(CYCLE_EDGE, {}).update(facts)
    written: list[str] = []
    created: list[str] = []
    for owner, table in sorted(tables.items()):
        if path := write_baseline(root, owner, table):
            written.append(path)
            if owner not in analysis.manifests:
                created.append(path)
    unrecordable = [
        d
        for d in analysis.diagnostics
        if d.debt is None
        and d.code not in {Code.MANIFEST_CYCLE, Code.DEBT_PAID}
        and _within(d.path, targets)
    ]
    return written, created, unrecordable


def prune(root: Path, jobs: int | None = None) -> list[str]:
    """Lower every baseline to what still occurs; returns a line per manifest rewritten."""
    analysis = analyze(root, jobs=jobs)
    _refuse_malformed(analysis, "prune")
    found = tally(analysis.diagnostics)
    lines: list[str] = []
    for owner, manifest in sorted(analysis.manifests.items()):
        if not manifest.debt:
            continue
        unneeded = set(unneeded_back_edges(manifest.graph(), manifest.back_edges()))
        table: DebtTable = {}
        for code, rows in manifest.debt.items():
            kept = {
                key: min(n, len(found.get((owner, code, key), ())))
                for key, n in rows.items()
                if not (code is CYCLE_EDGE and local_edge(key) in unneeded)
            }
            table[code] = {key: n for key, n in kept.items() if n > 0}
        if write_baseline(root, owner, table):
            lines.append(
                f"{manifest_path(owner)}: {total(manifest.debt)} recorded debts lowered to {total(table)}"
            )
    return lines
