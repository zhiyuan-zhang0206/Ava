"""Source-tree integrity detector for a home that runs from its source checkout.

The 2026-08-28 fleet-wide outage (a half-installed ``ava_ledger`` plugin) and
earlier incidents of the same class all trace to someone editing the prod
checkout (``$AVA_HOME/source``) in place — a tree edit broke ``import ava`` for
every agent on the box within minutes. User ruling 2026-08-28: tampering is
detected before exec crashes, and legitimate runtime artifacts are allowlisted.

``ava cluster health-probe`` check 8 is the one caller: read-only detection on
the OS-cron cadence. It alerts on tamper and never writes. The check reports
changed tracked files and untracked files outside ``SOURCE_TREE_WHITELIST``.
A moved HEAD alone is not tamper: no current lifecycle records an installed
commit for a source checkout (the retired updater's ``installed_sha`` bookmark
has no writer), and an operator legitimately moves the checkout before an
``ava restart``. Running-versus-checkout drift is shown, not alerted, by the
roster ``code`` column.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

from shared.cluster_drift import prod_source_dir
from shared.deploy.git.gitenv import git_env
from shared.proc import run_bounded

# Bounded git: a guard must never hang a probe (mirrors
# ``cluster_drift._GIT_TIMEOUT_S``).
_READ_TIMEOUT_S = 5.0

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
    every untracked file (including the runtime artifacts) as tamper, while a
    catch-all pattern (``"*"``, ``"**"``, …) whitelists anything — detection is
    always empty.
    """
    if not patterns:
        raise ValueError(
            "SOURCE_TREE_WHITELIST must be non-empty: with no entries every untracked "
            "file, the runtime artifacts included, is tamper"
        )
    for pattern in patterns:
        if not pattern:
            raise ValueError(f"SOURCE_TREE_WHITELIST entry {pattern!r} is empty")
        for probe in _WHITELIST_CATCHALL_PROBES:
            if _pattern_matches(pattern, probe):
                raise ValueError(
                    f"SOURCE_TREE_WHITELIST pattern {pattern!r} would whitelist arbitrary "
                    "paths (the guard would never detect anything)"
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
    exactly the state in which tampering becomes invisible). Detects two
    tamper shapes:

    - tracked files changed vs HEAD (any non-untracked ``git status`` entry)
    - untracked files outside the whitelist
    """
    source = Path(repo) if repo is not None else prod_source_dir()
    if source is None:
        return ()
    if not (source / ".git").exists():
        return (f"{GUARD_SKIPPED_PREFIX}not a git checkout",)
    status = _git(source, "status", "--porcelain")
    if status is None or status.returncode != 0:
        return (f"{GUARD_SKIPPED_PREFIX}git unavailable",)
    violations: list[str] = []
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
    return tuple(violations)
