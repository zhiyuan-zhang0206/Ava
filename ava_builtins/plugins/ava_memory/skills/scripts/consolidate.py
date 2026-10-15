from __future__ import annotations

import argparse
import sys

from ava_builtins.plugins.ava_memory.pool_ops import (
    pool_dir,
    refresh_index,
    regenerate_indexes,
    run,
    stage_and_commit,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Single-box memory consolidation")
    ap.add_argument("-m", "--message", required=True, help="commit message")
    args = ap.parse_args()

    pool = pool_dir()
    print(f"memory pool: {pool}")

    regenerate_indexes()
    committed = stage_and_commit(args.message, pool)
    run(["git", "-C", str(pool), "push"])
    if committed:
        refresh_index()
    else:
        print("  (no changes to push — skipping refresh)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
