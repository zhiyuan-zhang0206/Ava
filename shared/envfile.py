"""Idempotent KEY=VALUE upsert into a unit's .env, preserving unrelated lines.

Lives in `shared` (stdlib-only, no settings import) so both `cli.commands.cluster_lifecycle`
and the settings-free `cli.enroll` can use one copy.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from shared.config import cluster_tz
from shared.platform import file_lock
from shared.private_storage import ensure_private_dir, ensure_private_file, write_private_bytes

_log = logging.getLogger(__name__)

ENV_BACKUP_KEEP = 20


# How long a writer waits for another process to finish its `.env` rewrite. The
# guarded sections are single-file rewrites (milliseconds), so seconds of waiting
# already means a holder is in trouble, and an unbounded wait would spread that
# holder's trouble to every other writer.
ENV_LOCK_TIMEOUT_S = 30.0


def env_lock_path(env_path: Path) -> Path:
    """The lock file guarding a unit's `.env` — a SIBLING, never `.env` itself.

    `shared.platform.file_lock`'s POSIX branch opens its path with `"w"`, which
    truncates: pointed at the real file, taking the lock would empty a cluster's
    secrets outright.
    """
    return env_path.with_name(env_path.name + ".lock")


def snapshot_env(path: Path, *, keep: int = ENV_BACKUP_KEEP) -> Path | None:
    """Copy `path` to a timestamped backup under `<home>/backups/env/` before it is
    rewritten, so any `.env` write — including one that unsets keys — is recoverable.

    `.env` is the ONLY on-disk copy of a cluster's secrets (API keys, the cluster
    secret); a bad write that dropped them once left the running process env as the
    sole surviving copy. This keeps a rolling history so that can't happen again.

    No-op when the file is absent or blank (nothing to preserve), or byte-identical
    to the most recent snapshot (dedupe, so a burst of no-change writes doesn't
    churn). Prunes to the newest `keep` snapshots. Returns the snapshot path, or
    None when skipped. Best-effort: a backup failure is logged, never raised — it
    must not block the write it protects.
    """
    try:
        if not path.exists():
            return None
        content = path.read_text()
        if not content.strip():
            return None
        backup_dir = path.parent / "backups" / "env"
        ensure_private_dir(backup_dir)
        existing = sorted(backup_dir.glob(".env.*"))
        if existing and existing[-1].read_text() == content:
            return None
        # Local-time stamp with microseconds: filename sorts chronologically and
        # stays unique across rapid successive writes.
        dest = (
            backup_dir
            / f".env.{datetime.now().astimezone(cluster_tz()).strftime('%Y%m%d-%H%M%S-%f')}"
        )
        dest.write_text(content)
        ensure_private_file(dest)
        if keep > 0:
            for old in sorted(backup_dir.glob(".env.*"))[:-keep]:
                old.unlink(missing_ok=True)
        return dest
    except (OSError, RuntimeError):
        _log.warning("snapshot_env: could not back up %s", path, exc_info=True)
        return None


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    """Set each key in a unit's `.env`, preserving unrelated lines.

    Cross-process exclusive for the whole read-modify-write, like every other door
    onto this file (`env_lock_path`). This one is the busiest: converge runs it on
    **every `ava start`** (the redis URL, the app port, the pooler's DB URL), which
    is exactly the writer that interleaves with the gateway's config PUT and the ops
    daemon's `config_write` arm.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(env_lock_path(path), timeout_s=ENV_LOCK_TIMEOUT_S):
        snapshot_env(path)
        lines = path.read_text().splitlines() if path.exists() else []
        remaining = dict(updates)
        out: list[str] = []
        for line in lines:
            key = line.split("=", 1)[0].strip() if "=" in line else None
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
            else:
                out.append(line)
        for k, v in remaining.items():
            out.append(f"{k}={v}")
        write_private_bytes(path, ("\n".join(out) + "\n").encode())


def _chmod_private(path: Path) -> None:
    """Owner-only on a .env write — .env is the cluster's only on-disk secret
    copy, so its mode must not depend on umask (audit round-2 security P1-3:
    snapshot_env already chmods 0600, the main file did not)."""
    try:
        ensure_private_file(path)
    except (OSError, RuntimeError):
        _log.warning("could not chmod 0600 %s", path, exc_info=True)


def remove_env(path: Path, keys: set[str]) -> None:
    """Remove the named keys from a unit's .env, preserving unrelated lines.

    The counterpart of `upsert_env` for keys that must LEAVE the surface (e.g.
    the retired AVA_PGBOUNCER_PORT) — an idempotent line filter, snapshotting
    first like every other .env write. A missing key is a no-op; a missing file
    stays missing."""
    if not path.exists():
        return
    with file_lock(env_lock_path(path), timeout_s=ENV_LOCK_TIMEOUT_S):
        lines = path.read_text().splitlines()
        out = [line for line in lines if line.split("=", 1)[0].strip() not in keys]
        if len(out) == len(lines):
            return  # nothing to remove — no snapshot churn
        snapshot_env(path)
        write_private_bytes(path, ("\n".join(out) + "\n").encode())
