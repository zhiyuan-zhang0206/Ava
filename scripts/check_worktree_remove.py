#!/usr/bin/env python
"""`git worktree remove` guard (issue #194) — refuse the removal when live
sessions or processes are anchored under the worktree.

Usage: python scripts/check_worktree_remove.py <worktree-path>

Run it with the checkout's interpreter (`.venv/bin/python scripts/check_worktree_remove.py
<path>`): the scan needs psutil, which a system python does not have.

Exits 0 when nothing live is anchored under the path, 1 when there is (the
caller should abort the removal), 2 on usage errors, 3 when a dependency cannot
be imported (the wrong interpreter: no verdict was reached, so neither "clean"
nor "refused"). See base/deploy/git/worktree_guard.py for the scan.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from base.host.env.dotenv_boot import skip_config_fetch

if __name__ == "__main__":
    # The scan reads this machine's live session records (`$AVA_HOME/run/pty`), so it
    # keeps the real home (no scratch home here). It reads only: no gateway fetch, no
    # database, no writes under the home.
    skip_config_fetch()


EXIT_MISSING_DEPENDENCY = 3

try:
    from base.deploy.git.worktree_guard import find_live_anchors
except ModuleNotFoundError as error:
    print(
        f"MISSING DEPENDENCY {error.name}: {sys.executable} cannot run the live-anchor scan, "
        "so no verdict was reached. Run it with the checkout's interpreter: "
        ".venv/bin/python scripts/check_worktree_remove.py <worktree-path>",
        file=sys.stderr,
    )
    raise SystemExit(EXIT_MISSING_DEPENDENCY) from None


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/check_worktree_remove.py <worktree-path>", file=sys.stderr)
        return 2
    target = Path(sys.argv[1])
    hits = find_live_anchors(target)
    if hits:
        print(f"REFUSE {target}: {len(hits)} live anchor(s) would be killed by removal:")
        for hit in hits:
            print(f"  - {hit}")
        return 1
    print(f"OK {target}: nothing live anchored under it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
