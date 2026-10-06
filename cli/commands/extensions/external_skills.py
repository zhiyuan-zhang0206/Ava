"""Publish Ava's operator skill into already-present external agent homes.

The external homes remain user-owned.  Ava's authority is bound by a private
ledger under ``AVA_HOME`` and every mutation is serialized and limited to each
named skill target (plus transaction siblings bearing an Ava generation ID).
"""

from __future__ import annotations

import stat
import sys
import uuid
from pathlib import Path
from typing import Any, cast

from base.host.private_storage import ensure_private_dir
from base.native_process.os_platform import LockTimeoutError, file_lock
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.extensions.external_skill_host.cleanup import (
    cleanup_garbage_impl,
    queue_garbage,
    transaction_path,
)
from cli.commands.extensions.external_skill_host.filesystem import (
    ClientConflictError,
    SourceIntegrityError,
    SourceSnapshot,
    exists,
    lstat,
    manifest_digest,
    materialize_source_snapshot,
    rename_no_replace,
    source_lstat,
    source_snapshot,
    tree_digest,
    tree_manifest,
    write_new,
)
from cli.commands.extensions.external_skill_host.ledger import (
    FORMAT,
    SKILL_NAME,
    load_ledger,
    ownership_marker,
    parse_record,
    stage_manifest,
    write_ledger,
)

_SKILL_NAMES: tuple[str, ...] = ("ava-guide",)
_MARKER_NAME = ".ava-managed.json"
_CLIENTS = (("Codex", ".codex", "codex"), ("Claude Code", ".claude", "claude"))


def _prepared_stage_path(ledger_path: Path, generation_id: str) -> Path:
    return ledger_path.parent / f".{ledger_path.stem}-stage-{generation_id}"


def _verify_marker(
    root: Path, installation_id: str, generation_id: str, *, skill_name: str = SKILL_NAME
) -> None:
    if not stat.S_ISDIR(lstat(root).st_mode):
        raise ClientConflictError("Ava-managed target is not a regular directory")
    record = parse_record(root / _MARKER_NAME)
    if (
        record is None
        or record.get("skill") != skill_name
        or record.get("installation_id") != installation_id
        or record.get("generation_id") != generation_id
    ):
        raise ClientConflictError("Ava-managed target ownership marker does not match its ledger")


def _require_digest(root: Path, expected: str, reason: str) -> None:
    if tree_digest(root) != expected:
        raise ClientConflictError(reason)


def _validate_lock(path: Path) -> None:
    if exists(path) and not stat.S_ISREG(lstat(path).st_mode):
        raise ClientConflictError("ownership lock is not a regular file")


def _validate_directory(path: Path, reason: str) -> None:
    if not stat.S_ISDIR(lstat(path).st_mode):
        raise ClientConflictError(reason)


def _warn(label: str, reason: str) -> None:
    print(f"  ! {label} external operator skill skipped: conflict: {reason}", file=sys.stderr)


def _cleanup_garbage(
    ledger_path: Path,
    ledger: dict[str, Any],
    skills_root: Path,
    label: str,
    *,
    skill_name: str = SKILL_NAME,
) -> None:
    cleanup_garbage_impl(
        ledger_path, ledger, skills_root, label, rename_no_replace, skill_name=skill_name
    )


def _stage_copy(
    snapshot: SourceSnapshot,
    source_manifest: list[dict[str, Any]],
    skills_root: Path,
    ledger_path: Path,
    ledger: dict[str, Any],
    source_digest: str,
    *,
    skill_name: str = SKILL_NAME,
) -> Path:
    generation_id = uuid.uuid4().hex
    marker = ownership_marker(
        ledger["installation_id"], generation_id, source_digest, skill_name=skill_name
    )
    expected_manifest = stage_manifest(source_manifest, marker)
    transaction = {
        "claim_state": "idle",
        "expected_digest": manifest_digest(expected_manifest),
        "expected_manifest": expected_manifest,
        "generation_id": generation_id,
        "source_digest": source_digest,
        "stage_state": "preparing",
    }
    ledger["transaction"] = transaction
    write_ledger(ledger_path, ledger)
    prepared = _prepared_stage_path(ledger_path, generation_id)
    prepared.mkdir(mode=0o700)
    write_new(prepared / _MARKER_NAME, marker, 0o600)
    materialize_source_snapshot(snapshot, prepared)
    if tree_manifest(prepared) != expected_manifest:
        raise SourceIntegrityError("operator skill source copy did not verify")
    transaction["stage_state"] = "publishing"
    write_ledger(ledger_path, ledger)
    stage = transaction_path(skills_root, "stage", generation_id, skill_name=skill_name)
    rename_no_replace(prepared, stage)
    transaction["stage_state"] = "published"
    write_ledger(ledger_path, ledger)
    return stage


def _abandon_transaction(
    ledger_path: Path,
    ledger: dict[str, Any],
    skills_root: Path,
    *,
    skill_name: str = SKILL_NAME,
) -> bool:
    """Move every remaining residue pointer to durable cleanup state."""
    transaction = cast(dict[str, Any] | None, ledger["transaction"])
    if transaction is None:
        return True
    generation_id = transaction["generation_id"]
    stage = transaction_path(skills_root, "stage", generation_id, skill_name=skill_name)
    prepared = _prepared_stage_path(ledger_path, generation_id)
    if transaction["claim_state"] != "idle":
        return False
    if transaction["stage_state"] == "preparing" and exists(prepared):
        queue_garbage(
            ledger,
            kind="prepared",
            path_generation_id=generation_id,
            manifest=transaction["expected_manifest"],
        )
    if transaction["stage_state"] == "publishing" and exists(prepared):
        return False
    if transaction["stage_state"] in {"publishing", "published"} and exists(stage):
        queue_garbage(
            ledger,
            kind="stage",
            path_generation_id=generation_id,
            manifest=transaction["expected_manifest"],
        )
    ledger["transaction"] = None
    write_ledger(ledger_path, ledger)
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
        raise ClientConflictError("claimed target has no installed ownership record")
    if not exists(previous):
        if not exists(target):
            raise ClientConflictError("claimed target and prior copy are both missing")
        _verify_marker(
            target, ledger["installation_id"], old["generation_id"], skill_name=target.name
        )
        _require_digest(target, old["digest"], "restored managed target was modified")
    else:
        rename_no_replace(previous, target)
    transaction["claim_state"] = "idle"
    write_ledger(ledger_path, ledger)


def _reconcile_stage_publication(
    ledger_path: Path,
    ledger: dict[str, Any],
    skills_root: Path,
    transaction: dict[str, Any],
    *,
    skill_name: str = SKILL_NAME,
) -> None:
    if transaction["stage_state"] != "publishing":
        return
    generation_id = transaction["generation_id"]
    prepared = _prepared_stage_path(ledger_path, generation_id)
    stage = transaction_path(skills_root, "stage", generation_id, skill_name=skill_name)
    prepared_exists = exists(prepared)
    stage_exists = exists(stage)
    if prepared_exists and stage_exists:
        raise ClientConflictError("stage source and destination both exist")
    if prepared_exists:
        if tree_manifest(prepared) != transaction["expected_manifest"]:
            raise ClientConflictError("prepared Ava stage was modified")
        rename_no_replace(prepared, stage)
    elif stage_exists:
        _verify_marker(stage, ledger["installation_id"], generation_id, skill_name=skill_name)
        if tree_manifest(stage) != transaction["expected_manifest"]:
            raise ClientConflictError("published Ava stage was modified")
    else:
        raise ClientConflictError("stage publication outcome is ambiguous")
    transaction["stage_state"] = "published"
    write_ledger(ledger_path, ledger)


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
        raise ClientConflictError("target claim has no installed ownership record")
    previous_exists = exists(previous)
    target_exists = exists(target)
    if previous_exists and target_exists:
        raise ClientConflictError("target claim outcome is ambiguous")
    if previous_exists:
        _verify_marker(
            previous, ledger["installation_id"], old["generation_id"], skill_name=target.name
        )
        _require_digest(previous, old["digest"], "claimed managed target was modified")
        transaction["claim_state"] = "claimed"
    elif target_exists:
        _verify_marker(
            target, ledger["installation_id"], old["generation_id"], skill_name=target.name
        )
        _require_digest(target, old["digest"], "managed target changed during claim")
        transaction["claim_state"] = "idle"
    else:
        raise ClientConflictError("target claim outcome is ambiguous")
    write_ledger(ledger_path, ledger)


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
    if exists(previous) and old is not None:
        queue_garbage(
            ledger,
            kind="previous",
            path_generation_id=transaction["generation_id"],
            manifest=old["manifest"],
        )
    write_ledger(ledger_path, ledger)


def _activate(
    ledger_path: Path,
    ledger: dict[str, Any],
    skills_root: Path,
    target: Path,
) -> str:
    transaction = cast(dict[str, Any], ledger["transaction"])
    generation_id = transaction["generation_id"]
    stage = transaction_path(skills_root, "stage", generation_id, skill_name=target.name)
    previous = transaction_path(skills_root, "previous", generation_id, skill_name=target.name)
    if transaction["stage_state"] != "published" or not exists(stage):
        raise ClientConflictError("incomplete Ava transaction was preserved")
    _verify_marker(stage, ledger["installation_id"], generation_id, skill_name=target.name)
    if tree_manifest(stage) != transaction["expected_manifest"]:
        raise ClientConflictError("staged Ava transaction was modified")
    old = ledger["installed"]
    action = "installed" if old is None else "updated"
    if exists(target):
        if old is None:
            raise ClientConflictError("unmanaged target appeared during installation")
        if exists(previous):
            raise ClientConflictError("prior transaction path already exists")
        transaction["claim_state"] = "claiming"
        write_ledger(ledger_path, ledger)
        try:
            rename_no_replace(target, previous)
            transaction["claim_state"] = "claimed"
            write_ledger(ledger_path, ledger)
            _verify_marker(
                previous, ledger["installation_id"], old["generation_id"], skill_name=target.name
            )
            _require_digest(
                previous, old["digest"], "managed target changed before it could be claimed"
            )
        except (OSError, ClientConflictError):
            _restore_claimed_previous(ledger_path, ledger, transaction, previous, target)
            raise
    if exists(target):
        raise ClientConflictError("a target appeared after the managed copy was claimed")
    try:
        rename_no_replace(stage, target)
    except OSError:
        _restore_claimed_previous(ledger_path, ledger, transaction, previous, target)
        raise
    _verify_marker(target, ledger["installation_id"], generation_id, skill_name=target.name)
    if tree_digest(target) != transaction["expected_digest"]:
        raise ClientConflictError("activated Ava target failed verification")
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
    stage = transaction_path(
        skills_root, "stage", transaction["generation_id"], skill_name=target.name
    )
    previous = transaction_path(
        skills_root, "previous", transaction["generation_id"], skill_name=target.name
    )
    _reconcile_stage_publication(
        ledger_path, ledger, skills_root, transaction, skill_name=target.name
    )
    if transaction["stage_state"] == "published" and exists(target) and not exists(stage):
        _verify_marker(
            target, ledger["installation_id"], transaction["generation_id"], skill_name=target.name
        )
        _require_digest(
            target, transaction["expected_digest"], "activated transaction was modified"
        )
        action = "installed" if ledger["installed"] is None else "updated"
        _commit_activation(ledger_path, ledger, transaction, previous)
        return action
    _reconcile_target_claim(ledger_path, ledger, transaction, previous, target)
    if transaction["claim_state"] == "claimed":
        if exists(previous) and exists(target):
            raise ClientConflictError("late target prevents restoration of claimed copy")
        _restore_claimed_previous(ledger_path, ledger, transaction, previous, target)
    if transaction["stage_state"] == "published" and exists(stage):
        return _activate(ledger_path, ledger, skills_root, target)
    if _abandon_transaction(ledger_path, ledger, skills_root, skill_name=target.name):
        return None
    raise ClientConflictError("incomplete Ava transaction still owns a claimed target")


def _validate_source_path(repo: Path, source: Path) -> None:
    current = repo
    repo_stat = source_lstat(current)
    if not stat.S_ISDIR(repo_stat.st_mode):
        raise SourceIntegrityError("operator skill repository root is not a directory")
    for part in source.relative_to(repo).parts:
        current /= part
        current_stat = source_lstat(current)
        if not stat.S_ISDIR(current_stat.st_mode):
            raise SourceIntegrityError("operator skill source path is not a directory")


def _ensure_ledger_root(ctx: ConvergeCtx) -> Path:
    for path in (ctx.ava_home, ctx.ava_home / "configs"):
        if not stat.S_ISDIR(lstat(path).st_mode):
            raise ClientConflictError("private ownership ledger parent is not a directory")
    root = ctx.ava_home / "configs" / "external-agent-skills"
    ensure_private_dir(root)
    return root


def _skills_root_of(client_home: Path) -> Path:
    """The client's `skills/` directory, created when absent; refuses a non-directory."""
    home_stat = lstat(client_home)
    if not stat.S_ISDIR(home_stat.st_mode):
        raise ClientConflictError("client home is not a regular directory")
    skills_root = client_home / "skills"
    if not exists(skills_root):
        skills_root.mkdir()
    if not stat.S_ISDIR(lstat(skills_root).st_mode):
        raise ClientConflictError("skills root is not a regular directory")
    return skills_root


def _ledger_for(ledger_path: Path, client_key: str, target: Path) -> dict[str, Any]:
    """The client's ownership ledger, minted fresh when none exists (an unmanaged target refuses)."""
    ledger = load_ledger(ledger_path, client_key)
    if ledger is None:
        if exists(target):
            raise ClientConflictError("unmanaged target was preserved")
        ledger = cast(
            dict[str, Any],
            {
                "client": client_key,
                "format": FORMAT,
                "garbage": [],
                "installation_id": uuid.uuid4().hex,
                "installed": None,
                "retained": [],
                "transaction": None,
            },
        )
        write_ledger(ledger_path, ledger)
    return ledger


def _managed_target_current(ledger: dict[str, Any], target: Path, source_digest: str) -> bool:
    """True when the managed copy is intact and already at `source_digest`.

    Refuses a missing, user-modified or unmanaged target; False means a (re)install is due.
    """
    installed = ledger["installed"]
    if installed is None:
        if exists(target):
            raise ClientConflictError("unmanaged target was preserved")
        return False
    if not exists(target):
        raise ClientConflictError("managed target is missing")
    _verify_marker(
        target, ledger["installation_id"], installed["generation_id"], skill_name=target.name
    )
    if tree_digest(target) != installed["digest"]:
        raise ClientConflictError("user-modified managed target was preserved")
    return installed["source_digest"] == source_digest


def _converge_locked(
    snapshot: SourceSnapshot,
    source_manifest: list[dict[str, Any]],
    source_digest: str,
    client_home: Path,
    client_key: str,
    label: str,
    ledger_path: Path,
    *,
    skill_name: str = SKILL_NAME,
) -> None:
    skills_root = _skills_root_of(client_home)
    target = skills_root / skill_name
    ledger = _ledger_for(ledger_path, client_key, target)
    _cleanup_garbage(ledger_path, ledger, skills_root, label, skill_name=skill_name)
    recovered = _recover(ledger_path, ledger, skills_root, target)
    if recovered is not None:
        print(f"  · {label} external operator skill {recovered}: skills/{skill_name}")
        _cleanup_garbage(ledger_path, ledger, skills_root, label, skill_name=skill_name)
        return
    _cleanup_garbage(ledger_path, ledger, skills_root, label, skill_name=skill_name)
    if _managed_target_current(ledger, target, source_digest):
        return
    try:
        _stage_copy(
            snapshot,
            source_manifest,
            skills_root,
            ledger_path,
            ledger,
            source_digest,
            skill_name=skill_name,
        )
    except (OSError, SourceIntegrityError):
        if _abandon_transaction(ledger_path, ledger, skills_root, skill_name=skill_name):
            _cleanup_garbage(ledger_path, ledger, skills_root, label, skill_name=skill_name)
        raise
    try:
        action = _activate(ledger_path, ledger, skills_root, target)
    except (OSError, ClientConflictError):
        if _abandon_transaction(ledger_path, ledger, skills_root, skill_name=skill_name):
            _cleanup_garbage(ledger_path, ledger, skills_root, label, skill_name=skill_name)
        raise
    print(f"  · {label} external operator skill {action}: skills/{skill_name}")
    _cleanup_garbage(ledger_path, ledger, skills_root, label, skill_name=skill_name)


def converge_external_agent_skill(ctx: ConvergeCtx, *, host_home: Path | None = None) -> None:
    """Copy the complete Ava Guide into present Codex and Claude Code homes.

    Legacy standalone targets and their ledgers remain untouched; the guide
    uses a separate per-client ledger and never adopts or overwrites them.
    """
    home = Path.home() if host_home is None else host_home
    try:
        _validate_directory(home, "host home is not a regular directory")
    except (OSError, ClientConflictError) as exc:
        for label, _, _ in _CLIENTS:
            _warn(label, f"host home unavailable ({type(exc).__name__})")
        return
    present = [client for client in _CLIENTS if exists(home / client[1])]
    if not present:
        return
    try:
        ledger_root = _ensure_ledger_root(ctx)
    except (OSError, RuntimeError) as exc:
        for label, _, _ in present:
            _warn(label, f"private ownership ledger unavailable ({type(exc).__name__})")
        return
    for skill_name in _SKILL_NAMES:
        source = ctx.repo / "ava_builtins" / "skills" / "platform" / skill_name
        _validate_source_path(ctx.repo, source)
        snapshot = source_snapshot(source)
        source_manifest = snapshot.manifest()
        source_digest = manifest_digest(source_manifest)
        for label, home_name, client_key in present:
            ledger_name = client_key if skill_name == SKILL_NAME else f"{client_key}-{skill_name}"
            ledger_path = ledger_root / f"{ledger_name}.json"
            lock_path = ledger_root / f"{ledger_name}.lock"
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
                        skill_name=skill_name,
                    )
            except ClientConflictError as exc:
                _warn(label, str(exc))
            except (LockTimeoutError, OSError) as exc:
                _warn(label, f"conflict ({type(exc).__name__})")
