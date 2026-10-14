"""Starts a throwaway Postgres and never tears it down; never imported by a production entry.

Run as ``python _orphan_owner_child.py <throwaway-base> <url-file>``. The parent kills this process
to leave an orphaned instance for the sweep to reap.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from base.cluster.dataplane import pg_tools
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig

# `cm` must stay referenced: a dropped context manager is finalized, and its
# GeneratorExit would run the very teardown this test needs never to happen.
cm = pg_tools.throwaway_postgres(config=ThrowawayConfig(Path(sys.argv[1])))
url = cm.__enter__()
target = Path(sys.argv[2])
part = Path(sys.argv[2] + ".part")
part.write_text(url)
part.replace(target)
time.sleep(600)
