"""The cluster's extension registry — which extensions exist, and their content.

Slice S2 of `future/infra/extension-ownership.md` (issue #39); the ownership
model it implements is
`decisions/2026-08-21-extension-ownership-three-tiers.md`: the **cluster** owns
content, identity and default enablement; the machine owns only capabilities;
the agent owns an activation delta.

This module is the write side and the read side of the two tables. It does not
materialize anything onto a machine — that is converge's job and lands with the
next slice, so today these rows are written and not yet read. That ordering is
deliberate (expand, then switch readers, then contract): making `ava skill
install` depend on the cluster DB before anything materializes from the rows
would buy the dependency without the payoff.

## Content addressing

A package's identity as *content* is `install_registry.tree_hash` — the same
hash converge already uses as its user-modification detector, so the registry
and the local trees speak one vocabulary and a materialized directory can be
compared to its row by hashing it, with no re-packing. `extension_blobs` is
keyed by that value and its rows are immutable: re-installing identical content
re-uses the blob, and two extensions holding identical trees share one row.

The archive is a **deterministic** tar: entries sorted by path, and every
mtime/uid/gid/mode normalized. Without that, packing the same tree twice
produces different bytes, and "the blob for this hash already exists" becomes a
coin flip — `content_hash` addresses the tree, so the bytes stored under it must
be a function of the tree alone.

## The size cap

`MAX_BLOB_BYTES` is enforced in three places that cannot disagree:

1. here, before the write, with a message naming the actual size and the cap;
2. `extension_blobs_size_cap`, a CHECK constraint — so a writer that bypasses
   this module still cannot land an oversized blob;
3. `extension_blobs_size_is_real`, which forces `size_bytes` to equal
   `octet_length(archive)` — otherwise (1) and (2) both check a number the
   writer chose rather than the bytes actually stored.

The number lives here and in the DDL; `tests/shared/test_extension_registry.py`
pins them together by writing exactly the cap and exactly one byte over. It is
a cap on *extension content*, which is source trees — large artifacts are host
provisioning and do not belong in the cluster's data plane.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg_pool import ConnectionPool

from shared.install_registry import IGNORED_NAMES, TrustTier

ExtensionKind = Literal["skill", "plugin", "mcp"]
"""What a registry row carries. Mirrors `install_registry.PackageType` — the two
will converge as the per-machine registry demotes to a cache, and until then a
name means the same kind on both sides."""

# TrustTier is re-exported from install_registry rather than redefined: the tier
# a package was ingested at is the same fact whether it is recorded in the
# per-machine registry or the cluster row, and the design has it "moved up with
# the row". Two Literals would be two things to keep equal.
__all__ = ["Extension", "ExtensionKind", "TrustTier"]

# The ceiling on one extension's archived tree. See the module docstring for why
# this is a constraint rather than a convention, and `db/schema.sql`'s
# `extension_blobs_size_cap`, which carries the same number.
MAX_BLOB_BYTES = 8 * 1024 * 1024

# The source value that means "this came from the checkout, not from an
# install". Such a row carries only `default_enabled` and MUST NOT have a blob —
# the schema enforces the iff (`extensions_blob_iff_installed`).
REPO_SOURCE = "repo"


class ExtensionTooLargeError(Exception):
    """An extension's archived tree exceeds `MAX_BLOB_BYTES`.

    Deliberately its own type rather than a ValueError: the caller's correct
    response is to tell the operator what to remove, not to retry or to fall
    back to a machine-local install — a silent local install is exactly the
    drift the registry exists to delete.
    """


@dataclass(frozen=True)
class Extension:
    """One registry row, as read back."""

    name: str
    kind: ExtensionKind
    source: str
    content_hash: str | None
    trust: TrustTier
    default_enabled: bool
    source_ref: str | None = None
    version: str | None = None
    manifest: dict[str, Any] | None = None
    installed_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_repo_source(self) -> bool:
        """True for a checkout-shipped row: enablement only, never a blob."""
        return self.source == REPO_SOURCE


def pack_tree(root: Path) -> bytes:
    """Deterministic tar of every file under `root`, `IGNORED_NAMES` excluded.

    Byte-identical for identical content: entries are emitted in sorted path
    order and every mtime, uid, gid, uname, gname and mode is normalized. The
    blob is content-addressed, so a tar that varied with the filesystem's
    timestamps would store different bytes under the same `content_hash` and
    make "does this blob already exist" answer differently on two machines.

    Directories are not emitted — `unpack_tree` creates parents as needed, and
    an empty directory carries no extension content.
    """
    files = sorted(
        f
        for f in root.rglob("*")
        if f.is_file() and not any(part in IGNORED_NAMES for part in f.relative_to(root).parts)
    )
    buf = io.BytesIO()
    # No compression: these are small text trees, and an uncompressed tar keeps
    # the bytes a pure function of the content (gzip embeds an mtime).
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for f in files:
            info = tarfile.TarInfo(name=f.relative_to(root).as_posix())
            data = f.read_bytes()
            info.size = len(data)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            # 0o644 for everything: an execute bit that varied by machine would
            # change the archive bytes for identical content.
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def unpack_tree(archive: bytes, dest: Path) -> None:
    """Extract `archive` into `dest`, refusing any entry that escapes it.

    A blob is content that arrived from outside this cluster, so the path
    traversal check is not ceremony: a `../` entry would let an install write
    outside the extension's own directory.
    """
    dest.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            target = (dest / member.name).resolve()
            if not target.is_relative_to(resolved_dest):
                raise ValueError(
                    f"extension archive entry {member.name!r} escapes the destination "
                    f"directory — refusing to extract"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            assert extracted is not None  # noqa: S101 — isfile() members always extract
            target.write_bytes(extracted.read())


def blob_hash(archive: bytes) -> str:
    """A hash of the archive BYTES — a fallback identity, not the scheme.

    `extension_blobs` is keyed by the TREE hash (`install_registry.tree_hash`),
    because that is the vocabulary the per-machine registry and converge's
    user-modification detector already speak: a materialized tree can then be
    compared to its row by hashing the directory, with no re-packing and no
    second notion of "same content". `pack_tree` is deterministic, so tree and
    archive are 1:1 either way — the choice is about which value other code can
    compute cheaply.

    Used only when a caller has bytes and no tree to hash.
    """
    return hashlib.sha256(archive).hexdigest()


def check_size(archive: bytes, *, name: str) -> None:
    """Refuse an archive over `MAX_BLOB_BYTES`, naming both numbers.

    Called before the write so the operator hears about it at install time,
    where they still have the tree in front of them, rather than as a constraint
    violation from a later transaction.
    """
    if len(archive) > MAX_BLOB_BYTES:
        raise ExtensionTooLargeError(
            f"extension {name!r} packs to {len(archive)} bytes, over the "
            f"{MAX_BLOB_BYTES}-byte cluster registry cap. Extension content is source "
            f"trees; large artifacts belong in host provisioning, not in the cluster's "
            f"data plane. Remove the large files from the package, or install them by "
            f"another route."
        )


def put_blob(
    conn: psycopg.Connection, archive: bytes, *, name: str, content_hash: str | None = None
) -> str:
    """Store `archive` under `content_hash` if absent; return the hash used.

    `content_hash` is the TREE hash (`install_registry.tree_hash`) of what the
    archive contains — the same value converge already records as its
    user-modification detector, so the cluster row and a materialized tree
    compare directly with no re-packing. `register_tree` always supplies it;
    the `blob_hash` fallback exists for a caller holding only bytes and is not
    the addressing scheme (see `blob_hash`).

    Idempotent by construction: blobs are immutable and content-addressed, so a
    second write of identical content is a no-op rather than an update.
    Re-storing is the common case — reinstalling an unchanged package, or two
    extensions with identical trees.
    """
    check_size(archive, name=name)
    digest = content_hash if content_hash is not None else blob_hash(archive)
    conn.execute(
        "INSERT INTO extension_blobs (content_hash, archive, size_bytes) "
        "VALUES (%s, %s, %s) ON CONFLICT (content_hash) DO NOTHING",
        (digest, archive, len(archive)),
    )
    return digest


def get_blob(conn: psycopg.Connection, content_hash: str) -> bytes | None:
    """The archive stored under `content_hash`, or None."""
    row = conn.execute(
        "SELECT archive FROM extension_blobs WHERE content_hash = %s", (content_hash,)
    ).fetchone()
    return None if row is None else bytes(row[0])


def upsert(
    conn: psycopg.Connection,
    *,
    name: str,
    kind: ExtensionKind,
    source: str,
    content_hash: str | None,
    trust: TrustTier = "unreviewed",
    source_ref: str | None = None,
    version: str | None = None,
    manifest: dict[str, Any] | None = None,
    default_enabled: bool | None = None,
) -> None:
    """Write or update one extension row.

    `default_enabled=None` on an update PRESERVES the stored value — that column
    is cluster POLICY, and reinstalling or upgrading a package must not silently
    re-enable something an operator turned off. On a fresh insert None means the
    schema default (enabled).

    `trust` is keyed to `content_hash` (user ruling 2026-08-21, issue #218):
    trust is a cluster-level fact about content. When the upsert CHANGES the
    content_hash, the caller's tier lands (the default `unreviewed` — a review
    never launders across versions). When the content_hash is unchanged, trust
    only ever rises: a `reviewed`/`builtin` row is never downgraded by a later
    `unreviewed` write (multi-machine convergence), while an `unreviewed` row
    accepts a `reviewed` promotion.

    `updated_at` is stamped on every write, which is what makes a materializer
    able to ask "has anything changed since I last converged".
    """
    import json

    conn.execute(
        "INSERT INTO extensions "
        "  (name, kind, source, source_ref, version, content_hash, manifest, trust, "
        "   default_enabled) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, true)) "
        "ON CONFLICT (name) DO UPDATE SET "
        "  kind = EXCLUDED.kind, source = EXCLUDED.source, source_ref = EXCLUDED.source_ref, "
        "  version = EXCLUDED.version, content_hash = EXCLUDED.content_hash, "
        "  manifest = EXCLUDED.manifest, "
        # Trust is keyed to content (user ruling 2026-08-21, issue #218): a
        # review means "I reviewed THESE BYTES". Content changed -> the caller's
        # tier lands (default unreviewed — a review never launders across
        # versions). Content unchanged -> trust only ever rises: a reviewed /
        # builtin row is never downgraded by a later unreviewed write.
        "  trust = CASE "
        "    WHEN EXCLUDED.content_hash IS DISTINCT FROM extensions.content_hash "
        "      THEN EXCLUDED.trust "
        "    WHEN extensions.trust IN ('reviewed', 'builtin') THEN extensions.trust "
        "    ELSE EXCLUDED.trust END, "
        "  default_enabled = COALESCE(%s, extensions.default_enabled), "
        "  updated_at = now()",
        (
            name,
            kind,
            source,
            source_ref,
            version,
            content_hash,
            json.dumps(manifest) if manifest is not None else None,
            trust,
            default_enabled,
            default_enabled,
        ),
    )


_SELECT = (
    "SELECT name, kind, source, content_hash, trust, default_enabled, source_ref, version, "
    "       manifest, installed_at, updated_at FROM extensions"
)


def _row_to_extension(row: tuple[Any, ...]) -> Extension:
    return Extension(
        name=row[0],
        kind=row[1],
        source=row[2],
        content_hash=row[3],
        trust=row[4],
        default_enabled=row[5],
        source_ref=row[6],
        version=row[7],
        manifest=row[8],
        installed_at=row[9],
        updated_at=row[10],
    )


def get(conn: psycopg.Connection, name: str) -> Extension | None:
    """One row by name, or None."""
    row = conn.execute(f"{_SELECT} WHERE name = %s", (name,)).fetchone()
    return None if row is None else _row_to_extension(row)


def list_enabled(conn: psycopg.Connection, *, kind: ExtensionKind | None = None) -> list[Extension]:
    """Every cluster-enabled row, optionally narrowed to one kind.

    The materialization question ("what should this machine have") in its
    simplest form — capability matching narrows it further from S4, and the
    per-agent overlay from S3. Ordered by name so a converge log and a status
    table read the same way twice.
    """
    if kind is None:
        rows = conn.execute(f"{_SELECT} WHERE default_enabled ORDER BY name").fetchall()
    else:
        rows = conn.execute(
            f"{_SELECT} WHERE default_enabled AND kind = %s ORDER BY name", (kind,)
        ).fetchall()
    return [_row_to_extension(r) for r in rows]


def set_default_enabled(conn: psycopg.Connection, name: str, *, enabled: bool) -> bool:
    """Flip one row's cluster-default enablement; False when no such row.

    The write behind `ava skill enable/disable` once those retarget from the
    per-machine file to the cluster row.
    """
    result = conn.execute(
        "UPDATE extensions SET default_enabled = %s, updated_at = now() WHERE name = %s",
        (enabled, name),
    )
    return result.rowcount > 0


def register_tree(
    pool: ConnectionPool,
    *,
    root: Path,
    name: str,
    kind: ExtensionKind,
    source: str,
    trust: TrustTier = "unreviewed",
    source_ref: str | None = None,
    version: str | None = None,
    manifest: dict[str, Any] | None = None,
    default_enabled: bool | None = None,
) -> str:
    """Pack `root`, store the blob and upsert the row — one transaction.

    The whole install-side entry point. One transaction because a row pointing
    at a blob that is not there is the state the FK exists to make impossible;
    committing them separately would leave a window where it is merely unlikely.

    `default_enabled` passes straight through to `upsert`, so None keeps a
    stored policy value on an update and means "enabled" on a fresh insert. An
    install never passes it — installing IS enabling. The adoption sweep does,
    because a name it is uploading may be one this machine has switched OFF, and
    that has to land in the same transaction as the row rather than as a second
    write somebody could observe between.

    Returns the content hash the row now points at.
    """
    from shared.install_registry import tree_hash

    archive = pack_tree(root)
    check_size(archive, name=name)
    # The TREE hash, not the archive's: this is the value a machine can recompute
    # from a materialized directory to answer "is this the content the row points
    # at", and the one converge already records per package.
    digest = tree_hash(root)
    with pool.connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ WRITE")
        put_blob(conn, archive, name=name, content_hash=digest)
        upsert(
            conn,
            name=name,
            kind=kind,
            source=source,
            content_hash=digest,
            trust=trust,
            source_ref=source_ref,
            version=version,
            manifest=manifest,
            default_enabled=default_enabled,
        )
    return digest
