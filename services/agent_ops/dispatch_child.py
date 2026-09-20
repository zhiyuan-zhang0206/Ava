"""One-shot dispatch child behind the restricted observer's `/ops` allowlist.

`services/agent_ops/bootstrap.py` must never import the ops stack or ordinary
Settings (its startup refuses an interpreter that imported `shared.config`), so
an admitted effect op is executed here instead: the observer spawns this module
as a short-lived child of the same image and relays what it prints.

The child runs the daemon's own routing -- `daemon.dispatch_once` -- in a
process where Settings, the database pool and every ops import are ordinary.
stdout carries exactly one JSON object, `{"status", "result"}`: the same
response body the full daemon's `/ops` would return for the op. Logs go to
stderr; a child that cannot answer at all degrades, on the observer's side, to
the same failed envelope the daemon returns for a crashed dispatch.
"""

from __future__ import annotations

import json
import sys

from pydantic import ValidationError

from services.agent_ops import daemon
from shared.op_envelope import OpEnvelope


def main() -> int:
    try:
        envelope = OpEnvelope.model_validate(json.loads(sys.stdin.buffer.read()))
    except (json.JSONDecodeError, ValidationError) as exc:
        sys.stdout.write(
            json.dumps({"status": "failed", "result": {"error": f"invalid envelope: {exc}"}})
        )
        return 0
    status, result = daemon.dispatch_once(
        envelope.kind, envelope.payload, idempotency_key=envelope.idempotency_key
    )
    sys.stdout.write(json.dumps({"status": status, "result": result}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
