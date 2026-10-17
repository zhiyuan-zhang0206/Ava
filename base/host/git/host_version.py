"""Host version — derived from the checkout's commit, never hand-maintained.

The host version is the date axis of the running build: `YYYY.M.D` from the
commit date of the checkout's HEAD, with `+g<short-sha>` added in the display
form. It always exists on every checkout-mode machine, advances by itself,
and needs no release process and no bump discipline (task #2915 design §5.5
v3 — the hand-maintained `[project].version` was rejected as the gate source;
it survives only as the checkout-less fallback).

The gates compare against the bare `YYYY.M.D` string — `engines.meiva` ranges
via `base.packages.plugins.manifest.range_allows` plus the `requires_commit` ancestor
check. Prefer the display form only where a human reads it (status output,
log lines).

Fallback chain: git-derived date -> `[project].version` from `pyproject.toml`
(the checkout-less / wheel case) -> `HostVersionError` (callers surface
"unknown" rather than guessing a number a gate would trust).
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path
from typing import Any

from base.host.git.gitenv import git_env
from base.host.proc import run_bounded
from base.paths import repo_root as _repo_root

_GIT_TIMEOUT_S = 10.0


class HostVersionError(RuntimeError):
    """No host version could be derived — not a git checkout and no usable
    `pyproject.toml` version. Callers must not substitute a default."""


# ── version grammar ─────────────────────────────────────────────────────
# The three-segment version every gate compares: the host's own and a plugin
# manifest's (`base.packages.plugins.manifest` builds its ranges on it).

_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?$")
_PRE_RE = re.compile(r"^(dev|alpha|a|beta|b|preview|pre|rc|c)(?:[.-]?(\d+))?$", re.I)
_PRE_ALIASES = {"alpha": "a", "beta": "b", "preview": "rc", "pre": "rc", "c": "rc"}
_PRE_ORDER = {"dev": 0, "a": 1, "b": 2, "rc": 3}


def _normalize_prerelease(raw: str) -> str:
    match = _PRE_RE.match(raw)
    if match is None:
        return raw.lower()
    label = _PRE_ALIASES.get(match[1].lower(), match[1].lower())
    return f"{label}.{int(match[2] or 0)}"


def _prerelease_key(raw: str) -> tuple[int, int, str]:
    match = re.fullmatch(r"(dev|a|b|rc)\.(\d+)", raw)
    if match is not None:
        return (_PRE_ORDER[match[1]], int(match[2]), "")
    return (4, 0, raw)


@total_ordering
@dataclass(frozen=True)
class Version:
    """Three-segment version; prereleases sort below the matching release."""

    major: int
    minor: int = 0
    patch: int = 0
    prerelease: str | None = None

    def __lt__(self, other: Version) -> bool:
        left = (self.major, self.minor, self.patch)
        right = (other.major, other.minor, other.patch)
        if left != right:
            return left < right
        if self.prerelease is None:
            return False
        if other.prerelease is None:
            return True
        return _prerelease_key(self.prerelease) < _prerelease_key(other.prerelease)


def parse_version(raw: str) -> Version | None:
    m = _VERSION_RE.match(raw.strip())
    if m is None:
        return None
    prerelease = _normalize_prerelease(m[4]) if m[4] is not None else None
    return Version(int(m[1]), int(m[2] or 0), int(m[3] or 0), prerelease)


def host_version_from_repo(repo: Path) -> str:
    """The Meiva version this checkout carries (`[project].version` in its
    pyproject.toml) — the host version `engines.meiva` is checked against."""
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        raise HostVersionError(f"cannot determine host version: no pyproject.toml in {repo}")
    try:
        data: dict[str, Any] = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise HostVersionError(f"cannot determine host version: {e}") from e
    project: dict[str, Any] | None = data.get("project")
    version = project.get("version") if project is not None else None
    if not isinstance(version, str) or parse_version(version) is None:
        raise HostVersionError(
            f"cannot determine host version: {pyproject} [project].version invalid"
        )
    return version


def _derived(repo: Path) -> tuple[str, str] | None:
    """(bare version, short sha) from `repo`'s HEAD commit, or None."""
    try:
        result = run_bounded(  # git + fixed args, no user input
            ["git", "-C", str(repo), "log", "-1", "--format=%cd|%h", "--date=short"],
            capture_output=True,
            text=True,
            env=git_env(),
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    date_part, _, sha = result.stdout.strip().partition("|")
    try:
        year, month, day = date_part.split("-")
        return f"{int(year)}.{int(month)}.{int(day)}", sha
    except ValueError:
        return None


def host_version(repo: Path | None = None) -> str:
    """The gate-comparable host version: bare `YYYY.M.D`.

    Raises:
        HostVersionError: no git commit and no readable pyproject version.
    """
    repo = repo or _repo_root()
    derived = _derived(repo)
    if derived is not None:
        return derived[0]
    return host_version_from_repo(repo)


def host_version_display(repo: Path | None = None) -> str:
    """The human-facing form: `YYYY.M.D+g<short-sha>` when the checkout is a
    git repo, bare otherwise (falls back through `host_version`)."""
    repo = repo or _repo_root()
    derived = _derived(repo)
    if derived is not None:
        return f"{derived[0]}+g{derived[1]}"
    return host_version(repo)
