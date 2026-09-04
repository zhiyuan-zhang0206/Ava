"""Publish Ava's operator skill into already-present external agent homes.

The external homes remain user-owned.  Ava's authority is bound by a private
ledger under ``AVA_HOME`` and every mutation is serialized and limited to one
named skill target (plus transaction siblings bearing an Ava generation ID).
"""

from __future__ import annotations

import stat
import sys
import uuid
from pathlib import Path
from typing import Any, cast

from cli.commands._converge_spec import ConvergeCtx
from cli.commands._external_agent_skill_cleanup import (
    _cleanup_garbage_impl,
    _queue_garbage,
    _transaction_path,
)
from cli.commands._external_agent_skill_fs import (
    _ClientConflictError,
    _exists,
    _lstat,
    _manifest_digest,
    _materialize_source_snapshot,
    _rename_no_replace,
    _source_lstat,
    _source_snapshot,
    _SourceIntegrityError,
    _SourceSnapshot,
    _tree_digest,
    _tree_manifest,
    _write_new,
)
from cli.commands._external_agent_skill_ledger import (
    _FORMAT,
    _load_ledger,
    _ownership_marker,
    _parse_record,
    _stage_manifest,
    _write_ledger,
)
from shared.platform import LockTimeoutError, file_lock
from shared.private_storage import ensure_private_dir

_SKILL_NAME = "operating-ava-cluster"
_MARKER_NAME = ".ava-managed.json"
_CLIENTS = (("Codex", ".codex", "codex"), ("Claude Code", ".claude", "claude"))


def _prepared_stage_path(ledger_path: Path, generation_id: str) -> Path:
    return ledger_path.parent / f".{ledger_path.stem}-stage-{generation_id}"


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


def _warn(label: str, reason: str) -> None:
    print(f"  ! {label} external operator skill skipped: conflict: {reason}", file=sys.stderr)


def _cleanup_garbage(
    ledger_path: Path, ledger: dict[str, Any], skills_root: Path, label: str
) -> None:
    _cleanup_garbage_impl(ledger_path, ledger, skills_root, label, _rename_no_replace)


def _stage_copy(
    snapshot: _SourceSnapshot,
    source_manifest: list[dict[str, Any]],
    skills_root: Path,
    ledger_path: Path,
    ledger: dict[str, Any],
    source_digest: str,
) -> Path:
    generation_id = uuid.uuid4().hex
    marker = _ownership_marker(ledger["installation_id"], generation_id, source_digest)
    expected_manifest = _stage_manifest(source_manifest, marker)
    transaction = {
        "claim_state": "idle",
        "expected_digest": _manifest_digest(expected_manifest),
        "expected_manifest": expected_manifest,
        "generation_id": generation_id,
        "source_digest": source_digest,
        "stage_state": "preparing",
    }
    ledger["transaction"] = transaction
    _write_ledger(ledger_path, ledger)
    prepared = _prepared_stage_path(ledger_path, generation_id)
    prepared.mkdir(mode=0o700)
    _write_new(prepared / _MARKER_NAME, marker, 0o600)
    _materialize_source_snapshot(snapshot, prepared)
    if _tree_manifest(prepared) != expected_manifest:
        raise _SourceIntegrityError("operator skill source copy did not verify")
    transaction["stage_state"] = "publishing"
    _write_ledger(ledger_path, ledger)
    stage = _transaction_path(skills_root, "stage", generation_id)
    _rename_no_replace(prepared, stage)
    transaction["stage_state"] = "published"
    _write_ledger(ledger_path, ledger)
    return stage


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
    prepared = _prepared_stage_path(ledger_path, generation_id)
    if transaction["claim_state"] != "idle":
        return False
    if transaction["stage_state"] == "preparing" and _exists(prepared):
        _queue_garbage(
            ledger,
            kind="prepared",
            path_generation_id=generation_id,
            manifest=transaction["expected_manifest"],
        )
    if transaction["stage_state"] == "publishing" and _exists(prepared):
        return False
    if transaction["stage_state"] in {"publishing", "published"} and _exists(stage):
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
    if transaction["claim_state"] != "claimed":
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
    transaction["claim_state"] = "idle"
    _write_ledger(ledger_path, ledger)


def _reconcile_stage_publication(
    ledger_path: Path,
    ledger: dict[str, Any],
    skills_root: Path,
    transaction: dict[str, Any],
) -> None:
    if transaction["stage_state"] != "publishing":
        return
    generation_id = transaction["generation_id"]
    prepared = _prepared_stage_path(ledger_path, generation_id)
    stage = _transaction_path(skills_root, "stage", generation_id)
    prepared_exists = _exists(prepared)
    stage_exists = _exists(stage)
    if prepared_exists and stage_exists:
        raise _ClientConflictError("stage source and destination both exist")
    if prepared_exists:
        if _tree_manifest(prepared) != transaction["expected_manifest"]:
            raise _ClientConflictError("prepared Ava stage was modified")
        _rename_no_replace(prepared, stage)
    elif stage_exists:
        _verify_marker(stage, ledger["installation_id"], generation_id)
        if _tree_manifest(stage) != transaction["expected_manifest"]:
            raise _ClientConflictError("published Ava stage was modified")
    else:
        raise _ClientConflictError("stage publication outcome is ambiguous")
    transaction["stage_state"] = "published"
    _write_ledger(ledger_path, ledger)


def _reconcile_target_claim(
    ledger_path: Path,
    ledger: dict[str, Any],
    transaction: dict[str, Any],
    previous: Path,
    target: Path,
) -> None:
    if transaction["claim_state"] != "claiming":
        return
    old = ledger["installed"]
    if old is None:
        raise _ClientConflictError("target claim has no installed ownership record")
    previous_exists = _exists(previous)
    target_exists = _exists(target)
    if previous_exists and target_exists:
        raise _ClientConflictError("target claim outcome is ambiguous")
    if previous_exists:
        _verify_marker(previous, ledger["installation_id"], old["generation_id"])
        _require_digest(previous, old["digest"], "claimed managed target was modified")
        transaction["claim_state"] = "claimed"
    elif target_exists:
        _verify_marker(target, ledger["installation_id"], old["generation_id"])
        _require_digest(target, old["digest"], "managed target changed during claim")
        transaction["claim_state"] = "idle"
    else:
        raise _ClientConflictError("target claim outcome is ambiguous")
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
    if transaction["stage_state"] != "published" or not _exists(stage):
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
        transaction["claim_state"] = "claiming"
        _write_ledger(ledger_path, ledger)
        try:
            _rename_no_replace(target, previous)
            transaction["claim_state"] = "claimed"
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
    _reconcile_stage_publication(ledger_path, ledger, skills_root, transaction)
    if transaction["stage_state"] == "published" and _exists(target) and not _exists(stage):
        _verify_marker(target, ledger["installation_id"], transaction["generation_id"])
        _require_digest(
            target, transaction["expected_digest"], "activated transaction was modified"
        )
        action = "installed" if ledger["installed"] is None else "updated"
        _commit_activation(ledger_path, ledger, transaction, previous)
        return action
    _reconcile_target_claim(ledger_path, ledger, transaction, previous, target)
    if transaction["claim_state"] == "claimed":
        if _exists(previous) and _exists(target):
            raise _ClientConflictError("late target prevents restoration of claimed copy")
        _restore_claimed_previous(ledger_path, ledger, transaction, previous, target)
    if transaction["stage_state"] == "published" and _exists(stage):
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
    snapshot: _SourceSnapshot,
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
                "retained": [],
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
        _stage_copy(snapshot, source_manifest, skills_root, ledger_path, ledger, source_digest)
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
    snapshot = _source_snapshot(source)
    source_manifest = snapshot.manifest()
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
                    snapshot,
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
