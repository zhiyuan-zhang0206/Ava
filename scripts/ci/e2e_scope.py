#!/usr/bin/env python3
"""Whether a pull request can skip the e2e jobs: the rule is written here, not decided per run.

e2e is the integration net for product behavior, so it runs whenever either side changed. A
change that only edits unit tests inside packages (`<package>/**/tests/**/*.py`) cannot change
product behavior, and the backend selection already ran every test that can observe the edited
tests. The rule skips e2e exactly when all of these hold, with no further judgment:

- the event is a pull request, and not a Trunk queue branch (the queue always runs everything);
- test selection is enforced and its decision is SELECTED;
- every changed path is a Python file in a package-internal `tests` directory.

Top-level `tests/` is out of scope on purpose: e2e imports `tests.fixtures`, `tests.components`
and its own helpers, so a change there can reach e2e. The premise that e2e imports nothing from
a package-internal tests directory is pinned by `scripts/ci/tests/test_e2e_scope.py`.

Standalone and stdlib-only: the classify job runs it without an environment.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from meizel.roles import packages  # noqa: E402 - standalone script

# The packages that carry their own `tests` directories: the application packages and the
# repository's own tooling (not `tests`, `ui`, the schedule templates or the demos).
PACKAGES = (*packages("application"), *packages("tooling"))
_PACKAGE_UNIT_TEST = re.compile(rf"(?:{'|'.join(PACKAGES)})/(?:[^/]+/)*tests/(?:[^/]+/)*[^/]+\.py")
_QUEUE_PREFIXES = ("trunk-merge/", "trunk-temp/")


def is_package_unit_test(path: str) -> bool:
    """Whether a repo-relative path is a Python file under a package-internal `tests` directory."""
    return _PACKAGE_UNIT_TEST.fullmatch(path) is not None


def skips_e2e(*, event: str, head_ref: str, mode: str, decision: str, changed: list[str]) -> bool:
    """The whole rule: see the module docstring."""
    return (
        event == "pull_request"
        and not head_ref.startswith(_QUEUE_PREFIXES)
        and mode == "enforce"
        and decision == "SELECTED"
        and bool(changed)
        and all(is_package_unit_test(path) for path in changed)
    )


def main(argv: list[str] | None = None) -> int:
    """Print `skip=true` or `skip=false` for the workflow's step output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--mode", default="")
    parser.add_argument("--decision", default="")
    args = parser.parse_args(argv)
    changed = [line.strip() for line in args.changed_files.read_text().splitlines() if line.strip()]
    skip = skips_e2e(
        event=args.event,
        head_ref=args.head_ref,
        mode=args.mode,
        decision=args.decision,
        changed=changed,
    )
    print(f"skip={str(skip).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
