"""Wire-content field constraints for the ops RPC schemas.

The lifecycle request bodies in the `ops.rpc_schemas` door and its `terminate`
submodule share the same content guardrail.
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
