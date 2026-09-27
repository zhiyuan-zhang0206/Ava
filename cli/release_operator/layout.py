"""On-disk layout for this package's own bookkeeping under `$AVA_HOME/releases`.

This is not a release-transition concept: `cli.release_transition` never
chooses a work directory for the caller (see `release_prepare.ava.okf.md`,
"the adapter does not create a home or infer one from environment
settings"). `ava cluster release prepare` needs *some* deterministic place to
put its work directory so `ava cluster release request` can find the
resulting receipt again by commit alone — this module is that one convention,
private to the operator-verb package.

A commit keeps the SAME work directory across attempts: `Preparation.work`
must not already exist when `prepare_image` starts, so a second `prepare` for
a commit whose first attempt failed needs that operator to inspect and clear
`work/<commit>` first (move it aside, or remove it) — the same "retry needs
fresh work, after the caller explicitly accounts for the retained evidence"
rule `release_prepare.ava.okf.md` already documents. This module does not
weaken that rule; it only fixes the name so a later command can find it.
"""

from __future__ import annotations

import re
from pathlib import Path

_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def require_commit_shape(commit: str) -> None:
    """Fail fast before any path is built from unvalidated operator input."""
    if not _COMMIT.fullmatch(commit):
        raise ValueError("commit must be an exact 40-character lowercase hex SHA")


def releases_store(home: Path) -> Path:
    return home / "releases"


def prepare_work_root(home: Path) -> Path:
    return releases_store(home) / "work"


def prepare_work_dir(home: Path, commit: str) -> Path:
    require_commit_shape(commit)
    return prepare_work_root(home) / commit


def receipt_path(home: Path, commit: str) -> Path:
    return prepare_work_dir(home, commit) / "receipt.json"
