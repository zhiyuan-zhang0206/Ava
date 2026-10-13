"""Reconcile what the rules found with what each directory has recorded as debt.

A directory with an empty baseline is strict: every violation fails. A directory with recorded
debt is strict-to-be: recorded violations pass, a new one fails, and a recorded debt that no
longer occurs fails too, so the baseline can only shrink.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import replace

from meizel.contracts.debt import CYCLE, owner_hint, source_of, total
from meizel.contracts.manifest import Manifest
from meizel.graph.model import Code, Diagnostic

__all__ = ["Tally", "debts_in", "reconcile", "tally"]

type Tally = dict[tuple[str, Code, str], list[Diagnostic]]


def tally(diagnostics: Iterable[Diagnostic]) -> Tally:
    """The violations that can be recorded, grouped by owner directory, code and key."""
    groups: Tally = defaultdict(list)
    for diagnostic in diagnostics:
        if diagnostic.debt is not None:
            groups[(diagnostic.debt.owner, diagnostic.code, diagnostic.debt.key)].append(diagnostic)
    return groups


def _excess(
    found: list[Diagnostic], recorded: int, owner: str, manifests: dict[str, Manifest]
) -> list[Diagnostic]:
    manifest = manifests.get(owner)
    notes: list[str] = []
    if recorded:
        notes.append(f"{recorded} recorded, {len(found)} found")
    if manifest is not None and manifest.debt:
        notes.append(owner_hint(owner, manifest.debt))
    return [replace(d, hints=(*d.hints, *notes)) for d in found]


def _paid(
    manifest: Manifest,
    groups: Tally,
    in_scope: Callable[[str], bool],
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for code, rows in sorted(manifest.debt.items(), key=lambda item: str(item[0])):
        if code is CYCLE:
            continue
        for key, recorded in sorted(rows.items()):
            source = source_of(manifest.directory, code, key)
            remaining = len(groups.get((manifest.directory, code, key), ()))
            if remaining >= recorded or (source is not None and not in_scope(source)):
                continue
            found.append(
                Diagnostic(
                    manifest.path,
                    manifest.debt_line(code, key),
                    Code.DEBT_PAID,
                    f"{code} debt {key!r}: {recorded} recorded but {remaining} remain",
                    ("run `python -m meizel debt prune` to lower the baseline",),
                )
            )
    return found


def reconcile(
    diagnostics: list[Diagnostic],
    manifests: dict[str, Manifest],
    in_scope: Callable[[str], bool] = lambda _: True,
) -> list[Diagnostic]:
    """The diagnostics that fail `check`.

    `in_scope(path)` says whether a file was analysed. A recorded debt about a file that was not
    analysed cannot be called paid, so a partial run leaves it alone.
    """
    groups = tally(diagnostics)
    failing = [d for d in diagnostics if d.debt is None]
    for (owner, code, key), found in groups.items():
        manifest = manifests.get(owner)
        recorded = manifest.debt.get(code, {}).get(key, 0) if manifest is not None else 0
        if len(found) > recorded:
            failing.extend(_excess(found, recorded, owner, manifests))
    for manifest in manifests.values():
        failing.extend(_paid(manifest, groups, in_scope))
    return failing


def debts_in(manifests: dict[str, Manifest]) -> int:
    """How many debts are recorded across `manifests`."""
    return sum(total(m.debt) for m in manifests.values())
