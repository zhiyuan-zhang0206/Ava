"""Where a top-level test belongs, for `tests_location.py --suggest` (never run by a hook).

The tests-location checks decide from a test's path alone. Naming the package a test should move
to needs the placement rule (`scripts/structure/placement.py`): a module index, the production
import graph and, for a test that spans units, the import direction between them. That costs 10 ms
to 2.7 s per file (a cold `.cache/structure` or a test that spans units that the import-linter
contracts are silent about), so the hooks print this command instead of running it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from scripts.structure import placement, tests_location


def suggest(rel: str, repo_root: Path) -> str:
    """Where the test at repo-relative `rel` belongs, as one paragraph."""
    if (reason := tests_location.stays_by_design(rel)) is not None:
        return f"{rel}: stays at the top level by design ({reason})."
    tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"), filename=rel)
    nodes = list(ast.walk(tree))
    index = placement.ModuleIndex(repo_root)
    result = placement.place(rel, tree, index, nodes)
    refs, _ = placement.placement_references(tree, index, nodes, rel)
    basis = [ref for ref in refs if ref.kind in placement.STRONG_KINDS] or refs
    if result.home is None:
        return (
            f"{rel}: references no first-party package, so no package is its home. If it checks a "
            "repository artifact or the test harness itself, register it as `contract`; "
            "otherwise decide which package its subject is and place it there."
        )
    name = Path(rel).name
    if result.ambiguous:
        units = sorted({ref.unit for ref in basis})
        return (
            f"{rel}: no package can hold this test: it uses {', '.join(units)}, and none of "
            "these units may import all the others. Split the file by unit (each part goes to its "
            "own package), or register it as `integration` with the reason."
        )
    modules = sorted({ref.module for ref in basis if ref.unit == result.unit})
    shown = ", ".join(modules[:4]) + (f" and {len(modules) - 4} more" if len(modules) > 4 else "")
    return (
        f"{rel}: put it in {result.home}/tests/{name}. It references {shown} (unit "
        f"{result.unit}); any package above {result.home} inside {placement.unit_root(result.unit or '')} "
        "that holds what it uses is also legal. If it cannot live in a package, register it as "
        "`contract` or `integration`."
    )
