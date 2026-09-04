"""Crash-recoverable preservation of external skill transaction residue."""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from cli.commands._external_agent_skill_fs import (
    _ClientConflictError,
    _exists,
    _tree_manifest,
    _validate_manifest_subset,
    _verify_cleanup_file,
)
from cli.commands._external_agent_skill_ledger import _write_ledger

_SKILL_NAME = "operating-ava-cluster"


def _transaction_path(skills_root: Path, kind: str, generation_id: str) -> Path:
    return skills_root / f".{_SKILL_NAME}.ava-{kind}-{generation_id}"


def _prepared_path(ledger_path: Path, generation_id: str) -> Path:
    return ledger_path.parent / f".{ledger_path.stem}-stage-{generation_id}"


def _quarantine_path(ledger_path: Path, kind: str, generation_id: str) -> Path:
    return ledger_path.parent / f".{ledger_path.stem}-retained-{kind}-{generation_id}"


def _file_claims(manifest: list[dict[str, Any]]) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []
    for item in manifest:
        if item["kind"] != "file":
            continue
        relative = str(item["path"])
        path = Path(relative)
        suffix = hashlib.sha256(relative.encode()).hexdigest()[:16]
        quarantine = path.with_name(f".{path.name}.ava-retained-{suffix}").as_posix()
        claims.append({"path": relative, "quarantine": quarantine, "state": "source"})
    return claims


def _queue_garbage(
    ledger: dict[str, Any],
    *,
    kind: str,
    path_generation_id: str,
    manifest: list[dict[str, Any]],
) -> None:
    record = {
        "file_claims": _file_claims(manifest),
        "kind": kind,
        "location": "source",
        "manifest": manifest,
        "path_generation_id": path_generation_id,
    }
    if record not in ledger["garbage"]:
        ledger["garbage"].append(record)


def _source_path(ledger_path: Path, skills_root: Path, kind: str, generation_id: str) -> Path:
    if kind == "prepared":
        return _prepared_path(ledger_path, generation_id)
    return _transaction_path(skills_root, kind, generation_id)


def _verify_cleanup_candidate(path: Path, manifest: list[dict[str, Any]]) -> None:
    current = _tree_manifest(path)
    _validate_manifest_subset(current, manifest)


def _restore_cleanup_candidate(
    ledger_path: Path,
    ledger: dict[str, Any],
    item: dict[str, Any],
    source: Path,
    quarantine: Path,
    rename: Callable[[Path, Path], None],
) -> None:
    if _exists(quarantine) and not _exists(source):
        rename(quarantine, source)
        item["location"] = "source"
        _write_ledger(ledger_path, ledger)


def _manifest_files(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(entry["path"]): entry for entry in item["manifest"] if entry["kind"] == "file"}


def _preserve_file_claim(
    ledger_path: Path,
    ledger: dict[str, Any],
    claim: dict[str, str],
    root: Path,
    expected: dict[str, Any],
    rename: Callable[[Path, Path], None],
) -> None:
    """Isolate one file, then terminally preserve it instead of path-unlinking."""
    source = root / claim["path"]
    quarantine = root / claim["quarantine"]
    if claim["state"] == "source":
        if not _exists(source) or _exists(quarantine):
            claim["state"] = "retained"
            _write_ledger(ledger_path, ledger)
            return
        try:
            _verify_cleanup_file(source, expected)
        except (OSError, _ClientConflictError):
            claim["state"] = "retained"
            _write_ledger(ledger_path, ledger)
            return
        claim["state"] = "claiming"
        _write_ledger(ledger_path, ledger)
        try:
            rename(source, quarantine)
        except OSError:
            # The root is already private. Whether the rename failed or
            # completed before reporting failure, terminal preservation is
            # safe and avoids inferring ownership or retrying a mutable path.
            claim["state"] = "retained"
            _write_ledger(ledger_path, ledger)
            return
        claim["state"] = "quarantine"
        _write_ledger(ledger_path, ledger)
    elif claim["state"] == "claiming":
        source_exists = _exists(source)
        quarantine_exists = _exists(quarantine)
        if source_exists and not quarantine_exists:
            claim["state"] = "source"
            _write_ledger(ledger_path, ledger)
            _preserve_file_claim(ledger_path, ledger, claim, root, expected, rename)
            return
        # A crash left the rename outcome ambiguous. The write-ahead record
        # names both paths, but path shape or matching bytes cannot establish
        # that the destination is the object Ava moved. Preserve both.
        claim["state"] = "retained"
        _write_ledger(ledger_path, ledger)
        return
    if claim["state"] == "quarantine":
        # The root is already isolated under Ava's private ledger. A changed
        # pathname is never retried or adopted for deletion.
        with suppress(OSError, _ClientConflictError):
            _verify_cleanup_file(quarantine, expected)
        claim["state"] = "retained"
        _write_ledger(ledger_path, ledger)


def _preserve_claimed_tree(
    ledger_path: Path,
    ledger: dict[str, Any],
    item: dict[str, Any],
    quarantine: Path,
    rename: Callable[[Path, Path], None],
) -> None:
    files = _manifest_files(item)
    for claim in item["file_claims"]:
        expected = files[claim["path"]]
        _preserve_file_claim(ledger_path, ledger, claim, quarantine, expected, rename)
    item["location"] = "retained"
    ledger["garbage"].remove(item)
    ledger["retained"].append(item)
    _write_ledger(ledger_path, ledger)


def _cleanup_garbage_item(
    ledger_path: Path,
    ledger: dict[str, Any],
    skills_root: Path,
    item: dict[str, Any],
    rename: Callable[[Path, Path], None],
) -> bool:
    generation_id = item["path_generation_id"]
    source = _source_path(ledger_path, skills_root, item["kind"], generation_id)
    quarantine = _quarantine_path(ledger_path, item["kind"], generation_id)
    if item["location"] == "source":
        source_exists = _exists(source)
        quarantine_exists = _exists(quarantine)
        if not source_exists:
            if quarantine_exists:
                raise _ClientConflictError("unclaimed cleanup quarantine exists")
            return True
        if quarantine_exists:
            raise _ClientConflictError("cleanup source and quarantine both exist")
        item["location"] = "claiming"
        _write_ledger(ledger_path, ledger)
        try:
            rename(source, quarantine)
        except OSError:
            if _exists(source) and not _exists(quarantine):
                item["location"] = "source"
                _write_ledger(ledger_path, ledger)
            raise
    if item["location"] == "claiming":
        source_exists = _exists(source)
        quarantine_exists = _exists(quarantine)
        if source_exists and quarantine_exists:
            raise _ClientConflictError("cleanup claim outcome is ambiguous")
        if source_exists:
            item["location"] = "source"
            _write_ledger(ledger_path, ledger)
            return False
        if not quarantine_exists:
            raise _ClientConflictError("cleanup claim outcome is ambiguous")
        try:
            _verify_cleanup_candidate(quarantine, item["manifest"])
        except (OSError, _ClientConflictError):
            _restore_cleanup_candidate(ledger_path, ledger, item, source, quarantine, rename)
            raise
        item["location"] = "quarantine"
        _write_ledger(ledger_path, ledger)
    if not _exists(quarantine):
        return True
    _preserve_claimed_tree(ledger_path, ledger, item, quarantine, rename)
    return True


def _cleanup_garbage_impl(
    ledger_path: Path,
    ledger: dict[str, Any],
    skills_root: Path,
    label: str,
    rename: Callable[[Path, Path], None],
) -> None:
    for item in list(ledger["garbage"]):
        try:
            if (
                _cleanup_garbage_item(ledger_path, ledger, skills_root, item, rename)
                and item in ledger["garbage"]
            ):
                ledger["garbage"].remove(item)
                _write_ledger(ledger_path, ledger)
        except (OSError, _ClientConflictError) as exc:
            print(
                f"  ! {label} external operator skill skipped: conflict: "
                f"transaction cleanup conflict ({type(exc).__name__})",
                file=sys.stderr,
            )
