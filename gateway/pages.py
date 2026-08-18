"""Backward-compatible re-export from ops.pages.

After the gateway/agent-runner decoupling (PR #1359), the DB functions
moved to `ops/pages.py`. This stub keeps existing import paths working.
"""

from ops.pages import *  # noqa: F403  # pyright: ignore[reportWildcardImportFromLibrary, reportUnusedImport]
