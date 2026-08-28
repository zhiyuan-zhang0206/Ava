"""Converge step for the prod source-tree guard — reset + clean at bring-up.

The repair half of the source-tree guard (2026-08-28 user ruling: the prod
tree must be kept whole — an edited tree broke ``import ava`` for every
agent); the health probe's check 8 is the read-only detector. See
``shared/source_tree_guard.py`` for the shared whitelist and primitives, and
``source-tree-guard.ava.okf.md`` for the full design.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cli.commands._converge_spec import ConvergeCtx


def reset_prod_source_tree(repo: Path | None = None) -> None:
    """Reset the prod source checkout to the installed commit and remove
    untracked files outside the runtime-artifact whitelist.

    The single repair entry point shared by the converge step and the
    pre-guard reset in ``ava start`` (Task #1905 QA finding 2: the start-time
    source-integrity guard used to adopt drift as the installed commit before
    the converge step could revert it, so the periodic reset never bit on the
    regular operational path). Skipped while a cluster update is in flight —
    a rollout legitimately owns the tree (deploy verbs are exempt; the update
    flow records ``installed_sha`` when it lands). When ``repo`` is given and
    is not the prod source checkout, nothing happens — a dev worktree start
    must not reset the prod tree (the host_global gate in converge_host
    applies the same rule to the converge path).
    """
    import shared.cluster_drift
    import shared.source_tree_guard

    source_root = shared.cluster_drift.prod_source_dir()
    if source_root is None:
        return
    if repo is not None and Path(repo).resolve() != source_root.resolve():
        return
    from cli.commands.status import _update_in_flight

    if _update_in_flight():
        print("  · source tree reset skipped: cluster update in flight", file=sys.stderr)
        return
    repair = shared.source_tree_guard.repair_source_tree(source_root)
    if repair is None:
        return
    if repair.reset_from is not None and repair.reset_to is not None:
        if repair.reset_from == repair.reset_to:
            print(
                f"  ! source tree reset: discarded tracked changes at HEAD {repair.reset_from[:7]}",
                file=sys.stderr,
            )
        else:
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


def ensure_source_tree_integrity(ctx: ConvergeCtx) -> None:
    """Converge-step wrapper around :func:`reset_prod_source_tree`.

    host_global: a dev worktree is a development context by construction
    (feature-branch HEAD, always-dirty tree) and must never be reset against
    the prod bookmark.
    """
    reset_prod_source_tree(ctx.repo)
