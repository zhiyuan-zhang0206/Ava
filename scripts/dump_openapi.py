"""Dump the FastAPI app's OpenAPI spec to JSON — used by the frontend
`openapi-typescript` codegen (single source of truth; when backend Pydantic
schemas change, the frontend types-generated.ts also needs codegen to sync).

Usage:
    .venv/bin/python scripts/dump_openapi.py [out_path]

Default writes to `ui/web/openapi.json`. The npm `codegen:types` script
invokes this and then runs openapi-typescript to convert the JSON to TS.

import does not trigger lifespan (FastAPI app instantiation), so the DB
pool does not come up — pure schema extraction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Put project root on sys.path so `from gateway.app import app` finds the module.
# Same pattern as scripts/start_gateway.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ava.self.AGENT_ID stays None here — fine; dump is a build-time tool that
# does not exercise the SDK or make any gateway HTTP calls.


def main() -> int:
    from gateway.app import app

    spec = app.openapi()
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "ui/web/openapi.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    # Trailing newline so the pre-commit `end-of-file-fixer` hook stops
    # touching this generated file (POSIX text file convention + reduces
    # noise of "false drift" from the codegen-fresh hook).
    out.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"OpenAPI spec -> {out} ({len(spec.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
