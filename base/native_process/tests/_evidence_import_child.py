"""Imports the runtime evidence types alone; never imported by a production entry.

Run as ``python -I -B _evidence_import_child.py <repo-root>``.
"""

from __future__ import annotations

import sys

sys.path.insert(0, sys.argv[1])
import base.native_process.evidence  # noqa: F401  # pyright: ignore[reportUnusedImport]

loaded = [
    name
    for name in sys.modules
    if name.startswith(("base.managed_writer", "base.runtime_publication", "cli.commands._update"))
]
assert not loaded, loaded
