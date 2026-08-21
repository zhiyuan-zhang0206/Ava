#!/usr/bin/env python
"""`git worktree remove` guard (issue #194) — refuse the removal when live
sessions or processes are anchored under the worktree.

Usage: python scripts/check_worktree_remove.py <worktree-path>

Exits 0 when nothing live is anchored under the path, 1 when there is (the
caller should abort the removal), 2 on usage errors. See
shared/worktree_guard.py for the scan.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.worktree_guard import find_live_anchors


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
