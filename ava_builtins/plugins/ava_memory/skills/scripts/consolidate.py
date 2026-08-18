"""Single-box consolidation: stage, commit, push, refresh index.

Usage: python3 consolidate.py -m "memory: <machine> <date> daily sync"

The single-box flow for a checkout that tracks its own branch directly.
For the multi-host flow see steward.py (per-machine) and arbiter_merge.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import pool_dir, refresh_index, run, stage_and_commit


def main() -> int:
    ap = argparse.ArgumentParser(description="Single-box memory consolidation")
    ap.add_argument("-m", "--message", required=True, help="commit message")
    args = ap.parse_args()

    pool = pool_dir()
    print(f"memory pool: {pool}")

    committed = stage_and_commit(args.message, pool)
    run(["git", "-C", str(pool), "push"])
    if committed:
        refresh_index()
    else:
        print("  (no changes to push — skipping refresh)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
