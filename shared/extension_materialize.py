"""Land the cluster's extensions onto this machine.

The read side of `shared/extension_registry.py`, and the step that makes a
machine fungible: whatever the cluster says should be here is here, and a
machine that was down during an install catches up the next time anything on it
starts. Slice S2 of `future/infra/extension-ownership.md`; skills only for now,
which is the whole point of doing skills first — pure text, no runtime, no host
requirements, so the row -> blob -> tree chain is exercised end to end at
minimum risk.

## What it will and will not overwrite

Three states, and the third is the one that matters:

- **absent** — extract the blob. The ordinary catch-up.
- **present and matching** — nothing to do. `content_hash` is the tree hash, so
  this is one `tree_hash()` of a small text directory, not a re-pack.
- **present and different** — either the ROW moved (someone installed a new
  version elsewhere) or a person edited this machine's copy. Those need opposite
  handling, and the per-machine registry already records which: it stores the
  hash of what was last written here. Matching that means the local tree is
  untouched since we wrote it, so the difference is the row moving -> re-extract.
  Not matching it means a local edit -> **refuse and say so**.

Refusing is the conservative direction on purpose. An extension is content
someone may have been iterating on in place (the design's L3 develop-a-plugin
loop lives exactly here), and silently reverting an edit to match a cluster row
destroys work with no trace. A loud skip leaves both copies intact and makes the
operator choose.

## What it does not do yet

No `plugins_config.json` / `mcp_enabled.json` rewrite (those demote to caches
with the plugin and MCP kinds), no capability matching (S4 — every skill matches
every machine, since skills declare no host requirements), no per-agent overlay
(S3), and no removal of a locally-installed skill whose row was deleted. That
last one is deliberate: deletion semantics interact with the adoption sweep,
which lands with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from shared import extension_registry as registry
from shared.install_registry import tree_hash
from shared.log import logger


def _no_names() -> list[str]:
    """Typed empty-list factory for the result fields.

    `field(default_factory=_no_names)` gives pyright `list[Unknown]` under the strict
    rules this package is held to; a named factory states the element type once
    instead of five `cast`s.
    """
    return []


@dataclass
class MaterializeResult:
    """What one materialization pass did, per extension name.

    Separate lists rather than a count because the caller prints them and an
    operator reads them: "updated: x" and "kept your edits to: x" are different
    news, and the second is the one that needs acting on.
    """

    landed: list[str] = field(default_factory=_no_names)
    """Extracted because the machine did not have them."""
    updated: list[str] = field(default_factory=_no_names)
    """Re-extracted because the cluster row moved and the local copy was untouched."""
    unchanged: list[str] = field(default_factory=_no_names)
    """Already matching the row."""
    kept_local_edits: list[str] = field(default_factory=_no_names)
    """NOT overwritten: the local tree differs from the row AND from what was
    last written here, so a person changed it. The operator resolves."""
    missing_blob: list[str] = field(default_factory=_no_names)
    """Row present, blob absent — a broken registry state the FK should prevent.
    Reported rather than raised so one bad row cannot block every other skill."""

    @property
    def changed(self) -> bool:
        return bool(self.landed or self.updated)


def _last_written_hash(name: str) -> str | None:
    """The tree hash this machine last wrote for `name`, from the per-machine
    install registry — the "has a person touched it since" reference.

    None when the package is not tracked locally (never installed here, or
    installed before the registry recorded hashes), which the caller reads as
    "no evidence it is untouched" and therefore refuses to overwrite.
    """
    from shared import install_registry

    pkg = install_registry.get(name)
    return None if pkg is None else pkg.content_hash


def materialize_skills(
    conn: psycopg.Connection, *, dest_root: Path, dry_run: bool = False
) -> MaterializeResult:
    """Bring `dest_root` in line with the cluster's enabled `kind='skill'` rows.

    `dest_root` is the machine's skills load directory. Repo-shipped skills are
    NOT handled here — a `source='repo'` row carries no blob and keeps
    converging from the checkout, which the schema enforces — so this only ever
    touches names that arrived by install.

    `dry_run` computes the verdicts without writing, for a status view.
    """
    result = MaterializeResult()
    for ext in registry.list_enabled(conn, kind="skill"):
        if ext.is_repo_source:
            continue
        if ext.content_hash is None:  # pragma: no cover — the schema forbids it
            continue
        dest = dest_root / ext.name
        verdict = _classify(ext.name, ext.content_hash, dest)
        if verdict == "unchanged":
            result.unchanged.append(ext.name)
            continue
        if verdict == "local-edit":
            result.kept_local_edits.append(ext.name)
            logger.warning(
                "skill {name} differs from the cluster registry and from what this machine "
                "last wrote — keeping the local copy; resolve by reinstalling or by "
                "installing your edit",
                name=ext.name,
            )
            continue
        archive = registry.get_blob(conn, ext.content_hash)
        if archive is None:
            result.missing_blob.append(ext.name)
            logger.error(
                "skill {name} points at content_hash {digest} with no blob — the row cannot "
                "be materialized on any machine",
                name=ext.name,
                digest=ext.content_hash,
            )
            continue
        if not dry_run:
            _replace_tree(archive, dest)
        (result.landed if verdict == "absent" else result.updated).append(ext.name)
    return result


def _classify(name: str, row_hash: str, dest: Path) -> str:
    """`absent` | `unchanged` | `stale` | `local-edit` for one extension.

    Split out so the decision is testable without a filesystem write and
    readable without the extraction noise around it.
    """
    if not dest.exists():
        return "absent"
    local = tree_hash(dest)
    if local == row_hash:
        return "unchanged"
    # The row and the disk disagree. Only the per-machine record of what WE last
    # wrote can say whether the disk moved or the row did.
    return "stale" if _last_written_hash(name) == local else "local-edit"


def _replace_tree(archive: bytes, dest: Path) -> None:
    """Extract `archive` over `dest`, staging first so a failure cannot leave a
    half-written tree where a skill used to be.

    The staging directory is a sibling, not a temp dir elsewhere, so the final
    move is a rename within one filesystem — atomic, and not a copy that could
    fail halfway.
    """
    import shutil

    staging = dest.with_name(f".{dest.name}.incoming")
    previous = dest.with_name(f".{dest.name}.previous")
    for leftover in (staging, previous):
        if leftover.exists():
            shutil.rmtree(leftover)
    registry.unpack_tree(archive, staging)
    if dest.exists():
        dest.rename(previous)
    try:
        staging.rename(dest)
    except OSError:
        # Put back what was there rather than leaving the name empty.
        if previous.exists() and not dest.exists():
            previous.rename(dest)
        raise
    finally:
        if previous.exists():
            shutil.rmtree(previous)
