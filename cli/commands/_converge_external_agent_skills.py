"""Publish Ava's operator skill into already-present external agent homes.

The external homes remain user-owned.  Ava's authority is bound by a private
ledger under ``AVA_HOME`` and every mutation is serialized and limited to one
named skill target (plus transaction siblings bearing an Ava generation ID).
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
import uuid
from pathlib import Path
from typing import Any, cast

from cli.commands._converge_spec import ConvergeCtx
from cli.commands._external_agent_skill_fs import (
    _ClientConflictError,
    _copy_source_contents,
    _exists,
    _lstat,
    _manifest_digest,
    _remove_manifest_subset,
    _rename_no_replace,
    _source_lstat,
    _SourceIntegrityError,
    _tree_digest,
    _tree_manifest,
    _write_new,
)
from cli.commands._external_agent_skill_ledger import (
    _FORMAT,
    _load_ledger,
    _parse_record,
    _write_ledger,
)
from shared.platform import LockTimeoutError, file_lock
from shared.private_storage import ensure_private_dir

_SKILL_NAME = "operating-ava-cluster"
_MARKER_NAME = ".ava-managed.json"
_CLIENTS = (("Codex", ".codex", "codex"), ("Claude Code", ".claude", "claude"))


def _marker(installation_id: str, generation_id: str, source_digest: str) -> bytes:
    return (
        json.dumps(
            {
                "format": _FORMAT,
                "generation_id": generation_id,
                "installation_id": installation_id,
                "owner": "ava",
                "skill": _SKILL_NAME,
                "source_digest": source_digest,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _stage_manifest(source_manifest: list[dict[str, Any]], marker: bytes) -> list[dict[str, Any]]:
    return sorted(
        [
            *source_manifest,
            {
                "kind": "file",
                "mode": 0o600,
                "path": _MARKER_NAME,
                "sha256": hashlib.sha256(marker).hexdigest(),
            },
        ],
        key=lambda item: str(item["path"]),
    )


def _transaction_path(skills_root: Path, kind: str, generation_id: str) -> Path:
    return skills_root / f".{_SKILL_NAME}.ava-{kind}-{generation_id}"


def _verify_marker(root: Path, installation_id: str, generation_id: str) -> None:
    if not stat.S_ISDIR(_lstat(root).st_mode):
        raise _ClientConflictError("Ava-managed target is not a regular directory")
    record = _parse_record(root / _MARKER_NAME)
    if (
        record is None
        or record.get("installation_id") != installation_id
        or record.get("generation_id") != generation_id
    ):
        raise _ClientConflictError("Ava-managed target ownership marker does not match its ledger")


def _require_digest(root: Path, expected: str, reason: str) -> None:
    if _tree_digest(root) != expected:
        raise _ClientConflictError(reason)


def _validate_lock(path: Path) -> None:
    if _exists(path) and not stat.S_ISREG(_lstat(path).st_mode):
        raise _ClientConflictError("ownership lock is not a regular file")


def _validate_directory(path: Path, reason: str) -> None:
    if not stat.S_ISDIR(_lstat(path).st_mode):
        raise _ClientConflictError(reason)


def _remove_owned_tree(root: Path, manifest: list[dict[str, Any]]) -> None:
    _remove_manifest_subset(root, manifest)


def _warn(label: str, reason: str) -> None:
    print(f"  ! {label} external operator skill skipped: conflict: {reason}", file=sys.stderr)


def _cleanup_garbage(
    ledger_path: Path, ledger: dict[str, Any], skills_root: Path, label: str
) -> None:
    remaining: list[dict[str, Any]] = []
    for item in ledger["garbage"]:
        path = _transaction_path(skills_root, item["kind"], item["path_generation_id"])
        if not _exists(path):
            continue
        try:
            _remove_owned_tree(path, item["manifest"])
        except (OSError, _ClientConflictError) as exc:
            remaining.append(item)
            _warn(label, f"transaction cleanup conflict ({type(exc).__name__})")
    if remaining != ledger["garbage"]:
        ledger["garbage"] = remaining
        _write_ledger(ledger_path, ledger)


def _stage_copy(
    source: Path,
    source_manifest: list[dict[str, Any]],
    skills_root: Path,
    ledger_path: Path,
    ledger: dict[str, Any],
    source_digest: str,
) -> Path:
    generation_id = uuid.uuid4().hex
    marker = _marker(ledger["installation_id"], generation_id, source_digest)
    expected_manifest = _stage_manifest(source_manifest, marker)
    transaction = {
        "copy_complete": False,
        "expected_digest": _manifest_digest(expected_manifest),
        "expected_manifest": expected_manifest,
        "generation_id": generation_id,
        "previous_claimed": False,
        "source_digest": source_digest,
        "stage_created": False,
    }
    ledger["transaction"] = transaction
    _write_ledger(ledger_path, ledger)
    stage = _transaction_path(skills_root, "stage", generation_id)
    stage.mkdir(mode=0o700)
    transaction["stage_created"] = True
    _write_ledger(ledger_path, ledger)
    _write_new(stage / _MARKER_NAME, marker, 0o600)
    _copy_source_contents(source, stage)
    if _tree_manifest(stage) != expected_manifest:
        raise _SourceIntegrityError("operator skill source copy did not verify")
    transaction["copy_complete"] = True
    _write_ledger(ledger_path, ledger)
    return stage


def _queue_garbage(
    ledger: dict[str, Any],
    *,
    kind: str,
    path_generation_id: str,
    manifest: list[dict[str, Any]],
) -> None:
    record = {
        "kind": kind,
        "manifest": manifest,
        "path_generation_id": path_generation_id,
    }
    if record not in ledger["garbage"]:
        ledger["garbage"].append(record)


def _abandon_transaction(
    ledger_path: Path,
    ledger: dict[str, Any],
    skills_root: Path,
) -> bool:
    """Move every remaining residue pointer to durable cleanup state."""
    transaction = cast(dict[str, Any] | None, ledger["transaction"])
    if transaction is None:
        return True
    generation_id = transaction["generation_id"]
    stage = _transaction_path(skills_root, "stage", generation_id)
    if transaction["previous_claimed"]:
        return False
    if transaction["stage_created"] and _exists(stage):
        _queue_garbage(
            ledger,
            kind="stage",
            path_generation_id=generation_id,
            manifest=transaction["expected_manifest"],
        )
    ledger["transaction"] = None
    _write_ledger(ledger_path, ledger)
    return True


def _restore_claimed_previous(
    ledger_path: Path,
    ledger: dict[str, Any],
    transaction: dict[str, Any],
    previous: Path,
    target: Path,
) -> None:
    """Restore a claimed target without replacing a late user destination."""
    if not transaction["previous_claimed"]:
        return
    old = ledger["installed"]
    if old is None:
        raise _ClientConflictError("claimed target has no installed ownership record")
    if not _exists(previous):
        if not _exists(target):
            raise _ClientConflictError("claimed target and prior copy are both missing")
        _verify_marker(target, ledger["installation_id"], old["generation_id"])
        _require_digest(target, old["digest"], "restored managed target was modified")
    else:
        _rename_no_replace(previous, target)
    transaction["previous_claimed"] = False
    _write_ledger(ledger_path, ledger)


def _commit_activation(
    ledger_path: Path,
    ledger: dict[str, Any],
    transaction: dict[str, Any],
    previous: Path,
) -> None:
    old = ledger["installed"]
    ledger["installed"] = {
        "digest": transaction["expected_digest"],
        "generation_id": transaction["generation_id"],
        "manifest": transaction["expected_manifest"],
        "source_digest": transaction["source_digest"],
    }
    ledger["transaction"] = None
    if _exists(previous) and old is not None:
        _queue_garbage(
            ledger,
            kind="previous",
            path_generation_id=transaction["generation_id"],
            manifest=old["manifest"],
        )
    _write_ledger(ledger_path, ledger)


def _activate(
    ledger_path: Path,
    ledger: dict[str, Any],
    skills_root: Path,
    target: Path,
) -> str:
    transaction = cast(dict[str, Any], ledger["transaction"])
    generation_id = transaction["generation_id"]
    stage = _transaction_path(skills_root, "stage", generation_id)
    previous = _transaction_path(skills_root, "previous", generation_id)
    if not transaction["stage_created"] or not transaction["copy_complete"] or not _exists(stage):
        raise _ClientConflictError("incomplete Ava transaction was preserved")
    _verify_marker(stage, ledger["installation_id"], generation_id)
    if _tree_manifest(stage) != transaction["expected_manifest"]:
        raise _ClientConflictError("staged Ava transaction was modified")
    old = ledger["installed"]
    action = "installed" if old is None else "updated"
    if _exists(target):
        if old is None:
            raise _ClientConflictError("unmanaged target appeared during installation")
        if _exists(previous):
            raise _ClientConflictError("prior transaction path already exists")
        _rename_no_replace(target, previous)
        transaction["previous_claimed"] = True
        try:
            _write_ledger(ledger_path, ledger)
            _verify_marker(previous, ledger["installation_id"], old["generation_id"])
            _require_digest(
                previous, old["digest"], "managed target changed before it could be claimed"
            )
        except (OSError, _ClientConflictError):
            _restore_claimed_previous(ledger_path, ledger, transaction, previous, target)
            raise
    if _exists(target):
        raise _ClientConflictError("a target appeared after the managed copy was claimed")
    try:
        _rename_no_replace(stage, target)
    except OSError:
        _restore_claimed_previous(ledger_path, ledger, transaction, previous, target)
        raise
    _verify_marker(target, ledger["installation_id"], generation_id)
    if _tree_digest(target) != transaction["expected_digest"]:
        raise _ClientConflictError("activated Ava target failed verification")
    _commit_activation(ledger_path, ledger, transaction, previous)
    return action


def _recover(
    ledger_path: Path,
    ledger: dict[str, Any],
    skills_root: Path,
    target: Path,
) -> str | None:
    transaction = ledger["transaction"]
    if transaction is None:
        return None
    stage = _transaction_path(skills_root, "stage", transaction["generation_id"])
    previous = _transaction_path(skills_root, "previous", transaction["generation_id"])
    if (
        transaction["stage_created"]
        and transaction["copy_complete"]
        and _exists(target)
        and not _exists(stage)
    ):
        _verify_marker(target, ledger["installation_id"], transaction["generation_id"])
        _require_digest(
            target, transaction["expected_digest"], "activated transaction was modified"
        )
        action = "installed" if ledger["installed"] is None else "updated"
        _commit_activation(ledger_path, ledger, transaction, previous)
        return action
    if transaction["previous_claimed"]:
        if _exists(previous) and _exists(target):
            raise _ClientConflictError("late target prevents restoration of claimed copy")
        _restore_claimed_previous(ledger_path, ledger, transaction, previous, target)
    if transaction["stage_created"] and transaction["copy_complete"] and _exists(stage):
        return _activate(ledger_path, ledger, skills_root, target)
    if _abandon_transaction(ledger_path, ledger, skills_root):
        return None
    raise _ClientConflictError("incomplete Ava transaction still owns a claimed target")


def _validate_source_path(repo: Path, source: Path) -> None:
    current = repo
    repo_stat = _source_lstat(current)
    if not stat.S_ISDIR(repo_stat.st_mode):
        raise _SourceIntegrityError("operator skill repository root is not a directory")
    for part in source.relative_to(repo).parts:
        current /= part
        current_stat = _source_lstat(current)
        if not stat.S_ISDIR(current_stat.st_mode):
            raise _SourceIntegrityError("operator skill source path is not a directory")


def _ensure_ledger_root(ctx: ConvergeCtx) -> Path:
    for path in (ctx.ava_home, ctx.ava_home / "configs"):
        if not stat.S_ISDIR(_lstat(path).st_mode):
            raise _ClientConflictError("private ownership ledger parent is not a directory")
    root = ctx.ava_home / "configs" / "external-agent-skills"
    ensure_private_dir(root)
    return root


def _converge_locked(
    source: Path,
    source_manifest: list[dict[str, Any]],
    source_digest: str,
    client_home: Path,
    client_key: str,
    label: str,
    ledger_path: Path,
) -> None:
    home_stat = _lstat(client_home)
    if not stat.S_ISDIR(home_stat.st_mode):
        raise _ClientConflictError("client home is not a regular directory")
    skills_root = client_home / "skills"
    if not _exists(skills_root):
        skills_root.mkdir()
    if not stat.S_ISDIR(_lstat(skills_root).st_mode):
        raise _ClientConflictError("skills root is not a regular directory")
    target = skills_root / _SKILL_NAME
    ledger = _load_ledger(ledger_path, client_key)
    if ledger is None:
        if _exists(target):
            raise _ClientConflictError("unmanaged target was preserved")
        ledger = cast(
            dict[str, Any],
            {
                "client": client_key,
                "format": _FORMAT,
                "garbage": [],
                "installation_id": uuid.uuid4().hex,
                "installed": None,
                "transaction": None,
            },
        )
        _write_ledger(ledger_path, ledger)
    _cleanup_garbage(ledger_path, ledger, skills_root, label)
    recovered = _recover(ledger_path, ledger, skills_root, target)
    if recovered is not None:
        print(f"  · {label} external operator skill {recovered}: skills/{_SKILL_NAME}")
        _cleanup_garbage(ledger_path, ledger, skills_root, label)
        return
    _cleanup_garbage(ledger_path, ledger, skills_root, label)
    installed = ledger["installed"]
    if installed is not None:
        if not _exists(target):
            raise _ClientConflictError("managed target is missing")
        _verify_marker(target, ledger["installation_id"], installed["generation_id"])
        if _tree_digest(target) != installed["digest"]:
            raise _ClientConflictError("user-modified managed target was preserved")
        if installed["source_digest"] == source_digest:
            return
    elif _exists(target):
        raise _ClientConflictError("unmanaged target was preserved")
    try:
        _stage_copy(source, source_manifest, skills_root, ledger_path, ledger, source_digest)
    except (OSError, _SourceIntegrityError):
        if _abandon_transaction(ledger_path, ledger, skills_root):
            _cleanup_garbage(ledger_path, ledger, skills_root, label)
        raise
    try:
        action = _activate(ledger_path, ledger, skills_root, target)
    except (OSError, _ClientConflictError):
        if _abandon_transaction(ledger_path, ledger, skills_root):
            _cleanup_garbage(ledger_path, ledger, skills_root, label)
        raise
    print(f"  · {label} external operator skill {action}: skills/{_SKILL_NAME}")
    _cleanup_garbage(ledger_path, ledger, skills_root, label)


def converge_external_agent_skill(ctx: ConvergeCtx, *, host_home: Path | None = None) -> None:
    """Copy one operator skill into present Codex and Claude Code homes."""
    home = Path.home() if host_home is None else host_home
    try:
        _validate_directory(home, "host home is not a regular directory")
    except (OSError, _ClientConflictError) as exc:
        for label, _, _ in _CLIENTS:
            _warn(label, f"host home unavailable ({type(exc).__name__})")
        return
    present = [client for client in _CLIENTS if _exists(home / client[1])]
    if not present:
        return
    source = ctx.repo / ".agents" / "skills" / _SKILL_NAME
    _validate_source_path(ctx.repo, source)
    source_manifest = _tree_manifest(source, source=True)
    source_digest = _manifest_digest(source_manifest)
    try:
        ledger_root = _ensure_ledger_root(ctx)
    except (OSError, RuntimeError) as exc:
        for label, _, _ in present:
            _warn(label, f"private ownership ledger unavailable ({type(exc).__name__})")
        return
    for label, home_name, client_key in present:
        ledger_path = ledger_root / f"{client_key}.json"
        lock_path = ledger_root / f"{client_key}.lock"
        try:
            _validate_lock(lock_path)
            with file_lock(lock_path, timeout_s=2):
                _converge_locked(
                    source,
                    source_manifest,
                    source_digest,
                    home / home_name,
                    client_key,
                    label,
                    ledger_path,
                )
        except _ClientConflictError as exc:
            _warn(label, str(exc))
        except (LockTimeoutError, OSError) as exc:
            _warn(label, f"conflict ({type(exc).__name__})")
