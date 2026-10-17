"""Idempotent KEY=VALUE upsert into a unit's .env, preserving unrelated lines.

Idempotence is byte-level: an upsert whose rendered result equals the current file
writes nothing — no snapshot, no rewrite, no audit record (task #3637: a repeated
converge, e.g. a boot-retry storm, must not manufacture `old == new` records). Any
differing byte still takes the full write path, so normalization of quoted or
oddly-spaced lines is preserved.

Lives in `base` (no settings import) so lifecycle commands and
first-start identity preparation share one implementation. `env_line_key` reads a line's key with the
same grammar the settings parser uses (`export KEY=v` sets `KEY`; #2981).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from dotenv import dotenv_values

from base.host.private_storage import ensure_private_file, write_private_bytes
from base.native_process.os_platform import file_lock
from base.paths.private_dir import ensure_private_dir

_log = logging.getLogger(__name__)

ENV_BACKUP_KEEP = 20


# How long a writer waits for another process to finish its `.env` rewrite. The
# guarded sections are single-file rewrites (milliseconds), so seconds of waiting
# already means a holder is in trouble, and an unbounded wait would spread that
# holder's trouble to every other writer.
ENV_LOCK_TIMEOUT_S = 30.0


def env_lock_path(env_path: Path) -> Path:
    """The lock file guarding a unit's `.env` — a SIBLING, never `.env` itself.

    `base.native_process.os_platform.file_lock`'s POSIX branch opens its path with `"w"`, which
    truncates: pointed at the real file, taking the lock would empty a cluster's
    secrets outright.
    """
    return env_path.with_name(env_path.name + ".lock")


_EXPORT_PREFIX = re.compile(r"export[^\S\r\n]+")
_UNQUOTED_KEY = re.compile(r"[^=\s#]+")


def env_line_key(line: str) -> str | None:
    """The key a `.env` assignment line targets, in the parser's own grammar.

    `export KEY=v` targets `KEY` — `export` plus whitespace is a prefix, never
    part of the key; `exportKEY=v` keeps its name. `'KEY'=v` is dotenv's
    single-quoted-key form; `"KEY"=v` keeps its quotes, as the parser leaves
    them. Comments, blank lines, and lines that carry no assignment (`KEY`, or
    junk after the key) return `None` — a bare key binds no value, and trailing
    junk makes the parser drop the binding wholesale.

    Only the KEY is read here, never the value: a line whose value does not
    decode still reports its key, so a reader can tell "not set" from "set to
    something the parser cannot read" and never silently ignores the latter
    (#2981).
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    prefix = _EXPORT_PREFIX.match(stripped)
    if prefix is not None:
        stripped = stripped[prefix.end() :]
    if stripped.startswith("'"):
        end = stripped.find("'", 1)
        if end <= 1:
            return None
        key, rest = stripped[1:end], stripped[end + 1 :]
    else:
        unquoted = _UNQUOTED_KEY.match(stripped)
        if unquoted is None:
            return None
        key, rest = unquoted.group(0), stripped[unquoted.end() :]
    rest = rest.lstrip()
    if not rest.startswith("="):
        return None
    return key


def env_line_export_prefix(line: str) -> str:
    """The normalized `export ` prefix a rewrite of `line` must keep, else `""`.

    Purely textual, and only meaningful for a line whose key was matched (the
    writer is about to replace it): a shell consumer treats `export KEY=v` and
    `KEY=v` differently, so a rewrite keeps the operator's prefix instead of
    silently dropping it. Whitespace variants normalize to one space; `exportKEY=`
    and `export=1` carry no prefix.
    """
    return "export " if _EXPORT_PREFIX.match(line.lstrip()) else ""


def capture_env_bytes(path: Path) -> bytes:
    """Return one exact `.env` image under the lock shared by every writer."""

    with file_lock(env_lock_path(path), timeout_s=ENV_LOCK_TIMEOUT_S):
        return path.read_bytes() if path.exists() else b""


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
        # Backups are local filesystem metadata: the stamp is the HOST clock,
        # not the cluster's (docs/decisions/data/backup/2026-09-28-env-backup-names-use-the-host-clock.md
        # — the ruling exception to the 2026-08-27 one-cluster-clock rule). This
        # writer also runs before first-start identity, so it must not load
        # runtime Settings to find a cluster clock in the first place.
        dest = backup_dir / f".env.{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')}"
        dest.write_text(content)
        ensure_private_file(dest)
        if keep > 0:
            for old in sorted(backup_dir.glob(".env.*"))[:-keep]:
                old.unlink(missing_ok=True)
        return dest
    except (OSError, RuntimeError):
        _log.warning("snapshot_env: could not back up %s", path, exc_info=True)
        return None


def unit_home() -> Path:
    """$MEIVA_HOME directory, created if missing.

    Routed through `base.paths.home.resolve_meiva_home()`: callers run before
    `load_meiva_env` pins `MEIVA_HOME` into the environment (the Settings-lite
    maintenance commands defer it — see `base.host.env.bootstrap._serve_flag`)."""
    from base.paths.home import resolve_meiva_home

    root = resolve_meiva_home()
    root.mkdir(parents=True, exist_ok=True)
    return root


def env_file_path() -> Path:
    """This unit's `.env` — the single config source of truth."""
    return unit_home() / ".env"


def read_env_aliases() -> dict[str, str]:
    """Raw `{ALIAS: value}` currently set in this unit's `.env` file.

    Reads the FILE (not `os.environ`), so a value written since process start is
    reflected — the gateway can serve a freshly-edited value without restarting
    itself. Absent file -> `{}`. A bare key with no value is dropped.
    """
    path = env_file_path()
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


class EnvAudit(Protocol):
    """Records one official `.env` write; implemented above host (`ops.env_audit`).

    A writer calls `prepare` while it still holds the old bytes and `record` after
    its write landed, both under the `.env` lock. `prepared` is whatever `prepare`
    returned: the implementation decides what survives redaction.
    """

    def prepare(self, changes: Sequence[Mapping[str, object]]) -> object: ...

    def record(
        self, path: Path, written: set[str], removed: set[str], prepared: object
    ) -> None: ...


def env_values_from_text(text: str) -> dict[str, str]:
    """Parse `key=value` text into `{alias: value}` for an audit value diff.

    Surrounding matching quotes are stripped, matching how the write paths
    render values. This feeds ONLY the local audit record: the audit drops the
    values of sensitive and unregistered aliases before persisting.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        key = env_line_key(line)
        if key is None:
            continue
        _, _, raw = line.partition("=")
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1]
        values[key] = raw
    return values


def upsert_env(
    path: Path,
    updates: dict[str, str],
    *,
    audit: EnvAudit | None = None,
) -> None:
    """Set each key in a unit's `.env`, preserving unrelated lines.

    A byte-identical result is not a write: when every line this call would render
    matches the file on disk, the call returns having changed nothing — no snapshot,
    no rewrite, no audit record. Converge runs this on every `meiva start`, and a
    boot-retry storm re-runs that start per attempt; without the skip, each retry
    manufactured an `old == new` audit record (task #3637).
    The skip compares rendered BYTES, not decoded values: a quoted or oddly-spaced
    line still takes the normal write path and is normalized by that write.

    Cross-process exclusive for the whole read-modify-write, like every other door
    onto this file (`env_lock_path`). This one is the busiest: converge runs it on
    **every `meiva start`** (the redis URL, the app port, the pooler's DB URL), which
    is exactly the writer that interleaves with the gateway's config PUT and the ops
    daemon's `config_write` arm.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(env_lock_path(path), timeout_s=ENV_LOCK_TIMEOUT_S):
        lines = path.read_text().splitlines() if path.exists() else []
        remaining = dict(updates)
        out: list[str] = []
        for line in lines:
            key = env_line_key(line)
            if key is not None and key in remaining:
                out.append(f"{env_line_export_prefix(line)}{key}={remaining.pop(key)}")
            else:
                out.append(line)
        for k, v in remaining.items():
            out.append(f"{k}={v}")
        payload = ("\n".join(out) + "\n").encode()
        # Skip-when-unchanged (task #3637): the bytes this call would write are the
        # bytes already on disk, so the correct write is no write — no snapshot, no
        # rewrite, no audit record. Decided under the same lock that would perform
        # the write, so no other writer can race the comparison.
        if path.exists() and path.read_bytes() == payload:
            return
        prepared = None
        if audit is not None:
            before = env_values_from_text("\n".join(lines))
            changes = [
                {"alias": key, "old": before.get(key), "new": value}
                for key, value in updates.items()
            ]
            changes.sort(key=lambda change: str(change["alias"]))
            prepared = audit.prepare(changes)
        snapshot_env(path)
        write_private_bytes(path, payload)
        if audit is not None:
            audit.record(path, set(updates), set(), prepared)


def remove_env(
    path: Path,
    keys: set[str],
    *,
    audit: EnvAudit | None = None,
) -> None:
    """Remove the named keys from a unit's .env, preserving unrelated lines.

    The counterpart of `upsert_env` for keys that must LEAVE the surface (e.g.
    a key whose declaration was removed) — an idempotent line filter,
    snapshotting first like every other .env write. A missing key is a no-op; a missing file
    stays missing."""
    if not path.exists():
        return
    with file_lock(env_lock_path(path), timeout_s=ENV_LOCK_TIMEOUT_S):
        content = path.read_text()
        lines = content.splitlines()
        out = [line for line in lines if env_line_key(line) not in keys]
        if len(out) == len(lines):
            return  # nothing to remove — no snapshot churn
        prepared = None
        if audit is not None:
            before = env_values_from_text("\n".join(lines))
            changes = [
                {"alias": key, "old": before.get(key), "new": None}
                for key in sorted(keys)
                if key in before
            ]
            prepared = audit.prepare(changes)
        snapshot_env(path)
        write_private_bytes(path, ("\n".join(out) + "\n").encode())
        if audit is not None:
            audit.record(path, set(), keys, prepared)
