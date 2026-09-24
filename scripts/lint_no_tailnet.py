#!/usr/bin/env python3
"""Forbid tailnet IP literals (100.64.0.0/10 host addresses) in the repo.

Run: `.venv/bin/python scripts/lint_no_tailnet.py [path ...]` (defaults to the
whole repo, git-tracked files only; an explicit path that does not exist is an
error (stderr + exit 1) rather than a silent no-op). Also run automatically via
pre-commit and in CI (the `repo-language`
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
import sys
from pathlib import Path

# Project root (this script lives under scripts/)
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import lint_common  # noqa: E402 - standalone script

# A concrete host address inside 100.64.0.0/10. The negative lookahead keeps
# the CIDR range notation ("100.64.0.0/10") out of scope — it names the
# range, it is not an address.
_TAILNET_IP_RE = re.compile(
    r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b(?!/\d{1,2})"
)

# Repo-relative path prefixes that are frozen historical narrative and never
# scanned (user ruling 2026-08-20).
_FROZEN_PATH_PREFIXES = ("decisions/",)

# Inline opt-out marker "<name>-ok:", same convention as the other repo lints.
_OPT_OUT_MARKER = "tailnet-ip-ok:"


def _is_frozen_path(rel_path: str) -> bool:
    return any(rel_path.startswith(prefix) for prefix in _FROZEN_PATH_PREFIXES)


def _tracked_files() -> list[str]:
    """Every git-tracked file, repo-relative, posix separators."""
    return lint_common.tracked_files(_REPO_ROOT)


def _scan_file(rel_path: str) -> list[tuple[int, str, str]]:
    """Return violations [(lineno, literal, line_stripped), ...]."""
    if _is_frozen_path(rel_path):
        return []
    text = lint_common.read_utf8_text(_REPO_ROOT / rel_path)
    if text is None:
        return []
    violations: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _TAILNET_IP_RE.search(line)
        if m is not None and _OPT_OUT_MARKER not in line:
            violations.append((lineno, m.group(0), line.strip()))
    return violations


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv:
        scan, missing = lint_common.resolve_targets(argv, _REPO_ROOT)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    else:
        scan = _tracked_files()

    total = 0
    for rel in scan:
        if rel.startswith(".git/"):
            continue
        for lineno, literal, content in _scan_file(rel):
            total += 1
            print(lint_common.format_violation(rel, lineno, literal, content))

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
