"""Wire-content field constraints for the ops RPC schemas.

Split out of `ops/rpc_schemas.py` when that file crossed the per-file line
ceiling; the lifecycle request bodies in `ops/rpc_schemas.py` and
`ops/rpc_terminate.py` share the same content guardrail.
"""

from typing import Annotated

from pydantic import StringConstraints

_MAX_CONTENT_CHARS = 1_000_000
"""Prompt/reply content needs a memory-abuse guardrail, not a 64 KiB wire contract.

The model provider context window is the downstream input bound; one million
characters is roughly 1 MiB, leaving legitimate handoffs and reports intact
while failing fast on abusive request bodies.
"""

UserContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=_MAX_CONTENT_CHARS),
]
