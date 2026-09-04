"""Publish Ava's operator skill into already-present external agent homes.

The external homes remain user-owned.  Ava's authority is bound by a private
ledger under ``AVA_HOME`` and every mutation is serialized and limited to one
named skill target (plus transaction siblings bearing an Ava generation ID).
"""

from __future__ import annotations

import json
import re
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
    _read_regular,
    _rename_no_replace,
    _source_lstat,
    _SourceIntegrityError,
    _tree_digest,
    _write_new,
)
from shared.platform import LockTimeoutError, file_lock
from shared.private_storage import ensure_private_dir, write_private_bytes

_SKILL_NAME = "operating-ava-cluster"
_MARKER_NAME = ".ava-managed.json"
_FORMAT = 2
_CLIENTS = (("Codex", ".codex", "codex"), ("Claude Code", ".claude", "claude"))
_HEX = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[0-9a-f]{32}$")


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


def _parse_record(path: Path) -> dict[str, Any] | None:
    if not _exists(path):
        return None
    data, _ = _read_regular(path, source=False)
    try:
        value: object = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _ClientConflictError("Ava ownership ledger is invalid") from exc
    if not isinstance(value, dict):
        raise _ClientConflictError("Ava ownership ledger is invalid")
    return cast(dict[str, Any], value)


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _load_ledger(path: Path, client_key: str) -> dict[str, Any] | None:
    record = _parse_record(path)
    if record is None:
        return None
    required = {
        "client",
        "format",
        "installation_id",
        "installed",
        "transaction",
        "garbage",
    }
    if set(record) != required or record["client"] != client_key or record["format"] != _FORMAT:
        raise _ClientConflictError("Ava ownership ledger is invalid")
    if not _valid_id(record["installation_id"]):
        raise _ClientConflictError("Ava ownership ledger is invalid")
    installed_value: object = record["installed"]
    if installed_value is not None:
        if not isinstance(installed_value, dict):
            raise _ClientConflictError("Ava ownership ledger is invalid")
        installed = cast(dict[str, Any], installed_value)
        if (
            set(installed) != {"digest", "generation_id", "source_digest"}
            or not all(_valid_digest(installed[key]) for key in ("digest", "source_digest"))
            or not _valid_id(installed["generation_id"])
        ):
            raise _ClientConflictError("Ava ownership ledger is invalid")
    transaction_value: object = record["transaction"]
    if transaction_value is not None:
        if not isinstance(transaction_value, dict):
            raise _ClientConflictError("Ava ownership ledger is invalid")
        transaction = cast(dict[str, Any], transaction_value)
        if (
            set(transaction)
            != {"generation_id", "initial_digest", "source_digest", "staged_digest"}
            or not _valid_id(transaction["generation_id"])
            or not _valid_digest(transaction["source_digest"])
            or any(
                value is not None and not _valid_digest(value)
                for value in (transaction["initial_digest"], transaction["staged_digest"])
            )
        ):
            raise _ClientConflictError("Ava ownership ledger is invalid")
    garbage_value: object = record["garbage"]
    if not isinstance(garbage_value, list):
        raise _ClientConflictError("Ava ownership ledger is invalid")
    for item_value in cast(list[object], garbage_value):
        if not isinstance(item_value, dict):
            raise _ClientConflictError("Ava ownership ledger is invalid")
        item = cast(dict[str, Any], item_value)
        if (
            set(item) != {"digest", "kind", "marker_generation_id", "path_generation_id"}
            or item["kind"] not in {"stage", "previous"}
            or not _valid_digest(item["digest"])
            or not _valid_id(item["marker_generation_id"])
            or not _valid_id(item["path_generation_id"])
        ):
            raise _ClientConflictError("Ava ownership ledger is invalid")
    return record


def _write_ledger(path: Path, ledger: dict[str, Any]) -> None:
    write_private_bytes(path, (json.dumps(ledger, indent=2, sort_keys=True) + "\n").encode())


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


def _remove_owned_tree(root: Path, installation_id: str, generation_id: str, digest: str) -> None:
    _verify_marker(root, installation_id, generation_id)
    if _tree_digest(root) != digest:
        raise _ClientConflictError("transaction residue was modified and was preserved")

    def remove(directory: Path) -> None:
        _lstat(directory)
        for child in list(directory.iterdir()):
            current = _lstat(child)
            if stat.S_ISDIR(current.st_mode):
                remove(child)
            elif stat.S_ISREG(current.st_mode):
                child.unlink()
            else:
                raise _ClientConflictError("transaction residue contains an unsafe entry")
        directory.rmdir()

    remove(root)


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
            _remove_owned_tree(
                path, ledger["installation_id"], item["marker_generation_id"], item["digest"]
            )
        except (OSError, _ClientConflictError) as exc:
            remaining.append(item)
            _warn(label, f"transaction cleanup conflict ({type(exc).__name__})")
    if remaining != ledger["garbage"]:
        ledger["garbage"] = remaining
        _write_ledger(ledger_path, ledger)


def _stage_copy(
    source: Path,
    skills_root: Path,
    ledger_path: Path,
    ledger: dict[str, Any],
    source_digest: str,
) -> Path:
    generation_id = uuid.uuid4().hex
    transaction = {
        "generation_id": generation_id,
        "initial_digest": None,
        "source_digest": source_digest,
        "staged_digest": None,
    }
    ledger["transaction"] = transaction
    _write_ledger(ledger_path, ledger)
    stage = _transaction_path(skills_root, "stage", generation_id)
    stage.mkdir(mode=0o700)
    marker = _marker(ledger["installation_id"], generation_id, source_digest)
    _write_new(stage / _MARKER_NAME, marker, 0o600)
    transaction["initial_digest"] = _tree_digest(stage)
    _write_ledger(ledger_path, ledger)
    _copy_source_contents(source, stage)
    if _tree_digest(stage, ignore_root_names=frozenset({_MARKER_NAME})) != source_digest:
        raise _SourceIntegrityError("operator skill source copy did not verify")
    transaction["staged_digest"] = _tree_digest(stage)
    _write_ledger(ledger_path, ledger)
    return stage


def _abandon_stage(ledger_path: Path, ledger: dict[str, Any], skills_root: Path) -> None:
    transaction = ledger["transaction"]
    if transaction is None:
        return
    stage = _transaction_path(skills_root, "stage", transaction["generation_id"])
    if _exists(stage) and transaction["staged_digest"] is not None:
        ledger["garbage"].append(
            {
                "digest": transaction["staged_digest"],
                "kind": "stage",
                "marker_generation_id": transaction["generation_id"],
                "path_generation_id": transaction["generation_id"],
            }
        )
    ledger["transaction"] = None
    _write_ledger(ledger_path, ledger)


def _commit_activation(
    ledger_path: Path,
    ledger: dict[str, Any],
    transaction: dict[str, Any],
    previous: Path,
) -> None:
    old = ledger["installed"]
    ledger["installed"] = {
        "digest": transaction["staged_digest"],
        "generation_id": transaction["generation_id"],
        "source_digest": transaction["source_digest"],
    }
    ledger["transaction"] = None
    if _exists(previous) and old is not None:
        ledger["garbage"].append(
            {
                "digest": old["digest"],
                "kind": "previous",
                "marker_generation_id": old["generation_id"],
                "path_generation_id": transaction["generation_id"],
            }
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
    if transaction["staged_digest"] is None or not _exists(stage):
        raise _ClientConflictError("incomplete Ava transaction was preserved")
    _verify_marker(stage, ledger["installation_id"], generation_id)
    if _tree_digest(stage) != transaction["staged_digest"]:
        raise _ClientConflictError("staged Ava transaction was modified")
    old = ledger["installed"]
    action = "installed" if old is None else "updated"
    if _exists(target):
        if old is None:
            raise _ClientConflictError("unmanaged target appeared during installation")
        if _exists(previous):
            raise _ClientConflictError("prior transaction path already exists")
        target.replace(previous)
        try:
            _verify_marker(previous, ledger["installation_id"], old["generation_id"])
            _require_digest(
                previous, old["digest"], "managed target changed before it could be claimed"
            )
        except (OSError, _ClientConflictError):
            if not _exists(target):
                previous.replace(target)
            raise
    if _exists(target):
        raise _ClientConflictError("a target appeared after the managed copy was claimed")
    try:
        _rename_no_replace(stage, target)
    except OSError:
        if _exists(previous) and not _exists(target):
            previous.replace(target)
        raise
    _verify_marker(target, ledger["installation_id"], generation_id)
    if _tree_digest(target) != transaction["staged_digest"]:
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
    if transaction["staged_digest"] is not None and _exists(target) and not _exists(stage):
        _verify_marker(target, ledger["installation_id"], transaction["generation_id"])
        _require_digest(target, transaction["staged_digest"], "activated transaction was modified")
        previous = _transaction_path(skills_root, "previous", transaction["generation_id"])
        action = "installed" if ledger["installed"] is None else "updated"
        _commit_activation(ledger_path, ledger, transaction, previous)
        return action
    if transaction["staged_digest"] is not None and _exists(stage):
        return _activate(ledger_path, ledger, skills_root, target)
    if _exists(stage) and transaction["initial_digest"] is not None:
        initial_stage_is_unchanged = True
        try:
            _require_digest(stage, transaction["initial_digest"], "incomplete stage changed")
        except _ClientConflictError:
            initial_stage_is_unchanged = False
        if initial_stage_is_unchanged:
            ledger["garbage"].append(
                {
                    "digest": transaction["initial_digest"],
                    "kind": "stage",
                    "marker_generation_id": transaction["generation_id"],
                    "path_generation_id": transaction["generation_id"],
                }
            )
    _abandon_stage(ledger_path, ledger, skills_root)
    raise _ClientConflictError("incomplete Ava transaction was preserved")


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
    _stage_copy(source, skills_root, ledger_path, ledger, source_digest)
    try:
        action = _activate(ledger_path, ledger, skills_root, target)
    except (OSError, _ClientConflictError):
        transaction = cast(dict[str, Any], ledger["transaction"])
        stage = _transaction_path(skills_root, "stage", transaction["generation_id"])
        if _exists(stage):
            _abandon_stage(ledger_path, ledger, skills_root)
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
    source_digest = _tree_digest(source, source=True)
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
                    source, source_digest, home / home_name, client_key, label, ledger_path
                )
        except _ClientConflictError as exc:
            _warn(label, str(exc))
        except (LockTimeoutError, OSError) as exc:
            _warn(label, f"conflict ({type(exc).__name__})")
