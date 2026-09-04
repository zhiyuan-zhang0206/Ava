"""`ava memory` — memory pool operations.

Wraps the git / gh / curl operations from the ava-memory skill so agents
call a single command instead of hand-typing shell pipelines. Each verb is
a standalone Python function callable from the cli/parsers dispatch tree.

Verbs:
  init          Explicitly initialize the memory pool and plugin-owned templates
  refresh       Trigger the gateway to re-index the memory pool
  search        Search gateway memory and render relative paths with metadata
"""

from __future__ import annotations

import sys
from typing import TypedDict, cast

import httpx

from shared.http_dial import post as dial_post
from shared.machine import gateway_api_base, gateway_auth_headers
from shared.memory_repo import MemoryBranchMismatch

_TIMEOUT_S = 30.0


class _MemorySearchResult(TypedDict):
    """Gateway result fields the human renderer reads; raw JSON stays untouched."""

    path: str
    tags: list[str] | None
    description: str | None


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


def _human_search_rows(results: list[_MemorySearchResult]) -> list[tuple[str, str, str]]:
    """Normalize nullable display metadata without changing the JSON response."""
    rows: list[tuple[str, str, str]] = []
    for result in results:
        if not isinstance(result, dict):
            raise TypeError("memory search results must contain objects")
        path = result["path"]
        raw_tags = result.get("tags")
        tags = ", ".join(str(tag) for tag in raw_tags) if raw_tags else ""
        raw_description = result.get("description")
        description = "" if raw_description is None else str(raw_description)
        rows.append((path, tags, description))
    return rows


def _print_search_table(results: list[_MemorySearchResult]) -> None:
    rows = _human_search_rows(results)
    path_width = max(len("path"), *(len(path) for path, _, _ in rows))
    tags_width = max(len("tags"), *(len(tags) for _, tags, _ in rows))
    print(f"{'path'.ljust(path_width)}  {'tags'.ljust(tags_width)}  description")
    for path, tags, description in rows:
        print(f"{path.ljust(path_width)}  {tags.ljust(tags_width)}  {description}")


def cmd_memory_search(query: str, *, limit: int, json_output: bool) -> int:
    """Search gateway memory while keeping paths relative to its memory root."""
    import json

    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}/api/memory/search"
    resp = dial_post(
        url,
        json={"query": query, "k": limit},
        timeout=_TIMEOUT_S,
        headers=gateway_auth_headers(),
    )
    if resp.status_code >= 400:
        print(resp.text, file=sys.stderr)
    resp.raise_for_status()
    raw_payload: object = resp.json()
    if not isinstance(raw_payload, dict):
        raise TypeError("memory search response must be an object")
    payload = cast(dict[str, object], raw_payload)
    raw_results: object = payload["results"]
    if not isinstance(raw_results, list):
        raise TypeError("memory search response results must be a list")
    results = cast(list[_MemorySearchResult], raw_results)
    if json_output:
        print(json.dumps(results))
    else:
        _print_search_table(results)
    return 0
