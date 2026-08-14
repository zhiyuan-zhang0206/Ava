"""Start the Gateway HTTP server (FastAPI 0.0.0.0:8000, reachable on the cluster's private network).

Equivalent to `.venv/bin/python -m gateway` — this script is just a
convenience wrapper, uniformly placed under `scripts/` for easy
lookup / IDE jump-to.

Gateway semantics: see gateway/gateway.ava.okf.md —
stateless HTTP adapter providing HTTP wrapping of lifecycle + UI ops for
HTTP-only clients (browser / CLI / admin). **Not a gateway / supervisor.**
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.app import main

if __name__ == "__main__":
    main()
