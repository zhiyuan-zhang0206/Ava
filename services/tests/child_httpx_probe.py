"""Fresh-interpreter probe: does importing one service entry load HTTPX?

Usage: `child_httpx_probe.py <entry>`; prints one JSON line. The entry names match
`ENTRIES` in `test_service_entries_httpx_import.py`.
"""

import json  # pyright: ignore[reportUnusedImport]
import sys


def _import_entry(entry: str) -> None:
    if entry == "heartbeat":
        import services.wake.heartbeat.daemon  # pyright: ignore[reportUnusedImport]
    elif entry == "memory-indexer":
        import services.derived.memory_indexer.daemon  # pyright: ignore[reportUnusedImport]
    elif entry == "memory-search":
        import services.derived.memory_search.daemon  # pyright: ignore[reportUnusedImport]
    elif entry == "ops":
        import services.agent_runner.agent_ops.daemon  # pyright: ignore[reportUnusedImport]
    elif entry == "browser-mcp":
        import services.desktop.browser.mcp_daemon  # pyright: ignore[reportUnusedImport]
    elif entry == "agent-host":
        import services.agent_runner.agent_host.daemon  # noqa: F401  # pyright: ignore[reportUnusedImport]
    else:
        raise SystemExit(f"unknown entry {entry!r}")


if __name__ == "__main__":
    _import_entry(sys.argv[1])
    sys.stdout.write(json.dumps({"httpx_loaded": "httpx" in sys.modules}) + "\n")
