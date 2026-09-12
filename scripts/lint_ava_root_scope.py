#!/usr/bin/env python3
"""Forbid permission-domain and macOS-specific symbols in the root supervisor.

Run: `.venv/bin/python scripts/lint_ava_root_scope.py [path ...]` — no paths
scans `services/ava_root/` (the root supervisor's own code); explicit paths
scan exactly those files/directories. Also run automatically via pre-commit
and in CI (the `repo-language` job).

## Why

The root supervisor (`services/ava_root/`) is the platform-neutral core of the
process tree. By design (ruling 2026-09-12) it must stay free of any
permission-domain content, so that the privileged identity / desktop-permission
machinery lives in a separate program behind an explicit adapter boundary.
This lint is the machine-checkable half of that constraint: it scans the root
supervisor's own code for permission-domain and platform-specific names —
including comments and strings, because a mention is how a coupling starts.

## What is scanned

Every text file under the scanned roots. Binary files (a NUL byte in the
file head) and non-UTF-8 files are skipped; `__pycache__` directories and
`.pyc` files are skipped.

## Rules

Three forbidden groups, matched case-insensitively per line:

1. Permission-domain symbols — the desktop-permission / privileged-identity
   stack (helper protocol names, TCC-era authorization names, accessibility
   and screen-capture APIs, Quartz/CoreGraphics primitives, keychain and
   signing tools, automation/Apple events, entitlement vocabulary).
2. macOS-specific concepts — scheduler and IPC mechanism names, system
   frameworks, and OS names that must not appear in platform-neutral code.
3. Other platform-mechanism names (e.g. specific service managers) that
   belong to the OS edge, not the tree's own code.

The lists below are the authoritative vocabulary; extending them is part of
this lint's review surface.

## Exemptions

A line that genuinely must carry one of these names opts out inline with
`# ava-root-scope-ok: <reason>` (same convention as the other repo lints).
The marker exempts only its own line.

Error format `file:line: <symbol> | <line content>` + non-zero exit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Project root (this script lives under scripts/).
_REPO_ROOT = Path(__file__).resolve().parent.parent

# The default scan root: the root supervisor's own code.
_DEFAULT_TARGET = "services/ava_root"

# Inline opt-out marker, same convention as the other repo lints.
_OPT_OUT_MARKER = "ava-root-scope-ok:"

# ── Group 1: permission-domain symbols ──────────────────────────────────────
_PERMISSION_PATTERNS = (
    r"permissions?[\s_-]?helper",
    r"\btcc\b",
    r"\btccd\b",
    r"\bktccservice\w*",
    r"\baxuielement\w*",
    r"\baxisprocesstrusted\w*",
    r"\baxobserver\w*",
    r"\baccessibility\w*",
    r"\bscreen[\s_-]?capture\w*",
    r"\bscreencapture\b",
    r"\bscreen\s+recording\b",
    r"\bquartz\b",
    r"\bcoregraphics\b",
    r"\bcgwindow\w*",
    r"\bcgevent\w*",
    r"\bcgpreflight\w*",
    r"\bcgdisplay\w*",
    r"\bkeychain\w*",
    r"\bsecitem\w*",
    r"\bseckeychain\w*",
    r"security\.framework",
    r"\bsecurity\s+(find|add|delete|import|export)-",
    r"\bosascript\b",
    r"\bapplescript\b",
    r"\bappleevents?\b",
    r"\baedeterminepermission\w*",
    r"\bcodesign\w*",
    r"\bnotariz\w*",
    r"\bentitlements?\b",
)

# ── Group 2: macOS-specific concepts ────────────────────────────────────────
_MACOS_PATTERNS = (
    r"\blaunchd\b",
    r"\blaunchctl\b",
    r"\blaunchagents?\b",
    r"\blaunchdaemons?\b",
    r"\bplist\b",
    r"\bxpc\b",
    r"\bxpc_\w+",
    r"\bcocoa\b",
    r"\bappkit\b",
    r"\bcorefoundation\b",
    r"\bdarwin\b",
    r"\bmacos\b",
    r"\bmac\s+os\b",
    r"\bos\s+x\b",
    r"\bmach\b",
    r"\bmach_port\w*",
)

# ── Group 3: other platform-mechanism names ─────────────────────────────────
_OTHER_OS_PATTERNS = (
    r"\bsystemd\b",
    r"\bschtasks\b",
)

_FORBIDDEN_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("permission-domain", _PERMISSION_PATTERNS),
    ("macos-specific", _MACOS_PATTERNS),
    ("platform-mechanism", _OTHER_OS_PATTERNS),
)

_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (source, re.compile(source, re.IGNORECASE))
    for _group, patterns in _FORBIDDEN_PATTERNS
    for source in patterns
)

Violation = tuple[int, str, str]  # (lineno, symbol, line_stripped)


def _iter_files(targets: list[Path]) -> list[Path]:
    """Expand targets into files; directories are walked recursively."""
    files: list[Path] = []
    for target in targets:
        if target.is_dir():
            files.extend(
                sorted(
                    p
                    for p in target.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
                )
            )
        elif target.is_file():
            files.append(target)
    return files


def scan_file(path: Path) -> list[Violation]:
    """Return violations [(lineno, symbol, line_stripped), ...] for one file."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in data[:8192]:
        return []  # binary
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []  # not UTF-8 text
    violations: list[Violation] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _OPT_OUT_MARKER in line:
            continue
        for _source, pattern in _COMPILED:
            match = pattern.search(line)
            if match is not None:
                violations.append((lineno, match.group(0), line.strip()))
                break
    return violations


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    targets = [Path(arg).resolve() for arg in argv] if argv else [_REPO_ROOT / _DEFAULT_TARGET]
    total = 0
    for path in _iter_files(targets):
        for lineno, symbol, content in scan_file(path):
            total += 1
            try:
                shown = path.relative_to(_REPO_ROOT).as_posix()
            except ValueError:
                shown = path.as_posix()
            print(f"{shown}:{lineno}: {symbol} | {content}")
    if total:
        print(
            f"\n{total} forbidden symbol(s) found in the root supervisor's own "
            "code. The root supervisor must stay free of permission-domain and "
            "platform-specific names — keep the coupling at the OS edge, or, "
            "when a line genuinely must carry a name, annotate it with "
            "`# ava-root-scope-ok: <reason>`. See the docstring at the top of "
            "scripts/lint_ava_root_scope.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
