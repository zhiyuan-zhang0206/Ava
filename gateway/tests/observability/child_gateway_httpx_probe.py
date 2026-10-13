"""Fresh-interpreter probe: does importing the gateway entry load HTTPX?

Prints one JSON line. Run by `test_gateway_entry_imports_httpx.py`.
"""

import json
import sys

import gateway.app  # pyright: ignore[reportUnusedImport]
import gateway.cluster.server  # noqa: F401  # pyright: ignore[reportUnusedImport]

if __name__ == "__main__":
    sys.stdout.write(json.dumps({"httpx_loaded": "httpx" in sys.modules}) + "\n")
