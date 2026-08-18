#!/bin/bash
# Codegen `ui/web/src/lib/types-generated.ts` synced from Pydantic schemas —
# single source of truth. Changing a Pydantic model (gateway/schemas/ etc.)
# requires running this command to regenerate; otherwise the frontend uses
# the old schema.
#
# Steps:
#   1. .venv/bin/python scripts/dump_openapi.py -> ui/web/openapi.json
#      (FastAPI app.openapi() exports Pydantic models as an OpenAPI 3.1 spec)
#   2. cd ui/web && openapi-typescript ./openapi.json -o src/lib/types-generated.ts
#      (OpenAPI spec -> TypeScript types, components["schemas"]["X"] shape)
#
# Pre-commit hook runs this script + `git diff --exit-code` as the safety
# net — when a commit changes the schema without running codegen, the
# hook turns red, fail-loud.

set -e

cd "$(dirname "$0")/.."  # repo root

.venv/bin/python scripts/dump_openapi.py
(cd ui/web && npx --no-install openapi-typescript ./openapi.json -o src/lib/types-generated.ts)
