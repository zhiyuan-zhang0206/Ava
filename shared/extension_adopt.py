"""Adopt this machine's locally installed skills into the cluster registry.

The migration half of S2 (`future/infra/extension-ownership.md`): every machine
that installed a skill before the registry existed still holds it as a purely
local fact in `installed.json`. Nothing else in the slice looks at those — the
install path writes the cluster row going forward, and materialization only ever
reads rows — so without this, content that predates the registry stays invisible
to the cluster forever and the ownership model has a hole exactly the size of
everything installed so far.

Adoption, not a flag day: the sweep runs on every converge, is idempotent, and
never asks anyone to re-install anything.

## What it takes, and what it deliberately leaves

Only `origin='user'` skills — what a person installed here. Repo- and
plugin-origin packages are converge-managed derived state whose content comes
from the checkout; a `source='repo'` row is forbidden to carry a blob at all
(the schema's `extensions_blob_iff_installed` CHECK), and their enablement fold
is a separate step that needs the plugin and MCP kinds this slice does not have
yet.

`trust` does not travel. A local `reviewed` is a decision someone made about
THIS machine's copy (`ava skill trust`); promoting it cluster-wide because one
machine swept would make a review of one directory into a review for every
machine. Adopted rows land `unreviewed`, which is exactly what `ava skill
install` writes today, so a swept name and a freshly installed one are
indistinguishable afterwards.

`enabled` does travel, as the row's `default_enabled`. It is the only signal
available about what the operator wanted, and the alternative — adopting
everything as enabled — silently switches on something a person deliberately
turned off.

## The one case that refuses

Two machines can both hold a name. If their trees hash the same, they are the
same content by two routes and the second machine simply finds itself already
claimed — nothing to do, no report worth making.

If they differ, this is the one place in the model where two machines disagree
about what a name MEANS, and there is no evidence here for which is right.
Picking either destroys the other's work with no trace, so the sweep refuses
that name, names both machines, and leaves both copies where they are. It is
the same conservative direction `shared/extension_materialize.py` takes for a
locally edited tree, for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from psycopg_pool import ConnectionPool

from shared import extension_registry as registry
from shared.install_registry import tree_hash
from shared.log import logger


@dataclass(frozen=True)
class AdoptionConflict:
    """One name two machines disagree about, with both sides named.

    `claimed_by` is the row's `source` — `local:<machine>` for a name another
    machine swept or installed, a git URL for one that came from outside. It is
    what lets the warning say WHO to go and ask.
    """

    name: str
    local_hash: str
    cluster_hash: str
    claimed_by: str


def _no_names() -> list[str]:
    """Typed empty-list factory (same reason as the materializer's)."""
    return []


def _no_conflicts() -> list[AdoptionConflict]:
    """Typed empty-list factory for the conflict list."""
    return []


@dataclass
class AdoptionResult:
    """What one sweep did, per name."""

    adopted: list[str] = field(default_factory=_no_names)
    """Uploaded: the cluster had no row, this machine's tree became one."""
    already_claimed: list[str] = field(default_factory=_no_names)
    """A row exists and this machine's tree matches it — the ordinary steady state."""
    conflicts: list[AdoptionConflict] = field(default_factory=_no_conflicts)
    """NOT uploaded: a row exists with different content. The operator resolves."""
    missing_tree: list[str] = field(default_factory=_no_names)
    """Tracked in `installed.json` but absent from disk — a pre-existing local
    inconsistency this sweep reports rather than papers over by uploading
    nothing under that name."""


def adopt_local_installs(pool: ConnectionPool, *, skills_root: Path) -> AdoptionResult:
    """Upload every unclaimed `origin='user'` skill on this machine.

    Idempotent: a second pass finds every name already claimed with a matching
    hash and writes nothing. Safe to run on every converge, which is what makes
    this adoption rather than a one-shot migration somebody has to remember.
    """
    from shared import install_registry
    from shared.machine import machine_name

    result = AdoptionResult()
    for pkg in install_registry.load().packages:
        if pkg.type != "skill" or pkg.origin != "user":
            continue
        root = skills_root / pkg.name
        if not root.is_dir():
            result.missing_tree.append(pkg.name)
            logger.warning(
                "skill {name} is tracked in this machine's install registry but is not on "
                "disk — nothing to adopt into the cluster; reinstall it or deregister it",
                name=pkg.name,
            )
            continue
        local = tree_hash(root)
        with pool.connection() as conn:
            existing = registry.get(conn, pkg.name)
        if existing is not None:
            if existing.content_hash == local:
                result.already_claimed.append(pkg.name)
                continue
            result.conflicts.append(
                AdoptionConflict(
                    name=pkg.name,
                    local_hash=local,
                    cluster_hash=existing.content_hash or "",
                    claimed_by=existing.source,
                )
            )
            logger.warning(
                "skill {name} is claimed by {claimed_by} with different content than this "
                "machine holds ({cluster_hash} vs {local_hash}) — NOT adopting; both copies "
                "are intact, and whichever is right should be installed over the other",
                name=pkg.name,
                claimed_by=existing.source,
                cluster_hash=existing.content_hash,
                local_hash=local,
            )
            continue
        registry.register_tree(
            pool,
            root=root,
            name=pkg.name,
            kind="skill",
            source=f"local:{machine_name()}",
            default_enabled=pkg.enabled,
        )
        result.adopted.append(pkg.name)
    return result
