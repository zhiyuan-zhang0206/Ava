"""Source-tree integrity guard — detector + repair for the prod source checkout.

The 2026-08-28 fleet-wide outage (a half-installed ``ava_ledger`` plugin) and
earlier incidents of the same class all trace to someone editing the prod
checkout (``$AVA_HOME/source``) outside the update flow — a tree edit broke
``import ava`` for every agent on the box within minutes. User ruling
2026-08-28: the tree must be kept whole — periodically reset to the installed
commit, tampering detected before exec crashes, and legitimate runtime
artifacts allowlisted.

One module, two callers, one whitelist — the same family as the
editable-install guard (``shared/editable_install.py``):

- ``ava cluster health-probe`` check 8 — read-only detection on the OS-cron
  cadence (every 5 minutes). Alerts on tamper; never writes.
- ``ava converge`` step "source tree reset + clean" — repair at every bring-up:
  ``git reset --hard`` to the last fully installed commit and ``git clean`` of
  untracked files outside the whitelist. The step is skipped while a cluster
  update is in flight: a rollout legitimately owns the tree (deploy verbs are
  exempt), and the update flow records ``installed_sha`` when it lands.

Both callers share ``SOURCE_TREE_WHITELIST``, so the detector and the fixer can
never disagree about what is legal.
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass
from pathlib import Path

import shared.telemetry
from shared.cluster_drift import prod_source_dir
from shared.gitenv import git_env
from shared.proc import run_bounded

# Bounded git: a guard must never hang a probe or a start (mirrors
# ``cluster_drift._GIT_TIMEOUT_S``); the repair gets more room because a reset
# can touch many paths.
_READ_TIMEOUT_S = 5.0
_WRITE_TIMEOUT_S = 30.0

# Runtime artifacts legitimately produced inside the prod checkout. Inventory
# (2026-08-28): ``frontend/`` holds the built UI bundle the gateway serves —
# the frontend session's ``npm run build`` output (``.next/``,
# ``tsconfig.tsbuildinfo``, ``next-env.d.ts``; ``node_modules`` is already
# gitignored). Patterns match git-relative paths (forward slashes); a
# trailing-slash pattern covers the whole subtree.
SOURCE_TREE_WHITELIST: tuple[str, ...] = ("frontend/",)


def _git(
    source: Path, *args: str, timeout: float = _READ_TIMEOUT_S
) -> subprocess.CompletedProcess[str] | None:
    """Run a bounded git command in ``source``; None only when the checkout is
    absent or not a git repo (the caller decides how to treat that)."""
    if not (source / ".git").exists():
        return None
    try:
        return run_bounded(
            ["git", "-C", str(source), *args],
            capture_output=True,
            text=True,
            env=git_env(),
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _is_whitelisted(rel_path: str) -> bool:
    """True when a git-relative path is a legal runtime artifact."""
    for pattern in SOURCE_TREE_WHITELIST:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        if pattern.endswith("/") and rel_path.startswith(pattern):
            return True
    return False


def source_tree_violations(repo: Path | None = None) -> tuple[str, ...]:
    """Human-readable tamper findings for the prod source checkout, else ().

    Best-effort: returns () when the checkout is absent, not a git repo, or
    git is unavailable — a probe must not fail on a transient git hiccup (the
    same never-crash contract as ``editable_install_violations``). Detects
    three tamper shapes:

    - tracked files changed vs HEAD (any non-untracked ``git status`` entry)
    - untracked files outside the whitelist
    - HEAD moved off the last fully installed commit (``installed_sha``)
    """
    source = Path(repo) if repo is not None else prod_source_dir()
    if source is None:
        return ()
    violations: list[str] = []
    status = _git(source, "status", "--porcelain")
    if status is not None and status.returncode == 0:
        for line in status.stdout.splitlines():
            entry = line.strip()
            if not entry:
                continue
            if entry.startswith("??"):
                rel = entry[2:].strip()
                if not _is_whitelisted(rel):
                    violations.append(f"untracked outside whitelist: {rel}")
            else:
                violations.append(f"tracked change: {entry}")
    head = _git(source, "rev-parse", "HEAD")
    if head is not None and head.returncode == 0:
        from shared.source_integrity import get as get_installed_sha

        installed = get_installed_sha()
        head_sha = head.stdout.strip()
        if installed is not None and head_sha != installed:
            violations.append(f"HEAD {head_sha[:7]} moved off installed commit {installed[:7]}")
    return tuple(violations)


@dataclass(frozen=True)
class SourceTreeRepair:
    """What one repair pass did, or failed to do."""

    reset_from: str | None  # previous HEAD sha; None when no reset ran
    reset_to: str | None  # installed_sha the tree was reset to
    cleaned: tuple[str, ...]  # untracked paths removed
    kept_whitelisted: tuple[str, ...]  # untracked paths left in place
    errors: tuple[str, ...]  # git failures that left tampering in place


def repair_source_tree(repo: Path | None = None) -> SourceTreeRepair | None:
    """Reset the checkout to the last fully installed commit and remove
    untracked files outside the whitelist.

    None when the checkout is not a git repo. Never raises: a git failure is
    recorded in ``errors`` so the caller (the converge step) can surface it
    while the health probe keeps alerting on whatever tamper remains.
    """
    source = Path(repo) if repo is not None else prod_source_dir()
    if source is None or not (source / ".git").exists():
        return None
    errors: list[str] = []
    reset_from: str | None = None
    reset_to: str | None = None

    from shared.source_integrity import get as get_installed_sha

    installed = get_installed_sha()
    head = _git(source, "rev-parse", "HEAD")
    if installed is not None and head is not None and head.returncode == 0:
        head_sha = head.stdout.strip()
        if head_sha != installed:
            result = _git(source, "reset", "--hard", installed, timeout=_WRITE_TIMEOUT_S)
            if result is None or result.returncode != 0:
                detail = result.stderr.strip() if result is not None else "git unavailable"
                errors.append(f"git reset --hard {installed[:7]} failed: {detail[:200]}")
            else:
                reset_from, reset_to = head_sha, installed

    ls = _git(source, "ls-files", "--others", "--exclude-standard")
    untracked: tuple[str, ...] = ()
    if ls is not None and ls.returncode == 0:
        untracked = tuple(p for p in ls.stdout.splitlines() if p)
    cleaned: tuple[str, ...] = ()
    kept = tuple(p for p in untracked if _is_whitelisted(p))
    to_clean = tuple(p for p in untracked if not _is_whitelisted(p))
    if to_clean:
        exclusions = [flag for pattern in SOURCE_TREE_WHITELIST for flag in ("-e", pattern)]
        result = _git(source, "clean", "-fd", *exclusions, timeout=_WRITE_TIMEOUT_S)
        if result is None or result.returncode != 0:
            detail = result.stderr.strip() if result is not None else "git unavailable"
            errors.append(f"git clean -fd failed: {detail[:200]}")
        else:
            cleaned = to_clean

    if reset_from is not None or cleaned:
        shared.telemetry.emit(
            "telemetry",
            "source_tree_reset",
            level="warning",
            source="converge",
            attributes={
                "reset_from": reset_from,
                "reset_to": reset_to,
                "cleaned": list(cleaned),
                "kept_whitelisted": list(kept),
            },
        )
    return SourceTreeRepair(
        reset_from=reset_from,
        reset_to=reset_to,
        cleaned=cleaned,
        kept_whitelisted=kept,
        errors=tuple(errors),
    )
