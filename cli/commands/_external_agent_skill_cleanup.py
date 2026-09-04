"""Crash-recoverable cleanup for external operator skill transaction residue."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli.commands._external_agent_skill_fs import (
    _ClientConflictError,
    _exists,
    _tree_manifest,
    _validate_manifest_subset,
)
from cli.commands._external_agent_skill_ledger import _write_ledger

_SKILL_NAME = "operating-ava-cluster"


def _transaction_path(skills_root: Path, kind: str, generation_id: str) -> Path:
    return skills_root / f".{_SKILL_NAME}.ava-{kind}-{generation_id}"


def _quarantine_path(skills_root: Path, kind: str, generation_id: str) -> Path:
    return _transaction_path(skills_root, f"quarantine-{kind}", generation_id)


def _queue_garbage(
    ledger: dict[str, Any],
    *,
    kind: str,
    path_generation_id: str,
    manifest: list[dict[str, Any]],
) -> None:
    record = {
        "kind": kind,
        "location": "source",
        "manifest": manifest,
        "path_generation_id": path_generation_id,
    }
    if record not in ledger["garbage"]:
        ledger["garbage"].append(record)


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


def _cleanup_garbage_item(
    ledger_path: Path,
    ledger: dict[str, Any],
    skills_root: Path,
    item: dict[str, Any],
    remove_tree: Callable[[Path, list[dict[str, Any]]], None],
    rename: Callable[[Path, Path], None],
) -> bool:
    generation_id = item["path_generation_id"]
    source = _transaction_path(skills_root, item["kind"], generation_id)
    quarantine = _quarantine_path(skills_root, item["kind"], generation_id)
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
    remove_tree(quarantine, item["manifest"])
    return not _exists(quarantine)


def _cleanup_garbage_impl(
    ledger_path: Path,
    ledger: dict[str, Any],
    skills_root: Path,
    label: str,
    remove_tree: Callable[[Path, list[dict[str, Any]]], None],
    rename: Callable[[Path, Path], None],
) -> None:
    remaining: list[dict[str, Any]] = []
    for item in ledger["garbage"]:
        try:
            if not _cleanup_garbage_item(
                ledger_path, ledger, skills_root, item, remove_tree, rename
            ):
                remaining.append(item)
        except (OSError, _ClientConflictError) as exc:
            remaining.append(item)
            print(
                f"  ! {label} external operator skill skipped: conflict: "
                f"transaction cleanup conflict ({type(exc).__name__})",
                file=sys.stderr,
            )
    if remaining != ledger["garbage"]:
        ledger["garbage"] = remaining
        _write_ledger(ledger_path, ledger)
