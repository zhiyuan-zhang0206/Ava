"""Explain the same complete subject-LCA proof used by the root-test gate."""

from __future__ import annotations

import ast
from pathlib import Path

from scripts.structure import placement, placement_evidence, tests_location


def suggest(rel: str, repo_root: Path) -> str:
    """Where the test at repo-relative `rel` belongs, as one paragraph."""
    if (reason := tests_location.stays_by_design(rel)) is not None:
        return f"{rel}: stays at the top level by design ({reason})."
    tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"), filename=rel)
    result = placement_evidence.subject_lca(tree, rel, placement.ModuleIndex(repo_root))
    if result.unknown:
        details = "; ".join(f"{u.path}:{u.line}: {u.reason}" for u in result.unknown)
        return f"{rel}: incomplete subject evidence; placement is not certified: {details}."
    if result.directory is None:
        return (
            f"{rel}: references no first-party package subject; no subject proves placement. "
            "Existing repository-artifact or harness contracts remain separately registered."
        )
    shown = ", ".join(result.modules)
    if result.directory == "":
        return f"{rel}: complete subject LCA is root ({shown}); place it under tests/."
    return (
        f"{rel}: put it in {result.directory}/tests/{Path(rel).name}. "
        f"Complete subject LCA is {result.directory} ({shown})."
    )
