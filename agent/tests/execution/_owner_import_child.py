"""Imports the exec domain owner alone; never imported by a production entry.

Run as ``python -I -B _owner_import_child.py`` with the owner's agent environment set.
"""

from __future__ import annotations

import sys

import agent.execution.domain_owner  # noqa: F401  # pyright: ignore[reportUnusedImport]

assert "agent.graph" not in sys.modules
assert "ava" not in sys.modules
assert "base.config" not in sys.modules
