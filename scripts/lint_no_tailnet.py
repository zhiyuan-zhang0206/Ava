#!/usr/bin/env python3
"""Forbid tailnet IP literals (100.64.0.0/10 host addresses) in the repo.

Run: `.venv/bin/python scripts/lint_no_tailnet.py` (whole repo, git-tracked
files only). Also run automatically via pre-commit and in CI (the `repo-language`
job next to the no-CJK scan, so a docs-only or test-only PR cannot slip a
deployment address past a job that classifies by code side).

## Why

User ruling 2026-08-20 (+ the 2026-08-03/04 Gateway-URL rule): the repo must
not carry a deployment's private overlay addresses as literals. The cluster's
hand-visible URL is derived from the `AVA_GATEWAY_URL` variable, never
hardcoded; a literal would leak the deployment topology into a public repo
and mislead a consumer that has a different overlay. What is neutral and
allowed is the RANGE NAME — `100.64.0.0/10` (the CGNAT / VPN-overlay range) —
because naming the range is how the code documents its own policy; only a
concrete four-octet address inside it is banned.

## What is scanned

Every git-tracked file (`git ls-files`), so untracked build output
(node_modules, .next, coverage, logs) is out of scope by construction. Binary
files (NUL byte in the head, or non-UTF-8) are skipped. The pattern is a
dotted-quad IPv4 literal whose first two octets are in 100.64.0.0/10
(the CGNAT / VPN-overlay range — first octet pair 100.64 through 100.127)
and that is NOT followed by `/NN` — the slash
form is the CIDR range notation, not a host address.

## Exemptions

- `decisions/` — frozen historical narrative (2026-08-20 ruling: never
  rewritten, never extended).
- An inline `# tailnet-ip-ok: <reason>` marker on the same line as the
  literal, for tests that genuinely exercise the 100.64.0.0/10 range
  boundary (the exception that proves the gate: a boundary test's whole
  point is a value inside the range).

Error format `file:line: <literal> | <line content>` + non-zero exit.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Project root (this script lives under scripts/)
_REPO_ROOT = Path(__file__).resolve().parent.parent

# A concrete host address inside 100.64.0.0/10. The negative lookahead keeps
# the CIDR range notation ("100.64.0.0/10") out of scope — it names the
# range, it is not an address.
_TAILNET_IP_RE = re.compile(
    r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b(?!/\d{1,2})"
)

# Repo-relative path prefixes that are frozen historical narrative and never
# scanned (user ruling 2026-08-20).
_FROZEN_PATH_PREFIXES = ("decisions/",)

# Inline opt-out marker, same convention as the other repo lints
# (# env-ok: / # wrap-ok: / # emoji-ok:).
_OPT_OUT_MARKER = "tailnet-ip-ok:"


def _is_frozen_path(rel_path: str) -> bool:
    return any(rel_path.startswith(prefix) for prefix in _FROZEN_PATH_PREFIXES)


def _tracked_files() -> list[str]:
    """Every git-tracked file, repo-relative, posix separators."""

    # Fixed command line ("git ls-files"), no untrusted input; the repo path
    # comes from this script's own __file__, never from callers.
    out = subprocess.run(  # noqa: S603 - fixed argv, repo-root derived from __file__
        ["git", "-C", str(_REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return [p for p in out.stdout.splitlines() if p]


def _scan_file(rel_path: str) -> list[tuple[int, str, str]]:
    """Return violations [(lineno, literal, line_stripped), ...]."""
    if _is_frozen_path(rel_path):
        return []
    path = _REPO_ROOT / rel_path
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
    violations: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _TAILNET_IP_RE.search(line)
        if m is not None and _OPT_OUT_MARKER not in line:
            violations.append((lineno, m.group(0), line.strip()))
    return violations


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv:
        # Explicit paths: resolve against the repo root when a relative path
        # does not exist under the caller's CWD (pre-commit passes absolute
        # paths; a manual `python3 scripts/lint_no_tailnet.py some/file` runs
        # from the repo root anyway, but a test or wrapper may not).
        targets = []
        for a in argv:
            p = Path(a)
            if not p.is_absolute():
                cand = _REPO_ROOT / p
                if cand.exists():
                    p = cand
            targets.append(p.resolve())
        files = []
        for t in targets:
            try:
                rel = t.relative_to(_REPO_ROOT).as_posix()
            except ValueError:
                rel = t.as_posix()
            if t.is_file():
                files.append(rel)
            elif t.is_dir():
                files.extend(
                    p.relative_to(_REPO_ROOT).as_posix() for p in t.rglob("*") if p.is_file()
                )
        # Explicit paths: scan them (git-tracked or not — a worktree edit
        # that is not yet added still must be caught).
        scan = sorted(set(files))
    else:
        scan = _tracked_files()

    total = 0
    for rel in scan:
        if rel.startswith(".git/"):
            continue
        for lineno, literal, content in _scan_file(rel):
            total += 1
            print(f"{rel}:{lineno}: {literal} | {content}")

    if total:
        print(
            "\nTailnet IP literal found in the repo. Derive the address from "
            "configuration (AVA_GATEWAY_URL / reachable_host()), use a neutral "
            "site like a 10.x literal for opaque test fixtures, or, when the "
            "test genuinely exercises the 100.64.0.0/10 range, annotate the "
            "line with `# tailnet-ip-ok: <reason>`. The CIDR notation "
            "`100.64.0.0/10` is allowed — it names the range, it is not an "
            "address. See the docstring at the top of scripts/lint_no_tailnet.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
