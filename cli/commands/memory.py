"""`ava memory` — memory pool operations.

Wraps the git / gh / curl operations from the ava-memory skill so agents
call a single command instead of hand-typing shell pipelines. Each verb is
a standalone Python function callable from the cli/parsers dispatch tree.

Verbs:
  init          Explicitly initialize the memory pool and plugin-owned templates
  refresh       Trigger the gateway to re-index the memory pool
"""

from __future__ import annotations

import sys

import httpx

from shared.http_dial import post as dial_post
from shared.machine import gateway_api_base, gateway_auth_headers
from shared.memory_repo import MemoryBranchMismatch

_TIMEOUT_S = 30.0


def cmd_memory_init() -> int:
    """Explicitly initialize enabled plugin-owned memory resources.

    Memory repository branch validation belongs to this operator-requested
    provisioning path, never to converge or service startup.
    """
    from cli.commands._converge_plugins import run_plugin_scaffolds

    print("initializing memory resources...")
    try:
        result = run_plugin_scaffolds()
    except MemoryBranchMismatch as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 1
    if result.ran:
        print(f"scaffolded: {', '.join(result.ran)}")
    else:
        print("no enabled plugin scaffolds")
    return 0


def _refresh_index() -> None:
    """POST /api/memory/refresh to the gateway so it re-indexes."""
    url = f"{gateway_api_base()}/api/memory/refresh"
    try:
        resp = dial_post(url, timeout=_TIMEOUT_S, headers=gateway_auth_headers())
        resp.raise_for_status()
        data = resp.json()
        print(f"  index refreshed: {data.get('status', 'ok')}")
    except httpx.HTTPError as exc:
        print(f"  ✗ refresh failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def cmd_memory_refresh() -> int:
    """Trigger the gateway to re-index the memory pool.

    POSTs /api/memory/refresh so the memory indexer picks up the
    latest consolidated pool (merged main) and re-embeds changed files.
    """
    print("refreshing memory index...")
    _refresh_index()
    return 0
