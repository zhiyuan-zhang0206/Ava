"""Fresh-interpreter probes for the memory indexer daemon's cold import.

Usage: `child_daemon_import.py backends <module prefix>` prints the JSON list of
loaded modules under the prefix after importing the daemon;
`child_daemon_import.py timing` prints how long the import took in seconds.
"""

import json  # pyright: ignore[reportUnusedImport]
import sys
import time

if __name__ == "__main__":
    if sys.argv[1] == "backends":
        import services.derived.memory_indexer.daemon  # pyright: ignore[reportUnusedImport]

        prefix = sys.argv[2]
        sys.stdout.write(
            json.dumps(sorted(name for name in sys.modules if name.startswith(prefix)))
        )
    elif sys.argv[1] == "timing":
        started = time.monotonic()
        import services.derived.memory_indexer.daemon  # noqa: F401  # pyright: ignore[reportUnusedImport]

        sys.stdout.write(str(time.monotonic() - started))
    else:
        raise SystemExit(f"unknown probe {sys.argv[1]!r}")
