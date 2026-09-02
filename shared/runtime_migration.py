"""Explicit verified-candidate migration authority, never an environment bypass.

The official transition supplies a previously verified release and the expected
unit home. This context is not inferred from a moving selector or AVA_HOME.
Ordinary development migrations keep their checkout/Git ownership checks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg

from shared.cluster_lock import read_update_lease
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease, file_sha256


@dataclass(frozen=True)
class ReleaseMigrationContext:
    """A prepared generation bound to its canonical owning unit and SQL bytes."""

    release: VerifiedRelease
    home: Path
    holder: str
    acquired_at: datetime
    target_sha: str

    def assert_operation(self, conn: psycopg.Connection) -> None:
        """Reject a stale receipt, another operation, and post-rollout settle holds."""
        lease = read_update_lease(conn=conn)
        if (
            lease is None
            or lease.holder != self.holder
            or lease.acquired_at != self.acquired_at
            or lease.kind != "rollout"
            or lease.note is not None
        ):
            raise ReleaseRejectedError("release migration receipt does not own the current rollout")
        if re.fullmatch(r"[0-9a-f]{40}", self.target_sha) is None:
            raise ReleaseRejectedError("release migration requires an exact target commit")
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT target_sha FROM deployment_state WHERE id = 1 "
                "AND holder = %s AND acquired_at = %s AND phase = 'updating'",
                (self.holder, self.acquired_at),
            )
            row = cursor.fetchone()
        if row is None or row[0] != self.target_sha:
            raise ReleaseRejectedError("prepared candidate target differs from current operation")

    def validate(self, migration_dir: Path) -> dict[Path, str]:
        """Recheck SQL inventory and home binding before any database mutation.

        The release's manifest digest must originate from the prepared operation,
        not from hashing an arbitrary manifest and trusting its own claims.
        Full generation verification belongs to that transition boundary.
        """
        home = self.home
        root = self.release.root
        if not home.is_absolute() or home.resolve(strict=True) != home:
            raise ReleaseRejectedError("release migration home must be canonical and existing")
        if root != home / "releases" / self.release.digest or root.resolve(strict=True) != root:
            raise ReleaseRejectedError("candidate generation is not owned by this unit home")
        manifest_path = root / "manifest.json"
        if file_sha256(manifest_path) != self.release.manifest_digest:
            raise ReleaseRejectedError("prepared migration manifest changed")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        directory = migration_dir.resolve(strict=True)
        if directory != migration_dir or not directory.is_relative_to(root / "venv"):
            raise ReleaseRejectedError("migration package is outside the candidate venv")
        relative = directory.relative_to(root).as_posix() + "/"
        expected = {
            root / name: digest
            for name, digest in manifest["files"].items()
            if name.startswith(relative) and name.endswith(".sql")
        }
        actual = set(directory.glob("*.sql"))
        if actual != set(expected):
            raise ReleaseRejectedError("candidate SQL inventory changed")
        for path, digest in expected.items():
            if path.is_symlink() or file_sha256(path) != digest:
                raise ReleaseRejectedError("candidate SQL bytes changed")
        return expected
