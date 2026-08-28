"""Converge step for the prod source-tree guard — reset + clean at bring-up.

The repair half of the source-tree guard (2026-08-28 user ruling: the prod
tree must be kept whole — an edited tree broke ``import ava`` for every
agent); the health probe's check 8 is the read-only detector. See
``shared/source_tree_guard.py`` for the shared whitelist and primitives, and
``source-tree-guard.ava.okf.md`` for the full design.
"""

from __future__ import annotations

import sys

from cli.commands._converge_spec import ConvergeCtx


def ensure_source_tree_integrity(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Reset the prod source checkout to the installed commit and remove
    untracked files outside the runtime-artifact whitelist.

    Skipped while a cluster update is in flight — a rollout legitimately owns
    the tree (deploy verbs are exempt; the update flow records
    ``installed_sha`` when it lands). host_global: a dev worktree is a
    development context by construction (feature-branch HEAD, always-dirty
    tree) and must never be reset against the prod bookmark.
    """
    import shared.cluster_drift
    import shared.source_tree_guard

    source_root = shared.cluster_drift.prod_source_dir()
    if source_root is None:
        return
    from cli.commands.status import _update_in_flight

    if _update_in_flight():
        print("  · source tree reset skipped: cluster update in flight", file=sys.stderr)
        return
    repair = shared.source_tree_guard.repair_source_tree(source_root)
    if repair is None:
        return
    if repair.reset_from is not None and repair.reset_to is not None:
        print(
            f"  ! source tree reset: HEAD {repair.reset_from[:7]} -> {repair.reset_to[:7]}",
            file=sys.stderr,
        )
    if repair.cleaned:
        print(
            f"  ! source tree cleaned {len(repair.cleaned)} untracked file(s): "
            + ", ".join(repair.cleaned),
            file=sys.stderr,
        )
    for error in repair.errors:
        print(f"  ! source tree repair failed: {error}", file=sys.stderr)
