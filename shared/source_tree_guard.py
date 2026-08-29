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

# Prefix of the distinguishable "the guard could not evaluate the checkout"
# marker returned by ``source_tree_violations``. A blind guard must not look
# like a clean tree: a broken git is exactly the state in which tampering
# becomes invisible (and may be the work of the same actor that tampered).
GUARD_SKIPPED_PREFIX = "guard skipped: "


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


def _pattern_matches(pattern: str, rel_path: str) -> bool:
    """True when one whitelist pattern covers a git-relative path."""
    if fnmatch.fnmatch(rel_path, pattern):
        return True
    return pattern.endswith("/") and rel_path.startswith(pattern)


def _is_whitelisted(rel_path: str) -> bool:
    """True when a git-relative path is a legal runtime artifact."""
    return any(_pattern_matches(pattern, rel_path) for pattern in SOURCE_TREE_WHITELIST)


# Sentinel paths no runtime artifact should ever match: a whitelist pattern
# accepting one of them would whitelist arbitrary untracked files (or, for a
# bare "*"-class glob, everything) and make the guard a silent no-op.
_WHITELIST_CATCHALL_PROBES = ("guard-must-not-be-whitelisted.txt", "guard-must-not-be-whitelisted/")


def _validate_whitelist(patterns: tuple[str, ...]) -> None:
    """Fail fast on a misconfigured whitelist (called at import).

    Two misconfigurations make the guard a silent no-op on one side or a
    false-alarm machine on the other, and without a loud check neither is
    visible until the guard is needed and fails: an empty whitelist flags
    every untracked file as tamper (and repair would delete the runtime
    artifacts), while a catch-all pattern (``"*"``, ``"**"``, …) whitelists
    anything — detection is always empty, repair never acts.
    """
    if not patterns:
        raise ValueError(
            "SOURCE_TREE_WHITELIST must be non-empty: with no entries every untracked "
            "file is tamper and repair would remove the runtime artifacts"
        )
    for pattern in patterns:
        if not pattern:
            raise ValueError(f"SOURCE_TREE_WHITELIST entry {pattern!r} is empty")
        for probe in _WHITELIST_CATCHALL_PROBES:
            if _pattern_matches(pattern, probe):
                raise ValueError(
                    f"SOURCE_TREE_WHITELIST pattern {pattern!r} would whitelist arbitrary "
                    "paths (the guard would never detect or repair anything)"
                )


_validate_whitelist(SOURCE_TREE_WHITELIST)


def source_tree_violations(repo: Path | None = None) -> tuple[str, ...]:
    """Human-readable tamper findings for the prod source checkout.

    Returns ``()`` only when the guard SAW the tree and found nothing wrong.
    When the checkout exists but the guard cannot evaluate it — not a git
    checkout, or a git command failed (binary missing, command error,
    timeout) — the result carries a ``{GUARD_SKIPPED_PREFIX}...`` marker
    instead of ``()``, so the health probe reports the guard itself as
    failing rather than mistaking a blind guard for a clean tree (best-effort
    against transient git hiccups, but never silently blind — a broken git is
    exactly the state in which tampering becomes invisible). Detects three
    tamper shapes:

    - tracked files changed vs HEAD (any non-untracked ``git status`` entry)
    - untracked files outside the whitelist
    - HEAD moved off the last fully installed commit (``installed_sha``)
    """
    source = Path(repo) if repo is not None else prod_source_dir()
    if source is None:
        return ()
    if not (source / ".git").exists():
        return (f"{GUARD_SKIPPED_PREFIX}not a git checkout",)
    violations: list[str] = []
    blind = False
    status = _git(source, "status", "--porcelain")
    if status is None or status.returncode != 0:
        blind = True
    else:
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
    if head is None or head.returncode != 0:
        blind = True
    else:
        from shared.source_integrity import get as get_installed_sha

        installed = get_installed_sha()
        head_sha = head.stdout.strip()
        if installed is not None and head_sha != installed:
            violations.append(f"HEAD {head_sha[:7]} moved off installed commit {installed[:7]}")
    if blind:
        violations.append(f"{GUARD_SKIPPED_PREFIX}git unavailable")
    return tuple(violations)


@dataclass(frozen=True)
class SourceTreeRepair:
    """What one repair pass did, or failed to do."""

    reset_from: str | None  # previous HEAD sha; None when no reset ran
    reset_to: str | None  # installed_sha the tree was reset to
    cleaned: tuple[str, ...]  # untracked paths removed
    kept_whitelisted: tuple[str, ...]  # untracked paths left in place
    errors: tuple[str, ...]  # git failures that left tampering in place


def _tracked_dirty(source: Path) -> bool:
    """True when any tracked file differs from HEAD (modification, staging,
    deletion, rename — anything `git status` shows that is not untracked)."""
    status = _git(source, "status", "--porcelain")
    if status is None or status.returncode != 0:
        return False
    return any(not line.startswith("??") for line in status.stdout.splitlines())


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
        # Reset when HEAD moved off the installed commit OR tracked files are
        # dirty — the 2026-08-28 outage's main shape (a tracked file edited in
        # place, never committed) leaves HEAD == installed, so a
        # HEAD-comparison-only reset would never repair it. `git reset --hard`
        # to the same commit still discards working-tree modifications.
        # Untracked files alone never trigger the reset: they belong to the
        # clean step below (whitelisted ones are kept).
        tracked_dirty = _tracked_dirty(source)
        if head_sha != installed or tracked_dirty:
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
